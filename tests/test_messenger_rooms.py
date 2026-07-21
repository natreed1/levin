"""Room discovery + bot (server-invite) join-any-room for the cloud messenger."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse
import hashlib

import pytest

from messenger.db import MessageStore


def test_list_rooms_returns_created_rooms(tmp_path: Path):
    store = MessageStore(db_path=tmp_path / "messages.sqlite3")
    store.create_room("room-a", "Room A", hashlib.sha256(b"a").hexdigest())
    store.create_room("room-b", "Room B", hashlib.sha256(b"b").hexdigest())
    rooms = store.list_rooms()
    assert {r["room_id"] for r in rooms} == {"room-a", "room-b"}
    assert {r["title"] for r in rooms} == {"Room A", "Room B"}


def _client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_INVITE_TOKEN", "server-secret")
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("MESSENGER_DB_PATH", str(tmp_path / "messages.sqlite3"))
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("MESSENGER_SCHEDULER", "0")
    monkeypatch.delenv("MESSENGER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    starlette_testclient = pytest.importorskip("starlette.testclient")
    import messenger.app as app_module

    app = app_module.create_app()
    return starlette_testclient.TestClient(app)


def test_bot_joins_created_room_with_server_invite(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    created = client.post("/api/rooms", json={"name": "Nat", "title": "Test"})
    assert created.status_code == 200, created.text
    body = created.json()
    room_id = body["room_id"]
    room_invite = parse_qs(urlparse(body["share_url"]).query)["invite"][0]

    # A friend uses the room-specific invite.
    client.cookies.clear()
    friend = client.post(
        "/api/join",
        json={"invite": room_invite, "name": "Friend", "room_id": room_id},
    )
    assert friend.status_code == 200
    assert friend.json()["room_id"] == room_id

    # The bot uses the SERVER invite to join the same room.
    client.cookies.clear()
    bot = client.post(
        "/api/join",
        json={"invite": "server-secret", "name": "Qwen", "room_id": room_id},
    )
    assert bot.status_code == 200
    assert bot.json()["room_id"] == room_id

    # A wrong token cannot join.
    client.cookies.clear()
    bad = client.post(
        "/api/join",
        json={"invite": "nope", "name": "Intruder", "room_id": room_id},
    )
    assert bad.status_code == 403


def test_rooms_list_endpoint_requires_server_invite(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/rooms", json={"name": "Nat", "title": "Test"})

    forbidden = client.get("/api/rooms/list")
    assert forbidden.status_code == 403

    ok = client.get("/api/rooms/list", params={"key": "server-secret"})
    assert ok.status_code == 200
    rooms = ok.json()["rooms"]
    assert any(r["room_id"] == "legacy" for r in rooms)
    assert any(r["title"] == "Test" for r in rooms)
