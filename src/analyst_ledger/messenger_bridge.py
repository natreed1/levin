"""Server-side bridge from the ledger Chats UI to the cloud messenger.

Friend DMs stay on the messenger host (SQLite there). The ledger only proxies
list/send so Friend appears as another thread beside agent chats.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from .paths import data_dir

FRIEND_THREAD_ID = "friend"


class MessengerBridgeError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def messenger_configured() -> bool:
    return bool(messenger_url() and messenger_invite())


def messenger_url() -> str:
    return (os.environ.get("ANALYST_MESSENGER_URL") or "").strip().rstrip("/")


def messenger_invite() -> str:
    return (os.environ.get("ANALYST_MESSENGER_INVITE") or "").strip()


def messenger_display_name() -> str:
    name = (os.environ.get("ANALYST_MESSENGER_NAME") or "").strip()
    return name or "You"


def friend_thread_meta() -> Dict[str, Any]:
    return {
        "session_id": FRIEND_THREAD_ID,
        "title": "Friend",
        "desk_tag": "chat:friend",
        "ritual_id": None,
        "master": False,
        "friend": True,
        "started_at": None,
    }


def _cookie_path(cookie_key: str = "user") -> Path:
    safe = "".join(c for c in cookie_key if c.isalnum() or c in "-_") or "user"
    path = data_dir() / f"messenger_bridge_cookies_{safe}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Migrate legacy single-jar path once.
        legacy = data_dir() / "messenger_bridge_cookies.txt"
        if safe == "user" and legacy.exists():
            try:
                path.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        else:
            path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    return path


def _opener_for(cookie_key: str = "user") -> urllib.request.OpenerDirector:
    jar = MozillaCookieJar(str(_cookie_path(cookie_key)))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener._messenger_jar = jar  # type: ignore[attr-defined]
    return opener


def _opener() -> urllib.request.OpenerDirector:
    return _opener_for("user")


def _save_jar(opener: urllib.request.OpenerDirector) -> None:
    jar = getattr(opener, "_messenger_jar", None)
    if jar is not None:
        jar.save(ignore_discard=True, ignore_expires=True)


def _request(
    method: str,
    path: str,
    *,
    payload: Optional[dict] = None,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Dict[str, Any]:
    base = messenger_url()
    if not base:
        raise MessengerBridgeError(
            "Friend chat is not configured. Set ANALYST_MESSENGER_URL.",
            status=503,
        )
    own = opener is None
    opener = opener or _opener()
    url = urljoin(base + "/", path.lstrip("/"))
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        err = body.get("error") or exc.reason or str(exc)
        raise MessengerBridgeError(f"Messenger error: {err}", status=int(exc.code)) from exc
    except urllib.error.URLError as exc:
        raise MessengerBridgeError(
            f"Cannot reach messenger at {base}: {exc.reason}", status=503
        ) from exc
    finally:
        if own:
            _save_jar(opener)

    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise MessengerBridgeError("Messenger returned invalid JSON", status=502) from exc
    if not isinstance(body, dict):
        raise MessengerBridgeError("Messenger returned unexpected payload", status=502)
    body.setdefault("_http_status", status)
    return body


def ensure_session_as(
    name: str,
    *,
    cookie_key: str = "user",
    room_id: str = "legacy",
) -> str:
    """Join (or re-join) a cloud room as ``name`` using a dedicated cookie jar.

    The server invite grants the bot access to *any* room, so pass ``room_id``
    to watch created rooms as well as the legacy Friend thread.
    """
    invite = messenger_invite()
    if not invite:
        raise MessengerBridgeError(
            "Friend chat is not configured. Set ANALYST_MESSENGER_INVITE "
            "(same value as MESSENGER_INVITE_TOKEN on Fly).",
            status=503,
        )
    display = (name or "").strip() or messenger_display_name()
    opener = _opener_for(cookie_key)
    # /api/me returns 401 when logged out — that is normal, not a hard failure.
    try:
        me = _request("GET", "/api/me", opener=opener)
    except MessengerBridgeError as exc:
        if exc.status != 401:
            raise
        me = {"ok": False}
    if (
        me.get("ok")
        and me.get("name") == display
        and str(me.get("room_id") or "legacy") == room_id
    ):
        _save_jar(opener)
        return display
    joined = _request(
        "POST",
        "/api/join",
        payload={"invite": invite, "name": display, "room_id": room_id},
        opener=opener,
    )
    if not joined.get("ok"):
        raise MessengerBridgeError(
            f"Could not join messenger: {joined.get('error') or 'unknown'}",
            status=403,
        )
    _save_jar(opener)
    return str(joined.get("name") or display)


def list_bot_rooms() -> List[Dict[str, Any]]:
    """All rooms the bot can watch (legacy + created). Server-invite gated."""
    invite = messenger_invite()
    if not invite:
        return [{"room_id": "legacy", "title": "Friend room", "created_at": None}]
    from urllib.parse import quote

    try:
        data = _request("GET", f"/api/rooms/list?key={quote(invite)}")
    except MessengerBridgeError:
        # Older messenger builds without the endpoint: fall back to legacy only.
        return [{"room_id": "legacy", "title": "Friend room", "created_at": None}]
    rooms = [r for r in (data.get("rooms") or []) if isinstance(r, dict)]
    if not any(str(r.get("room_id")) == "legacy" for r in rooms):
        rooms.insert(
            0, {"room_id": "legacy", "title": "Friend room", "created_at": None}
        )
    return rooms


def list_room_messages(
    room_id: str, *, cookie_key: str, name: str, limit: int = 80
) -> List[Dict[str, Any]]:
    """Raw messages for ``room_id`` using the bot's per-room session."""
    ensure_session_as(name, cookie_key=cookie_key, room_id=room_id)
    opener = _opener_for(cookie_key)
    data = _request("GET", f"/api/messages?limit={int(limit)}", opener=opener)
    _save_jar(opener)
    if not data.get("ok"):
        raise MessengerBridgeError(
            f"Could not load room messages: {data.get('error') or 'unknown'}"
        )
    return [m for m in (data.get("messages") or []) if isinstance(m, dict)]


def post_room_message(
    room_id: str, *, cookie_key: str, name: str, body: str
) -> Dict[str, Any]:
    """Post ``body`` to ``room_id`` as ``name`` using a per-room session."""
    ensure_session_as(name, cookie_key=cookie_key, room_id=room_id)
    opener = _opener_for(cookie_key)
    data = _request(
        "POST", "/api/messages", payload={"body": body}, opener=opener
    )
    _save_jar(opener)
    if not data.get("ok"):
        raise MessengerBridgeError(
            f"{name} could not post: {data.get('error') or 'unknown'}"
        )
    return data.get("message") or {}


def ensure_session() -> str:
    """Join (or re-join) the cloud room as the local user; return display name."""
    return ensure_session_as(messenger_display_name(), cookie_key="user")


def list_raw_messages(limit: int = 200) -> List[Dict[str, Any]]:
    """Return raw messenger messages (requires an authenticated user session)."""
    ensure_session()
    data = _request("GET", f"/api/messages?limit={int(limit)}")
    if not data.get("ok"):
        raise MessengerBridgeError(
            f"Could not load friend messages: {data.get('error') or 'unknown'}"
        )
    return [m for m in (data.get("messages") or []) if isinstance(m, dict)]


def list_friend_messages(limit: int = 200) -> List[Dict[str, Any]]:
    """Return ledger-shaped chat_message dicts for the Friend thread UI."""
    me = ensure_session()
    raw = list_raw_messages(limit=limit)
    out: List[Dict[str, Any]] = []
    for msg in raw:
        author = str(msg.get("author") or "")
        body = str(msg.get("body") or "")
        created = str(msg.get("created_at") or "")
        msg_id = msg.get("id")
        role = "user" if author == me else "assistant"
        # Prefix friend/Qwen name so it reads like an agent reply.
        content = body if role == "user" else (f"{author}: {body}" if author else body)
        out.append(
            {
                "event_id": f"friend_{msg_id}",
                "ts": created,
                "type": "chat_message",
                "surface": "chat",
                "session_id": FRIEND_THREAD_ID,
                "sensitivity": "internal",
                "payload": {
                    "role": role,
                    "content": content,
                    "kind": "message",
                    "metadata": {
                        "friend": True,
                        "author": author,
                        "messenger_id": msg_id,
                    },
                },
            }
        )
    return out


def send_friend_message(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    if not text:
        raise MessengerBridgeError("message content is required")
    ensure_session()
    data = _request("POST", "/api/messages", payload={"body": text})
    if not data.get("ok"):
        raise MessengerBridgeError(
            f"Could not send: {data.get('error') or 'unknown'}",
            status=400,
        )
    return {
        "ok": True,
        "friend": True,
        "job_id": None,
        "status": "completed",
        "message": data.get("message"),
    }


def clear_friend_messages() -> Dict[str, Any]:
    """Delete the entire cloud room history (visible to every participant)."""
    ensure_session()
    data = _request("DELETE", "/api/messages")
    if not data.get("ok"):
        raise MessengerBridgeError(
            f"Could not delete chat: {data.get('error') or 'unknown'}",
            status=400,
        )
    return {
        "ok": True,
        "friend": True,
        "deleted": int(data.get("deleted") or 0),
    }
