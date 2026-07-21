"""Capture: mirror the hosted messenger's rooms into the local ledger.

One-way pull (messenger -> local ledger). It is:

- **READ-ONLY** — fetches messages; never posts anything back to the messenger.
- **IDEMPOTENT** — every ingested message stores its messenger id; re-running
  skips ids already present, so you can sync as often as you like (or schedule).
- **TAG-AWARE** — each incoming message is run through the actionable tagger, so
  an ask like "look into Acme AI" typed in the messenger is captured AND labelled
  in your local ledger (`intent:research` / `entity:<slug>` / `state:open`). It
  only tags; it does not act.

Config (via the existing bridge in ``messenger_bridge.py``):
``ANALYST_MESSENGER_URL`` + ``ANALYST_MESSENGER_INVITE`` (+ optional
``ANALYST_MESSENGER_NAME``). Kill-switch: ``ANALYST_MESSENGER_SYNC=off``.

Messages land in a per-room thread with ``desk_tag`` ``chat:messenger:{room_id}``,
which the dashboard already lists under Chats and shows on the Timeline.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .ledger import Ledger
from .schema import Sensitivity, Session, Surface

SYNC_ENV = "ANALYST_MESSENGER_SYNC"
_BOT_AUTHORS = {"qwen", "bot", "agent", "assistant"}


def sync_enabled() -> bool:
    raw = os.environ.get(SYNC_ENV, "on").strip().lower()
    return raw not in {"off", "0", "false", "no"}


def _messenger_thread(ledger: Ledger, room_id: str, title: str) -> Session:
    """Get or create the local chat thread mirroring one messenger room."""
    desk_tag = f"chat:messenger:{room_id}"
    for thread in ledger.list_chat_threads():
        if thread.get("desk_tag") == desk_tag:
            existing = ledger.get_session(thread["session_id"])
            if existing:
                return existing
    return ledger.start_background_session(
        title=title or f"Messenger: {room_id}",
        surface=Surface.CHAT.value,
        sensitivity=Sensitivity.INTERNAL.value,
        desk_tag=desk_tag,
    )


def _seen_messenger_ids(ledger: Ledger, session_id: str) -> set:
    seen: set = set()
    for event in ledger.list_chat_messages(session_id, limit=1000):
        mid = ((event.get("payload") or {}).get("metadata") or {}).get("messenger_id")
        if mid is not None:
            seen.add(str(mid))
    return seen


def _role_for(author: str) -> str:
    return "assistant" if author.strip().lower() in _BOT_AUTHORS else "user"


def ingest_room_messages(
    ledger: Ledger,
    room_id: str,
    room_title: str,
    messages: List[Dict[str, Any]],
    *,
    tag: bool = True,
) -> Dict[str, Any]:
    """Ingest already-fetched raw messenger messages into the local ledger."""
    thread = _messenger_thread(ledger, room_id, room_title)
    seen = _seen_messenger_ids(ledger, thread.session_id)
    ingested = 0
    tagged = 0
    for msg in messages:
        mid = msg.get("id")
        if mid is None or str(mid) in seen:
            continue
        author = str(msg.get("author") or "")
        body = str(msg.get("body") or "")
        role = _role_for(author)
        ledger.append_chat_message(
            thread.session_id,
            role=role,
            content=body,
            kind="message",
            metadata={
                "messenger_id": mid,
                "author": author,
                "room_id": room_id,
                "created_at": msg.get("created_at"),
            },
        )
        seen.add(str(mid))
        ingested += 1
        if tag and role == "user" and body.strip():
            try:
                from .classify import classify_message

                result = classify_message(body)
                if result["labels"]:
                    ledger.record_ask_labels(
                        thread.session_id,
                        result["labels"],
                        source="messenger",
                        meta={
                            "classification": {
                                "kind": result["kind"],
                                "entity": result["entity"],
                                "source": result["source"],
                            },
                            "author": author,
                            "messenger_id": mid,
                        },
                    )
                    tagged += 1
            except Exception:  # noqa: BLE001 — tagging must never break capture
                pass
    return {"ingested": ingested, "tagged": tagged, "session_id": thread.session_id}


def sync_messenger(
    ledger: Optional[Ledger] = None, *, limit: int = 200, tag: bool = True
) -> Dict[str, Any]:
    """Pull every reachable messenger room into the local ledger (one-way)."""
    if not sync_enabled():
        return {"status": "disabled", "rooms": 0, "ingested": 0, "tagged": 0}

    from .messenger_bridge import (
        MessengerBridgeError,
        list_bot_rooms,
        list_room_messages,
        messenger_configured,
        messenger_display_name,
    )

    if not messenger_configured():
        return {
            "status": "skipped",
            "reason": "messenger not configured (set ANALYST_MESSENGER_URL + ANALYST_MESSENGER_INVITE)",
            "rooms": 0,
            "ingested": 0,
            "tagged": 0,
        }

    ledger = ledger or Ledger()
    me = messenger_display_name()
    rooms = list_bot_rooms()
    total_ingested = 0
    total_tagged = 0
    threads: Dict[str, str] = {}
    errors: List[str] = []
    for room in rooms:
        room_id = str(room.get("room_id") or "legacy")
        title = str(room.get("title") or room_id)
        try:
            messages = list_room_messages(
                room_id, cookie_key=f"sync:{room_id}", name=me, limit=limit
            )
        except MessengerBridgeError as exc:
            errors.append(f"{room_id}: {exc}")
            continue
        result = ingest_room_messages(ledger, room_id, title, messages, tag=tag)
        total_ingested += result["ingested"]
        total_tagged += result["tagged"]
        threads[room_id] = result["session_id"]
    return {
        "status": "ok",
        "rooms": len(rooms),
        "ingested": total_ingested,
        "tagged": total_tagged,
        "threads": threads,
        "errors": errors,
    }


def capture_room_message(
    ledger: Ledger,
    room_id: str,
    author: str,
    text: str,
    *,
    room_title: str = "",
    messenger_id: Any = None,
    tag: bool = True,
) -> Dict[str, Any]:
    """Server-side hook: capture ONE live People-room message into the poster's
    ledger and classify it (deterministic, so it never blocks the send).

    This is the real-time counterpart to :func:`sync_messenger` — the messenger
    calls it from its message-post path so room chat lands in that user's
    Tracking ledger, tagged, exactly like everything else. Idempotent per
    ``messenger_id``; never raises (capture must not break chat).
    """
    thread = _messenger_thread(ledger, room_id, room_title or room_id)
    if messenger_id is not None and str(messenger_id) in _seen_messenger_ids(
        ledger, thread.session_id
    ):
        return {"captured": False, "reason": "duplicate", "session_id": thread.session_id}

    role = _role_for(str(author or ""))
    event = ledger.append_chat_message(
        thread.session_id,
        role=role,
        content=str(text or ""),
        kind="message",
        metadata={"messenger_id": messenger_id, "author": author, "room_id": room_id},
    )
    tagged = False
    if tag and role == "user" and str(text or "").strip():
        try:
            from .classify import classify_message

            result = classify_message(str(text), allow_qwen=False)
            if result["labels"]:
                ledger.record_ask_labels(
                    thread.session_id,
                    result["labels"],
                    source="people_room",
                    meta={
                        "classification": {
                            "kind": result["kind"],
                            "entity": result["entity"],
                            "source": result["source"],
                        },
                        "target_event_id": event.event_id,
                        "author": author,
                    },
                )
                tagged = True
        except Exception:  # noqa: BLE001 — capture must never break chat
            pass
    return {
        "captured": True,
        "tagged": tagged,
        "session_id": thread.session_id,
        "event_id": event.event_id,
    }
