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
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESSENGER_SCHEDULER", "0")
    monkeypatch.setenv("MESSENGER_EMAIL_DEV_EXPOSE", "1")
    monkeypatch.delenv("MESSENGER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("MESSENGER_RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MESSENGER_SMTP_HOST", raising=False)
    starlette_testclient = pytest.importorskip("starlette.testclient")
    import importlib
    import messenger.app as app_module

    importlib.reload(app_module)
    app = app_module.create_app()
    return starlette_testclient.TestClient(app)


def _signup_and_login(client, *, email: str, password: str = "password12", name: str = "Nat"):
    created = client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "display_name": name},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    token = body["dev_verify_url"].split("token=")[-1]
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    logged = client.post("/api/auth/login", json={"email": email, "password": password})
    assert logged.status_code == 200, logged.text
    return body


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


def test_owner_can_delete_room_non_owner_cannot(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner = _signup_and_login(client, email="owner@example.com", name="Owner")
    owner_id = owner["user_id"]

    created = client.post("/api/rooms", json={"name": "Owner", "title": "Delete me"})
    assert created.status_code == 200, created.text
    room_id = created.json()["room_id"]
    assert created.json()["owner_user_id"] == owner_id
    room_invite = parse_qs(urlparse(created.json()["share_url"]).query)["invite"][0]

    client.post("/api/messages", json={"body": "hello"})

    # Non-owner member cannot delete.
    client.cookies.clear()
    _signup_and_login(client, email="friend@example.com", name="Friend")
    joined = client.post(
        "/api/join",
        json={"invite": room_invite, "name": "Friend", "room_id": room_id},
    )
    assert joined.status_code == 200, joined.text
    forbidden = client.delete(f"/api/rooms/{room_id}")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"] == "owner_required"

    # Owner can delete; room and messages go away.
    client.cookies.clear()
    client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": "password12"},
    )
    deleted = client.delete(f"/api/rooms/{room_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "room_id": room_id}

    missing = client.delete(f"/api/rooms/{room_id}")
    assert missing.status_code == 404

    legacy = client.delete("/api/rooms/legacy")
    assert legacy.status_code == 400
    assert legacy.json()["error"] == "cannot_delete_legacy"

    listed = client.get("/api/rooms/list", params={"key": "server-secret"})
    assert listed.status_code == 200
    assert all(r["room_id"] != room_id for r in listed.json()["rooms"])
