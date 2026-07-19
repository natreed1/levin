"""Cloud messenger FastAPI app — invite-gated multi-room chat."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from messenger.auth import (
    COOKIE_NAME,
    invite_ok,
    mint_session,
    normalize_name,
    read_identity,
)
from messenger.db import MessageStore

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


def create_app() -> FastAPI:
    store = MessageStore()
    hub = RoomHub()
    limiter = RateLimiter()
    app = FastAPI(title="Messenger", docs_url=None, redoc_url=None)

    def _identity(request: Request) -> Optional[dict[str, str]]:
        return read_identity(request.cookies.get(COOKIE_NAME))

    def _set_session_cookie(response: Response, name: str, room_id: str) -> None:
        secure = os.environ.get("MESSENGER_COOKIE_SECURE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        # On Fly / HTTPS, prefer Secure cookies by default when not overridden.
        if not os.environ.get("MESSENGER_COOKIE_SECURE"):
            secure = bool(os.environ.get("FLY_APP_NAME"))
        response.set_cookie(
            key=COOKIE_NAME,
            value=mint_session(name, room_id),
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=60 * 60 * 24 * 30,
            path="/",
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/me")
    def me(request: Request) -> JSONResponse:
        identity = _identity(request)
        if not identity:
            return JSONResponse({"ok": False, "name": None}, status_code=401)
        room_id = identity["room_id"]
        room = store.room(room_id) if room_id != "legacy" else None
        return JSONResponse(
            {
                "ok": True,
                "name": identity["name"],
                "room_id": room_id,
                "room_title": (room or {}).get("title") or "Private room",
            }
        )

    @app.post("/api/rooms")
    async def create_room(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        if not invite_ok(str(body.get("creator_invite") or "")):
            return JSONResponse({"ok": False, "error": "bad_creator_invite"}, status_code=403)
        name = normalize_name(str(body.get("name") or ""))
        title = " ".join(str(body.get("title") or "").strip().split())[:80]
        if not name:
            return JSONResponse({"ok": False, "error": "bad_name"}, status_code=400)
        if not title:
            return JSONResponse({"ok": False, "error": "bad_title"}, status_code=400)
        room_id = secrets.token_urlsafe(9)
        room_invite = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(room_invite.encode("utf-8")).hexdigest()
        room = store.create_room(room_id, title, token_hash)
        share_url = str(request.base_url).rstrip("/") + "/?" + (
            f"room={room_id}&invite={room_invite}"
        )
        resp = JSONResponse(
            {
                "ok": True,
                "name": name,
                "room_id": room_id,
                "room_title": title,
                "share_url": share_url,
                "created_at": room["created_at"],
            }
        )
        _set_session_cookie(resp, name, room_id)
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
        if room_id == "legacy":
            valid_invite = invite_ok(invite)
            room_title = "Private room"
        else:
            token_hash = hashlib.sha256(invite.encode("utf-8")).hexdigest()
            room = store.room(room_id)
            valid_invite = store.room_token_ok(room_id, token_hash)
            room_title = str((room or {}).get("title") or "Private room")
        if not valid_invite:
            return JSONResponse({"ok": False, "error": "bad_invite"}, status_code=403)
        if not name:
            return JSONResponse({"ok": False, "error": "bad_name"}, status_code=400)
        resp = JSONResponse(
            {"ok": True, "name": name, "room_id": room_id, "room_title": room_title}
        )
        _set_session_cookie(resp, name, room_id)
        return resp

    @app.post("/api/logout")
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
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
        identity = _identity(request)
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
        """HTTP send path for ledger bridge / non-WS clients."""
        identity = _identity(request)
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
        return JSONResponse({"ok": True, "message": msg, "me": name})

    async def _clear_chat(name: str, room_id: str) -> dict[str, Any]:
        deleted = store.clear_messages(room_id=room_id)
        payload = {"type": "cleared", "by": name, "deleted": deleted}
        await hub.broadcast(room_id, payload)
        return {"ok": True, "deleted": deleted, "me": name}

    @app.delete("/api/messages")
    async def delete_messages(request: Request) -> JSONResponse:
        """Clear the whole room history for every participant."""
        identity = _identity(request)
        if not identity:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(
            await _clear_chat(identity["name"], identity["room_id"])
        )

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
            # Send recent history on connect.
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
                msg, err = _create_message(
                    name, room_id, str(data.get("body") or "")
                )
                if err == "empty":
                    continue
                if err:
                    await websocket.send_json({"type": "error", "error": err})
                    continue
                assert msg is not None
                await hub.broadcast(room_id, {"type": "message", "message": msg})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await hub.disconnect(websocket)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
