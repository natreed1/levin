"""Unified workflow messenger — FastAPI backend (accounts + chats + ledger APIs)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from messenger.auth import (
    COOKIE_NAME,
    invite_ok,
    normalize_name,
    normalize_title,
)
from messenger.db import MessageStore
from messenger.deps import (
    clear_session_cookie,
    current_user,
    resolve_identity,
    set_session_cookie,
)
from messenger.routers import (
    agent_chats_router,
    agents_catalog_router,
    auth_router,
    automations_router,
    capabilities_router,
    review_router,
    tracking_router,
)
from messenger.scheduler import CloudScheduler, ClassifySweep

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_LEN = 2000
RATE_LIMIT_WINDOW = 10.0  # seconds
RATE_LIMIT_MAX = 20  # messages per window per author
LOGIN_RATE_LIMIT_WINDOW = 900.0  # 15 minutes
LOGIN_RATE_LIMIT_MAX = 10
AUTH_EMAIL_RATE_LIMIT_WINDOW = 900.0  # 15 minutes
AUTH_EMAIL_RATE_LIMIT_MAX = 5


class RateLimiter:
    def __init__(
        self,
        window: float = RATE_LIMIT_WINDOW,
        max_hits: int = RATE_LIMIT_MAX,
    ) -> None:
        self._window = float(window)
        self._max_hits = int(max_hits)
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, [])
        cutoff = now - self._window
        self._hits[key] = [t for t in bucket if t >= cutoff]
        if len(self._hits[key]) >= self._max_hits:
            return False
        self._hits[key].append(now)
        return True


class RoomHub:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, room_id: str) -> None:
        await ws.accept()
        async with self._lock:
            self._clients[ws] = room_id

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(ws, None)

    async def broadcast(self, room_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = [
                ws for ws, client_room in self._clients.items()
                if client_room == room_id
            ]
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


def _public_base_url(request: Request) -> str:
    """Prefer proxy headers so share links stay https on Fly."""
    proto = (
        request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    ).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    host = (host or "").split(",")[0].strip()
    if not host:
        return str(request.base_url).rstrip("/")
    if proto not in {"http", "https"}:
        proto = "https"
    return f"{proto}://{host}".rstrip("/")



def _capture_people_room_message(
    store: MessageStore,
    *,
    room_id: str,
    author: str,
    text: str,
    msg: dict[str, Any],
    identity: dict[str, Any],
) -> None:
    """Mirror one People-room message into the room owner's Tracking ledger.

    Non-blocking: must never raise into the chat send path.
    """
    try:
        from analyst_ledger.messenger_sync import capture_room_message
        from messenger.tenancy import user_context

        owner_user_id = identity.get("user_id")
        room_title = ""
        if room_id != "legacy":
            room = store.room(room_id) or {}
            owner_user_id = room.get("owner_user_id") or owner_user_id
            room_title = str(room.get("title") or "")
        if not owner_user_id:
            return
        with user_context(str(owner_user_id)) as led:
            capture_room_message(
                led,
                room_id,
                author,
                text,
                room_title=room_title,
                messenger_id=msg.get("id"),
            )
    except Exception:  # noqa: BLE001 — capture must never break chat
        pass


def _maybe_dispatch_agent_mention(
    app: FastAPI,
    *,
    room_id: str,
    author: str,
    body: str,
    owner_user_id: Optional[str],
) -> None:
    """In-process agent / @workflow hooks (no HTTP bridge hop)."""
    text = body or ""
    lower = text.lower()
    # Cheap prefilter before spinning a thread. Keep in sync with
    # friend_personalities mentions (role names + legacy @Qwen*).
    mention_hints = (
        "@qwen",
        "@analyst",
        "@bullish",
        "@bull",
        "@contrarian",
        "@synthesizer",
        "@workflow",
    )
    if not any(h in lower for h in mention_hints):
        return
    # Defer to a daemon thread so the request path stays fast.
    import threading

    def work() -> None:
        try:
            from messenger.agent_hooks import handle_room_mention

            handle_room_mention(
                store=app.state.store,
                hub=app.state.hub,
                room_id=room_id,
                author=author,
                body=text,
                owner_user_id=owner_user_id,
                loop=getattr(app.state, "loop", None),
            )
        except Exception:
            pass

    threading.Thread(target=work, name="agent-mention", daemon=True).start()


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager

    store = MessageStore()
    hub = RoomHub()
    limiter = RateLimiter()
    login_limiter = RateLimiter(
        window=LOGIN_RATE_LIMIT_WINDOW, max_hits=LOGIN_RATE_LIMIT_MAX
    )
    auth_email_limiter = RateLimiter(
        window=AUTH_EMAIL_RATE_LIMIT_WINDOW, max_hits=AUTH_EMAIL_RATE_LIMIT_MAX
    )

    from analyst_ledger.workflow_engine import JobManager

    jobs = JobManager()
    live = os.environ.get("MESSENGER_SCHEDULER_LIVE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    scheduler_enabled = os.environ.get("MESSENGER_SCHEDULER", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    scheduler = CloudScheduler(
        list_user_ids=store.list_user_ids,
        interval_seconds=float(os.environ.get("MESSENGER_SCHEDULER_INTERVAL", "30")),
        run_stub=not live,
    )
    classify_enabled = os.environ.get("MESSENGER_CLASSIFY_SWEEP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    classify_sweep = ClassifySweep(
        list_user_ids=store.list_user_ids,
        interval_seconds=float(os.environ.get("MESSENGER_CLASSIFY_INTERVAL", "300")),
        limit_per_user=int(os.environ.get("MESSENGER_CLASSIFY_LIMIT", "20")),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.loop = asyncio.get_running_loop()
        if scheduler_enabled:
            app.state.scheduler.start()
        if classify_enabled:
            app.state.classify_sweep.start()
        yield
        app.state.scheduler.stop()
        app.state.classify_sweep.stop()

    app = FastAPI(
        title="Workflow Messenger",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.hub = hub
    app.state.limiter = limiter
    app.state.login_limiter = login_limiter
    app.state.auth_email_limiter = auth_email_limiter
    app.state.jobs = jobs
    app.state.scheduler = scheduler
    app.state.classify_sweep = classify_sweep

    app.include_router(auth_router)
    app.include_router(tracking_router)
    app.include_router(capabilities_router)
    app.include_router(agents_catalog_router)
    app.include_router(automations_router)
    app.include_router(agent_chats_router)
    app.include_router(review_router)

    # --- Browser extension ingest (same response shape as local dashboard) -------
    from fastapi import Depends as FastAPIDepends
    from messenger.deps import current_user_optional
    from messenger.tenancy import user_context

    @app.post("/api/ingest-browser")
    async def extension_ingest_browser(
        request: Request,
        user: Optional[dict[str, Any]] = FastAPIDepends(current_user_optional),
    ) -> JSONResponse:
        if not user:
            return JSONResponse(
                {
                    "error": (
                        "Not logged in — sign in to Flyleaf (levin.fly.dev) in this "
                        "browser so Capture can post visits, or point the extension "
                        "Endpoint at a signed-in local Workflow."
                    )
                },
                status_code=401,
            )
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        try:
            from analyst_ledger.dashboard import _ingest_browser

            with user_context(user["user_id"]) as ledger:
                event = _ingest_browser(ledger, data if isinstance(data, dict) else {})
            return JSONResponse(event)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/ingest-tv")
    async def extension_ingest_tv(
        request: Request,
        user: Optional[dict[str, Any]] = FastAPIDepends(current_user_optional),
    ) -> JSONResponse:
        if not user:
            return JSONResponse({"error": "Not logged in"}, status_code=401)
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        try:
            from analyst_ledger.dashboard import _ingest_tv

            with user_context(user["user_id"]) as ledger:
                event = _ingest_tv(ledger, data if isinstance(data, dict) else {})
            return JSONResponse(event)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=500)

    # --- Companion (Phase 6 local-safeguard surface) ---------------------------
    from fastapi import Depends as _Depends

    @app.post("/api/companion/link")
    async def companion_link_route(
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.companion import registry

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        base_url = str(body.get("base_url") or "").strip()
        token = str(body.get("token") or "").strip()
        if not base_url:
            return JSONResponse(
                {"ok": False, "error": "base_url_required", "message": "Companion URL is required."},
                status_code=400,
            )
        if not token:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "token_required",
                    "message": "Paste the companion token from the terminal (or companion_token file).",
                },
                status_code=400,
            )
        try:
            entry = registry.register(user["user_id"], base_url, token=token)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        # healthz is open; verify the bearer token against an authenticated route
        check = registry.verify_token(user["user_id"], timeout=3.0)
        if check.get("error") == "invalid_token":
            registry.unlink(user["user_id"])
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_token",
                    "message": "Companion rejected that token. Copy the token from the companion terminal again.",
                },
                status_code=400,
            )
        return JSONResponse(
            {
                "ok": True,
                "reachable": bool(check.get("reachable")),
                "authenticated": bool(check.get("authenticated")),
                "companion": {
                    "user_id": entry["user_id"],
                    "base_url": entry["base_url"],
                    "registered_at": entry["registered_at"],
                },
                "error": None if check.get("ok") else check.get("error"),
                "message": (
                    None
                    if check.get("ok")
                    else (
                        "Saved, but companion is not reachable from this server. "
                        "If you're on the cloud app, use a public tunnel URL to the companion "
                        "(not http://127.0.0.1)."
                        if not check.get("reachable")
                        else "Companion linked but auth check failed."
                    )
                ),
            }
        )

    @app.get("/api/companion/status")
    def companion_status(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.companion import registry

        return JSONResponse(registry.probe(user["user_id"]))

    @app.delete("/api/companion/link")
    def companion_unlink(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.companion import registry

        existed = registry.unlink(user["user_id"])
        return JSONResponse({"ok": True, "unlinked": existed})

    # --- Settings → Models (multi-profile) ------------------------------------

    @app.get("/api/settings/models")
    def settings_models_list(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models
        from messenger.model_link import registry as model_registry

        payload = model_registry().list_profiles(user["user_id"])
        payload["profiles"] = settings_models.annotate_open_source_reachability(
            user["user_id"], payload.get("profiles") or []
        )
        payload["companion"] = settings_models.companion_health(user["user_id"])
        active = model_registry().active_profile(user["user_id"])
        if active:
            public_active = model_registry().public_profile(active)
            # Mirror reachability onto the active summary when it's the enabled local.
            for row in payload["profiles"]:
                if row.get("id") == public_active.get("id"):
                    public_active = {**public_active, "reachable": row.get("reachable")}
                    break
            payload["active"] = public_active
        else:
            payload["active"] = None
        return JSONResponse(payload)

    @app.post("/api/settings/models")
    async def settings_models_add_frontier(
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        try:
            profile = model_registry().add_frontier(
                user["user_id"],
                provider=str(body.get("provider") or "anthropic"),
                api_key=str(body.get("api_key") or ""),
                model=str(body.get("model") or ""),
                base_url=str(body.get("base_url") or ""),
                label=str(body.get("label") or ""),
                activate=bool(body.get("activate", True)),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "profile": profile})

    @app.post("/api/settings/models/open-source/draft")
    async def settings_models_os_draft(
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        try:
            profile = model_registry().add_open_source_draft(
                user["user_id"],
                candidate_id=str(body.get("candidate_id") or body.get("id") or ""),
                runtime=str(body.get("runtime") or "ollama"),
                model=str(body.get("model") or body.get("label") or ""),
                label=str(body.get("label") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "profile": profile})

    @app.patch("/api/settings/models/{profile_id}")
    async def settings_models_patch(
        profile_id: str,
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        try:
            profile = model_registry().update_profile(
                user["user_id"],
                profile_id,
                label=body.get("label"),
                model=body.get("model"),
                api_key=body.get("api_key"),
                base_url=body.get("base_url"),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "profile": profile})

    @app.delete("/api/settings/models/{profile_id}")
    def settings_models_delete(
        profile_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        existed = model_registry().delete_profile(user["user_id"], profile_id)
        return JSONResponse({"ok": True, "deleted": existed})

    @app.post("/api/settings/models/{profile_id}/activate")
    def settings_models_activate(
        profile_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        try:
            profile = model_registry().activate(user["user_id"], profile_id)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "profile": profile})

    @app.post("/api/settings/models/{profile_id}/establish")
    def settings_models_establish(
        profile_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models

        result = settings_models.establish_pipeline(user["user_id"], profile_id)
        code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=code)

    @app.post("/api/settings/models/{profile_id}/rebuild")
    def settings_models_rebuild(
        profile_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models

        result = settings_models.rebuild_pipeline(user["user_id"], profile_id)
        code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=code)

    @app.post("/api/settings/models/{profile_id}/enable")
    def settings_models_enable(
        profile_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models

        result = settings_models.enable_profile(user["user_id"], profile_id)
        code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=code)

    @app.post("/api/settings/models/{profile_id}/disable")
    def settings_models_disable(
        profile_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models

        result = settings_models.disable_profile(user["user_id"], profile_id)
        code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=code)

    @app.get("/api/settings/models/status")
    def settings_models_status(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        return JSONResponse(model_registry().probe(user["user_id"]))

    @app.post("/api/settings/local-model/discover")
    def settings_local_discover(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models

        result = settings_models.discover(user["user_id"])
        code = 200 if result.get("ok") is not False else 400
        if result.get("error") in {"needs_companion", "companion_unreachable"}:
            code = 400
        return JSONResponse(result, status_code=code)

    @app.post("/api/settings/local-model/pull")
    async def settings_local_pull(
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models

        try:
            body = await request.json()
        except Exception:
            body = {}
        result = settings_models.pull_model(
            user["user_id"], str((body or {}).get("model") or "")
        )
        code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=code)

    @app.get("/api/settings/local-model/pull/{job_id}")
    def settings_local_pull_status(
        job_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger import settings_models

        result = settings_models.pull_job_status(user["user_id"], job_id)
        code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=code)

    # --- Legacy per-user model aliases (/api/model/*) -------------------------

    @app.post("/api/model/link")
    async def model_link_route(
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        try:
            entry = model_registry().register(
                user["user_id"],
                provider=str(body.get("provider") or "ollama"),
                base_url=str(body.get("base_url") or ""),
                api_key=str(body.get("api_key") or body.get("token") or ""),
                model=str(body.get("model") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "model_link": entry})

    @app.get("/api/model/providers")
    def model_providers(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import providers_public

        return JSONResponse({"ok": True, "providers": providers_public()})

    @app.get("/api/model/status")
    def model_status(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        return JSONResponse(model_registry().probe(user["user_id"]))

    @app.delete("/api/model/link")
    def model_unlink(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.model_link import registry as model_registry

        existed = model_registry().unlink(user["user_id"])
        return JSONResponse({"ok": True, "unlinked": existed})

    # --- Health / bootstrap ---------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/me")
    def me_compat(request: Request) -> JSONResponse:
        """Backward-compatible /api/me → prefer account, fall back to invite session."""
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        user_id = (identity or {}).get("user_id") if identity else None
        user = store.user_by_id(user_id) if user_id else None
        if user and identity:
            room_id = identity["room_id"]
            room = store.room(room_id) if room_id != "legacy" else None
            return JSONResponse(
                {
                    "ok": True,
                    "authenticated": True,
                    "user_id": user["user_id"],
                    "email": user["email"],
                    "name": identity["name"],
                    "display_name": user["display_name"],
                    "room_id": room_id,
                    "room_title": (room or {}).get("title") or "Private room",
                }
            )
        if identity:
            room_id = identity["room_id"]
            room = store.room(room_id) if room_id != "legacy" else None
            return JSONResponse(
                {
                    "ok": True,
                    "authenticated": False,
                    "legacy": True,
                    "name": identity["name"],
                    "room_id": room_id,
                    "room_title": (room or {}).get("title") or "Private room",
                }
            )
        return JSONResponse({"ok": False, "name": None}, status_code=401)

    # --- People rooms ---------------------------------------------------------

    @app.get("/api/rooms/mine")
    def rooms_mine(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        rooms = store.list_rooms_for_user(user["user_id"])
        from messenger.model_link import registry as model_registry

        models = model_registry()
        for room in rooms:
            owner_id = str(room.get("owner_user_id") or user["user_id"])
            config = room.get("config") or {}
            override_id = config.get("model_profile_id")
            active = (
                models.get_profile(owner_id, str(override_id))
                if override_id
                else models.active_profile(owner_id)
            )
            public = models.public_profile(active) if active else None
            room["compute"] = (
                {
                    "label": public.get("label") or public.get("model"),
                    "provider": public.get("provider_label") or public.get("provider"),
                    "model": public.get("model"),
                    "local": bool(public.get("is_local")),
                    "profile_id": public.get("id"),
                    "room_override": bool(override_id),
                }
                if public
                else None
            )
            room["model_profile_id"] = (
                str(override_id) if override_id else (public.get("id") if public else None)
            )
        return JSONResponse({"ok": True, "rooms": rooms})

    @app.get("/api/specialists")
    def specialists_list() -> JSONResponse:
        from messenger.specialist_room import list_specialists

        return JSONResponse({"ok": True, "specialists": list_specialists()})

    def _editable_room(room_id: str, user_id: str) -> tuple[Optional[dict[str, Any]], Optional[JSONResponse]]:
        room = store.room(room_id)
        if not room:
            return None, JSONResponse(
                {"ok": False, "error": "not_found"}, status_code=404
            )
        if room.get("owner_user_id") != user_id:
            return None, JSONResponse(
                {"ok": False, "error": "owner_required"}, status_code=403
            )
        return room, None

    @app.post("/api/rooms/{room_id}/invite")
    def room_invite(
        room_id: str,
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        _, error = _editable_room(room_id, user["user_id"])
        if error:
            return error
        room_invite = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(room_invite.encode("utf-8")).hexdigest()
        store.update_room_token(room_id, token_hash)
        return JSONResponse(
            {
                "ok": True,
                "room_id": room_id,
                "share_url": (
                    _public_base_url(request)
                    + f"/?room={room_id}&invite={room_invite}"
                ),
            }
        )

    @app.post("/api/rooms/{room_id}/agents")
    async def room_agent_add(
        room_id: str,
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        room, error = _editable_room(room_id, user["user_id"])
        if error:
            return error
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        agent_id = str((body or {}).get("agent_id") or "").strip().lower()
        from analyst_ledger.friend_personalities import PERSONALITIES_BY_ID

        if agent_id not in PERSONALITIES_BY_ID:
            return JSONResponse(
                {"ok": False, "error": "unknown_agent"}, status_code=400
            )
        config = dict(room.get("config") or {})
        agents = [
            str(value)
            for value in (config.get("agents") or config.get("specialists") or [])
            if str(value) in PERSONALITIES_BY_ID
        ]
        if agent_id not in agents:
            agents.append(agent_id)
        config["agents"] = agents
        # Keep older specialist-room orchestration compatible with the room roster.
        config["specialists"] = agents
        updated = store.update_room_config(room_id, config)
        return JSONResponse(
            {"ok": True, "room": updated, "agents": agents}
        )

    @app.delete("/api/rooms/{room_id}/agents/{agent_id}")
    def room_agent_remove(
        room_id: str,
        agent_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        room, error = _editable_room(room_id, user["user_id"])
        if error:
            return error
        config = dict(room.get("config") or {})
        agents = [
            str(value)
            for value in (config.get("agents") or config.get("specialists") or [])
            if str(value) != agent_id
        ]
        config["agents"] = agents
        config["specialists"] = agents
        updated = store.update_room_config(room_id, config)
        return JSONResponse(
            {"ok": True, "room": updated, "agents": agents}
        )

    @app.delete("/api/rooms/{room_id}")
    async def delete_room(
        room_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        """Owner-only: permanently delete a room (messages, members, config)."""
        if room_id == "legacy":
            return JSONResponse(
                {"ok": False, "error": "cannot_delete_legacy"},
                status_code=400,
            )
        _, error = _editable_room(room_id, user["user_id"])
        if error:
            return error
        try:
            from messenger.specialist_room import job_registry

            job_registry().stop_room(room_id)
        except Exception:
            pass
        await hub.broadcast(
            room_id,
            {
                "type": "room_deleted",
                "room_id": room_id,
                "by": user.get("name") or user.get("display_name"),
            },
        )
        if not store.delete_room(room_id):
            return JSONResponse(
                {"ok": False, "error": "not_found"}, status_code=404
            )
        return JSONResponse({"ok": True, "room_id": room_id})

    @app.post("/api/rooms/{room_id}/model")
    async def room_set_model(
        room_id: str,
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        """Set which Settings model this room uses (null = account default)."""
        room, error = _editable_room(room_id, user["user_id"])
        if error:
            return error
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        raw = (body or {}).get("profile_id")
        profile_id = str(raw).strip() if raw not in (None, "") else None
        from messenger.model_link import registry as model_registry

        models = model_registry()
        if profile_id is not None:
            profile = models.get_profile(user["user_id"], profile_id)
            if not profile:
                return JSONResponse(
                    {"ok": False, "error": "unknown_profile"}, status_code=400
                )
        config = dict(room.get("config") or {})
        if profile_id:
            config["model_profile_id"] = profile_id
        else:
            config.pop("model_profile_id", None)
        updated = store.update_room_config(room_id, config)
        public = None
        active = (
            models.get_profile(user["user_id"], profile_id)
            if profile_id
            else models.active_profile(user["user_id"])
        )
        if active:
            public = models.public_profile(active)
        return JSONResponse(
            {
                "ok": True,
                "room": updated,
                "model_profile_id": profile_id,
                "compute": (
                    {
                        "label": public.get("label") or public.get("model"),
                        "provider": public.get("provider_label")
                        or public.get("provider"),
                        "model": public.get("model"),
                        "local": bool(public.get("is_local")),
                        "profile_id": public.get("id"),
                        "room_override": bool(profile_id),
                    }
                    if public
                    else None
                ),
            }
        )

    @app.post("/api/rooms/{room_id}/specialist-run")
    async def specialist_run(
        room_id: str,
        request: Request,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        room = store.room(room_id)
        if not room:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if not store.user_in_room(room_id, user["user_id"]):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        config = room.get("config") if isinstance(room.get("config"), dict) else {}
        room_agents = config.get("agents") or config.get("specialists") or []
        if str(room.get("kind") or "people") != "specialist" and not room_agents:
            return JSONResponse(
                {"ok": False, "error": "room_has_no_agents"}, status_code=400
            )
        action = str(body.get("action") or "").strip().lower()
        topic = str(body.get("topic") or "")
        stub = bool(body.get("stub", False))
        continuous = bool(body.get("continuous") or body.get("loop"))
        try:
            rounds = int(body.get("rounds") or 1)
        except (TypeError, ValueError):
            rounds = 1
        rounds = max(1, min(rounds, 5))

        from messenger.specialist_room import job_registry, start_specialist_job

        existing = job_registry().active_for_room(room_id)
        if existing:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "already_running",
                    "message": "A specialist run is already active in this room. Stop it first.",
                    "job": existing.public(),
                },
                status_code=409,
            )

        owner_id = room.get("owner_user_id") or user["user_id"]
        try:
            job = start_specialist_job(
                store=store,
                hub=hub,
                room=room,
                action=action,
                topic=topic,
                owner_user_id=owner_id,
                stub=stub,
                rounds=rounds,
                continuous=continuous,
                loop=getattr(app.state, "loop", None),
            )
        except ValueError as exc:
            return JSONResponse(
                {"ok": False, "error": "bad_action", "message": str(exc)},
                status_code=400,
            )
        return JSONResponse(
            {
                "ok": True,
                "started": True,
                "action": action,
                "topic": topic,
                "stub": stub,
                "rounds": rounds,
                "continuous": continuous,
                "job": job.public(),
                "message": (
                    "Specialists are running in the background — you can leave this room."
                ),
            }
        )

    @app.post("/api/rooms/{room_id}/specialist-stop")
    def specialist_stop(
        room_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        room = store.room(room_id)
        if not room:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if not store.user_in_room(room_id, user["user_id"]):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        from messenger.specialist_room import job_registry

        job = job_registry().stop_room(room_id)
        if not job:
            return JSONResponse(
                {"ok": True, "stopped": False, "message": "No active run in this room."}
            )
        return JSONResponse(
            {
                "ok": True,
                "stopped": True,
                "job": job.public(),
                "message": "Stop requested — specialists will finish the current turn.",
            }
        )

    @app.get("/api/rooms/{room_id}/specialist-status")
    def specialist_status(
        room_id: str,
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        if not store.user_in_room(room_id, user["user_id"]):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        from messenger.specialist_room import job_registry

        job = job_registry().active_for_room(room_id)
        latest = job or job_registry().latest_for_room(room_id)
        return JSONResponse(
            {
                "ok": True,
                "running": job is not None,
                "job": latest.public() if latest else None,
            }
        )

    @app.get("/api/specialist-jobs")
    def specialist_jobs(
        user: dict[str, Any] = _Depends(current_user),
    ) -> JSONResponse:
        from messenger.specialist_room import job_registry

        # Only surface jobs for rooms the user is in.
        active = []
        for job in job_registry().list_active():
            if store.user_in_room(job["room_id"], user["user_id"]):
                active.append(job)
        return JSONResponse({"ok": True, "jobs": active})

    @app.post("/api/rooms")
    async def create_room(request: Request) -> JSONResponse:
        """Authenticated users create owner-scoped rooms; legacy invite path still works."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        user_id = (identity or {}).get("user_id") if identity else None
        rate_key = (
            (request.client.host if request.client else "unknown")
            + ":"
            + str((identity or {}).get("name") or body.get("name") or "anon")
        )
        if not limiter.allow(f"create:{rate_key}"):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        name = normalize_name(str(body.get("name") or "")) or (
            identity["name"] if identity else None
        )
        title = normalize_title(str(body.get("title") or ""))
        if not name:
            return JSONResponse({"ok": False, "error": "bad_name"}, status_code=400)
        if not title:
            return JSONResponse({"ok": False, "error": "bad_title"}, status_code=400)
        kind = str(body.get("kind") or "people").strip().lower() or "people"
        if kind not in {"people", "specialist"}:
            return JSONResponse({"ok": False, "error": "bad_kind"}, status_code=400)
        specialists = body.get("specialists")
        config: dict[str, Any] = {}
        if kind == "specialist":
            from analyst_ledger.friend_personalities import resolve_specialists

            ids = specialists if isinstance(specialists, list) else None
            roster = resolve_specialists(ids)
            if len(roster) < 2:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "specialist_rooms_need_two_plus",
                    },
                    status_code=400,
                )
            config = {
                "specialists": [p.id for p in roster],
                "modes": ["present", "debate", "idea"],
            }
        room_id = secrets.token_urlsafe(9)
        room_invite = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(room_invite.encode("utf-8")).hexdigest()
        room = store.create_room(
            room_id,
            title,
            token_hash,
            owner_user_id=user_id,
            kind=kind,
            config=config,
        )
        share_url = _public_base_url(request) + "/?" + (
            f"room={room_id}&invite={room_invite}"
        )
        if kind == "specialist":
            # Seed the room so the user sees the roster immediately.
            from analyst_ledger.friend_personalities import resolve_specialists

            roster = resolve_specialists(config.get("specialists"))
            store.add_message(
                author="Moderator",
                body=(
                    "Specialist workshop ready.\n"
                    f"Roster: {', '.join(p.mention + ' (' + p.role + ')' for p in roster)}\n"
                    "Use Present (recent work), Debate (two sides → ideas), "
                    "or mention specialists inline."
                ),
                room_id=room_id,
            )
        resp = JSONResponse(
            {
                "ok": True,
                "name": name,
                "room_id": room_id,
                "room_title": title,
                "share_url": share_url,
                "created_at": room["created_at"],
                "owner_user_id": user_id,
                "kind": kind,
                "config": config,
            }
        )
        set_session_cookie(
            resp,
            store=store,
            name=name,
            room_id=room_id,
            can_create=True,
            user_id=user_id,
            email=(identity or {}).get("email") if identity else None,
            revoke_sid=(identity or {}).get("sid") if identity else None,
        )
        return resp

    @app.post("/api/join")
    async def join(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        invite = str(body.get("invite") or "")
        room_id = str(body.get("room_id") or "legacy").strip()
        name = normalize_name(str(body.get("name") or ""))
        existing = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        user_id = (existing or {}).get("user_id")
        email = (existing or {}).get("email")
        # Prefer account display name when logged in.
        if user_id and not name:
            user = store.user_by_id(user_id)
            if user:
                name = user["display_name"]
                email = user["email"]
        if room_id == "legacy":
            valid_invite = invite_ok(invite)
            room_title = "Private room"
            can_create = valid_invite
        else:
            token_hash = hashlib.sha256(invite.encode("utf-8")).hexdigest()
            room = store.room(room_id)
            valid_invite = store.room_token_ok(room_id, token_hash) or invite_ok(invite)
            room_title = str((room or {}).get("title") or "Private room")
            can_create = invite_ok(invite) or bool(user_id)
        if not valid_invite:
            return JSONResponse({"ok": False, "error": "bad_invite"}, status_code=403)
        if not name:
            return JSONResponse({"ok": False, "error": "bad_name"}, status_code=400)
        if user_id and room_id != "legacy":
            store.add_room_member(room_id, user_id)
        resp = JSONResponse(
            {
                "ok": True,
                "name": name,
                "room_id": room_id,
                "room_title": room_title,
                "user_id": user_id,
            }
        )
        set_session_cookie(
            resp,
            store=store,
            name=name,
            room_id=room_id,
            can_create=can_create,
            user_id=user_id,
            email=email,
            revoke_sid=(existing or {}).get("sid") if existing else None,
        )
        return resp

    @app.post("/api/rooms/select")
    async def select_room(request: Request) -> JSONResponse:
        """Switch the active People room for an authenticated member."""
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        if not identity or not identity.get("user_id"):
            return JSONResponse({"ok": False, "error": "account_required"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        room_id = str(body.get("room_id") or "").strip()
        user_id = identity["user_id"]
        if room_id == "legacy":
            room_title = "Private room"
        else:
            room = store.room(room_id)
            if not room:
                return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
            if not store.user_in_room(room_id, user_id) and not invite_ok(
                str(body.get("invite") or "")
            ):
                return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
            room_title = str(room.get("title") or "Private room")
        resp = JSONResponse(
            {
                "ok": True,
                "room_id": room_id,
                "room_title": room_title,
                "name": identity["name"],
            }
        )
        set_session_cookie(
            resp,
            store=store,
            name=identity["name"],
            room_id=room_id,
            can_create=True,
            user_id=user_id,
            email=identity.get("email"),
            revoke_sid=identity.get("sid"),
        )
        return resp

    @app.get("/api/rooms/list")
    def rooms_list(request: Request) -> JSONResponse:
        """List all rooms. Gated by the server invite (bot/admin only)."""
        key = (
            request.headers.get("x-server-invite")
            or request.query_params.get("key")
            or ""
        )
        if not invite_ok(key):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        rooms = [
            {"room_id": "legacy", "title": "Friend room", "created_at": None}
        ] + store.list_rooms()
        return JSONResponse({"ok": True, "rooms": rooms})

    @app.post("/api/logout")
    def logout(request: Request) -> JSONResponse:
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        if not identity:
            return JSONResponse(
                {"ok": False, "error": "unauthorized"}, status_code=401
            )
        sid = (identity.get("sid") or "").strip()
        if sid:
            store.delete_session(sid)
        resp = JSONResponse({"ok": True})
        clear_session_cookie(resp)
        return resp

    def _create_message(
        name: str, room_id: str, body: str
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        body = (body or "").strip()
        if not body:
            return None, "empty"
        if len(body) > MAX_BODY_LEN:
            return None, "too_long"
        if not limiter.allow(f"{room_id}:{name}"):
            return None, "rate_limited"
        return store.add_message(author=name, body=body, room_id=room_id), None

    @app.get("/api/messages")
    def messages(request: Request, limit: int = 200) -> JSONResponse:
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        if not identity:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(
            {
                "ok": True,
                "me": identity["name"],
                "room_id": identity["room_id"],
                "messages": store.list_messages(
                    limit=limit, room_id=identity["room_id"]
                ),
            }
        )

    @app.post("/api/messages")
    async def post_message(request: Request) -> JSONResponse:
        """HTTP send path for non-WS clients + agent hooks."""
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        if not identity:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        text = str(body.get("body") or body.get("content") or "")
        name = identity["name"]
        room_id = identity["room_id"]
        msg, err = _create_message(name, room_id, text)
        if err == "empty":
            return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
        if err == "too_long":
            return JSONResponse({"ok": False, "error": "too_long"}, status_code=400)
        if err == "rate_limited":
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        assert msg is not None
        await hub.broadcast(room_id, {"type": "message", "message": msg})
        owner_user_id = None
        if room_id != "legacy":
            room = store.room(room_id)
            owner_user_id = (room or {}).get("owner_user_id") or identity.get("user_id")
        else:
            owner_user_id = identity.get("user_id")
        _capture_people_room_message(
            store,
            room_id=room_id,
            author=name,
            text=text,
            msg=msg,
            identity=identity,
        )
        _maybe_dispatch_agent_mention(
            app,
            room_id=room_id,
            author=name,
            body=text,
            owner_user_id=owner_user_id,
        )
        return JSONResponse({"ok": True, "message": msg, "me": name})

    async def _clear_chat(name: str, room_id: str) -> dict[str, Any]:
        deleted = store.clear_messages(room_id=room_id)
        payload = {"type": "cleared", "by": name, "deleted": deleted}
        await hub.broadcast(room_id, payload)
        return {"ok": True, "deleted": deleted, "me": name}

    @app.delete("/api/messages")
    async def delete_messages(request: Request) -> JSONResponse:
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        if not identity:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(await _clear_chat(identity["name"], identity["room_id"]))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        identity = resolve_identity(websocket.cookies.get(COOKIE_NAME), store)
        if not identity:
            await websocket.close(code=4401)
            return
        name = identity["name"]
        room_id = identity["room_id"]
        await hub.connect(websocket, room_id)
        try:
            await websocket.send_json(
                {
                    "type": "history",
                    "messages": store.list_messages(limit=200, room_id=room_id),
                }
            )
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                msg_type = data.get("type")
                if msg_type == "clear":
                    await _clear_chat(name, room_id)
                    continue
                if msg_type != "message":
                    continue
                text = str(data.get("body") or "")
                msg, err = _create_message(name, room_id, text)
                if err == "empty":
                    continue
                if err:
                    await websocket.send_json({"type": "error", "error": err})
                    continue
                assert msg is not None
                await hub.broadcast(room_id, {"type": "message", "message": msg})
                owner_user_id = identity.get("user_id")
                if room_id != "legacy":
                    room = store.room(room_id)
                    owner_user_id = (room or {}).get("owner_user_id") or owner_user_id
                _capture_people_room_message(
                    store,
                    room_id=room_id,
                    author=name,
                    text=text,
                    msg=msg,
                    identity=identity,
                )
                _maybe_dispatch_agent_mention(
                    app,
                    room_id=room_id,
                    author=name,
                    body=text,
                    owner_user_id=owner_user_id,
                )
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await hub.disconnect(websocket)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"])
    @app.api_route("/favicon.svg", methods=["GET", "HEAD"])
    def favicon() -> FileResponse:
        svg = STATIC_DIR / "favicon.svg"
        ico = STATIC_DIR / "favicon.ico"
        path = ico if ico.is_file() else svg
        return FileResponse(
            path,
            media_type="image/svg+xml" if path.suffix == ".svg" else "image/x-icon",
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
