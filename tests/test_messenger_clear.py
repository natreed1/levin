"""Clear / delete room chat for the cloud messenger."""

from __future__ import annotations

from pathlib import Path
import hashlib

import analyst_ledger.messenger_bridge as bridge
from messenger.db import MessageStore


def test_message_store_clear(tmp_path: Path):
    store = MessageStore(db_path=tmp_path / "messages.sqlite3")
    store.add_message("Nat", "hello")
    store.add_message("Friend", "hi")
    assert len(store.list_messages()) == 2
    assert store.clear_messages() == 2
    assert store.list_messages() == []
    assert store.clear_messages() == 0


def test_message_store_isolates_created_rooms(tmp_path: Path):
    store = MessageStore(db_path=tmp_path / "messages.sqlite3")
    token_hash = hashlib.sha256(b"room-secret").hexdigest()
    store.create_room("room-a", "Room A", token_hash)
    assert store.room_token_ok("room-a", token_hash)
    assert not store.room_token_ok("room-a", hashlib.sha256(b"wrong").hexdigest())

    store.add_message("Nat", "legacy", room_id="legacy")
    store.add_message("Nat", "private", room_id="room-a")
    assert [m["body"] for m in store.list_messages(room_id="legacy")] == ["legacy"]
    assert [m["body"] for m in store.list_messages(room_id="room-a")] == ["private"]

    assert store.clear_messages(room_id="room-a") == 1
    assert store.list_messages(room_id="room-a") == []
    assert [m["body"] for m in store.list_messages(room_id="legacy")] == ["legacy"]


def test_clear_friend_messages_calls_delete(monkeypatch):
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(bridge, "ensure_session", lambda: "Nat")

    def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path))
        return {"ok": True, "deleted": 3, "me": "Nat"}

    monkeypatch.setattr(bridge, "_request", fake_request)
    result = bridge.clear_friend_messages()
    assert result == {"ok": True, "friend": True, "deleted": 3}
    assert calls == [("DELETE", "/api/messages")]
