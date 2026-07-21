"""Messenger capture sync: ingest + dedupe + tag, with the bridge mocked."""

import pytest

import analyst_ledger.messenger_bridge as bridge
from analyst_ledger.ledger import Ledger
from analyst_ledger.messenger_sync import sync_messenger


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("ANALYST_MESSENGER_SYNC", "on")
    monkeypatch.setenv("ANALYST_CHAT_ACTIONABLE", "on")
    monkeypatch.setenv("ANALYST_CLASSIFY_QWEN", "off")  # deterministic-only in tests
    return Ledger()


def _fake_bridge(monkeypatch, rooms, messages_by_room, me="You"):
    monkeypatch.setattr(bridge, "messenger_configured", lambda: True)
    monkeypatch.setattr(bridge, "messenger_display_name", lambda: me)
    monkeypatch.setattr(bridge, "list_bot_rooms", lambda: rooms)

    def _list(room_id, *, cookie_key, name, limit=200):
        return messages_by_room.get(room_id, [])

    monkeypatch.setattr(bridge, "list_room_messages", _list)


def test_sync_ingests_and_tags(ledger, monkeypatch):
    rooms = [{"room_id": "legacy", "title": "Friend room"}]
    messages = {
        "legacy": [
            {"id": 1, "author": "Nat", "body": "hey", "created_at": "t1"},
            {"id": 2, "author": "Nat", "body": "we should look into Acme AI", "created_at": "t2"},
        ]
    }
    _fake_bridge(monkeypatch, rooms, messages)

    res = sync_messenger(ledger)
    assert res["status"] == "ok"
    assert res["ingested"] == 2
    assert res["tagged"] == 1

    sid = res["threads"]["legacy"]
    bodies = [m["payload"]["content"] for m in ledger.list_chat_messages(sid)]
    assert "we should look into Acme AI" in bodies

    labels = [
        e for e in ledger.list_events(session_id=sid, limit=50) if e["type"] == "label"
    ]
    assert labels
    assert "kind:research" in labels[0]["payload"]["labels"]
    assert "entity:acme-ai" in labels[0]["payload"]["labels"]
    assert labels[0]["payload"]["source"] == "messenger"


def test_sync_is_idempotent(ledger, monkeypatch):
    rooms = [{"room_id": "legacy", "title": "Friend"}]
    messages = {"legacy": [{"id": 1, "author": "Nat", "body": "hi", "created_at": "t"}]}
    _fake_bridge(monkeypatch, rooms, messages)

    first = sync_messenger(ledger)
    second = sync_messenger(ledger)
    assert first["ingested"] == 1
    assert second["ingested"] == 0  # nothing new on re-sync

    sid = first["threads"]["legacy"]
    assert len(ledger.list_chat_messages(sid)) == 1


def test_bot_author_is_assistant_role(ledger, monkeypatch):
    rooms = [{"room_id": "legacy", "title": "Friend"}]
    messages = {"legacy": [{"id": 9, "author": "Qwen", "body": "hello from the model", "created_at": "t"}]}
    _fake_bridge(monkeypatch, rooms, messages)

    res = sync_messenger(ledger)
    sid = res["threads"]["legacy"]
    roles = [m["payload"]["role"] for m in ledger.list_chat_messages(sid)]
    assert roles == ["assistant"]
    assert res["tagged"] == 0  # bot messages are not tagged as asks


def test_sync_skipped_when_not_configured(ledger, monkeypatch):
    monkeypatch.setattr(bridge, "messenger_configured", lambda: False)
    res = sync_messenger(ledger)
    assert res["status"] == "skipped"


def test_sync_disabled_kill_switch(ledger, monkeypatch):
    monkeypatch.setenv("ANALYST_MESSENGER_SYNC", "off")
    res = sync_messenger(ledger)
    assert res["status"] == "disabled"


def test_capture_room_message(ledger):
    from analyst_ledger.messenger_sync import capture_room_message

    res = capture_room_message(
        ledger,
        "legacy",
        "Nat",
        "the sync is broken, we should refactor the classifier",
        messenger_id=42,
    )
    assert res["captured"] is True
    assert res["tagged"] is True

    sid = res["session_id"]
    labels = [
        e for e in ledger.list_events(session_id=sid, limit=50) if e["type"] == "label"
    ]
    assert any("kind:build" in lbl["payload"]["labels"] for lbl in labels)

    # Idempotent per messenger_id — the same message isn't captured twice.
    again = capture_room_message(
        ledger, "legacy", "Nat", "the sync is broken", messenger_id=42
    )
    assert again["captured"] is False
