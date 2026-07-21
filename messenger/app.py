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
    read_identity,
)
from messenger.db import MessageStore
from messenger.deps import (
    clear_session_cookie,
    current_user,
    set_session_cookie,
)
from messenger.routers import (
    agent_chats_router,
    auth_router,
    automations_router,
    review_router,
    tracking_router,
)
from messenger.scheduler import CloudScheduler

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_LEN = 2000
RATE_LIMIT_WINDOW = 10.0  # seconds
RATE_LIMIT_MAX = 20  # messages per window per author


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, [])
        cutoff = now - RATE_LIMIT_WINDOW
        self._hits[key] = [t for t in bucket if t >= cutoff]
        if len(self._hits[key]) >= RATE_LIMIT_MAX:
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


def _maybe_dispatch_agent_mention(
    app: FastAPI,
    *,
    room_id: str,
    author: str,
    body: str,
    owner_user_id: Optional[str],
) -> None:
    """In-process @Qwen / @workflow hooks (no HTTP bridge hop)."""
    text = body or ""
    lower = text.lower()
    if "@qwen" not in lower and "@workflow" not in lower:
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.loop = asyncio.get_running_loop()
        if scheduler_enabled:
            app.state.scheduler.start()
        yield
        app.state.scheduler.stop()

    app = FastAPI(
        title="Workflow Messenger",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.hub = hub
    app.state.limiter = limiter
    app.state.jobs = jobs
    app.state.scheduler = scheduler

    app.include_router(auth_router)
    app.include_router(tracking_router)
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
                        "Not logged in — sign in to Workflow first, or point the "
                        "extension at http://127.0.0.1:8788/api/ingest-browser "
                        "with analyst dashboard running."
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
        try:
            entry = registry.register(
                user["user_id"],
                str(body.get("base_url") or ""),
                token=str(body.get("token") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "companion": {
                    "user_id": entry["user_id"],
                    "base_url": entry["base_url"],
                    "registered_at": entry["registered_at"],
                },
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

    # --- Per-user local model (each person tunnels their own Ollama) -----------

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
        identity = read_identity(request.cookies.get(COOKIE_NAME))
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
        return JSONResponse({"ok": True, "rooms": rooms})

    @app.get("/api/specialists")
    def specialists_list() -> JSONResponse:
        from messenger.specialist_room import list_specialists

        return JSONResponse({"ok": True, "specialists": list_specialists()})

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
        if str(room.get("kind") or "people") != "specialist":
            return JSONResponse(
                {"ok": False, "error": "not_a_specialist_room"}, status_code=400
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
        return JSONResponse(
            {
                "ok": True,
                "running": job is not None,
                "job": job.public() if job else None,
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
        identity = read_identity(request.cookies.get(COOKIE_NAME))
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
        title = " ".join(str(body.get("title") or "").strip().split())[:80]
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
            name=name,
            room_id=room_id,
            can_create=True,
            user_id=user_id,
            email=(identity or {}).get("email") if identity else None,
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
        existing = read_identity(request.cookies.get(COOKIE_NAME))
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
            name=name,
            room_id=room_id,
            can_create=can_create,
            user_id=user_id,
            email=email,
        )
        return resp

    @app.post("/api/rooms/select")
    async def select_room(request: Request) -> JSONResponse:
        """Switch the active People room for an authenticated member."""
        identity = read_identity(request.cookies.get(COOKIE_NAME))
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
            name=identity["name"],
            room_id=room_id,
            can_create=True,
            user_id=user_id,
            email=identity.get("email"),
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
    def logout() -> JSONResponse:
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
        identity = read_identity(request.cookies.get(COOKIE_NAME))
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
        identity = read_identity(request.cookies.get(COOKIE_NAME))
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
        identity = read_identity(request.cookies.get(COOKIE_NAME))
        if not identity:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(await _clear_chat(identity["name"], identity["room_id"]))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        identity = read_identity(websocket.cookies.get(COOKIE_NAME))
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
            },
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
