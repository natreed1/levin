"""Friends, usernames, and notifications."""

from __future__ import annotations

from pathlib import Path

import pytest

from messenger.auth import normalize_username


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


def _signup(client, *, email: str, name: str, username: str, password: str = "password12"):
    created = client.post(
        "/api/auth/signup",
        json={
            "email": email,
            "password": password,
            "display_name": name,
            "username": username,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    token = body["dev_verify_url"].split("token=")[-1]
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    logged = client.post("/api/auth/login", json={"email": email, "password": password})
    assert logged.status_code == 200, logged.text
    return body


def test_normalize_username():
    assert normalize_username("Nat_Reed") == "nat_reed"
    assert normalize_username("@Bullish") == "bullish"
    assert normalize_username("ab") is None
    assert normalize_username("1abc") is None
    assert normalize_username("bad-name") is None
    assert normalize_username("ok_user_123") == "ok_user_123"


def test_signup_stores_username(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup(client, email="a@example.com", name="Ada", username="ada_lovelace")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "ada_lovelace"


def test_friend_request_flow_and_notifications(tmp_path: Path, monkeypatch):
    client_a = _client(tmp_path, monkeypatch)
    _signup(client_a, email="a@example.com", name="Ada", username="ada")

    client_b = _client(tmp_path, monkeypatch)
    _signup(client_b, email="b@example.com", name="Bob", username="bob")

    client_a = _client(tmp_path, monkeypatch)
    assert client_a.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "password12"}
    ).status_code == 200

    sent = client_a.post("/api/friends/request", json={"username": "bob"})
    assert sent.status_code == 200, sent.text

    client_b = _client(tmp_path, monkeypatch)
    assert client_b.post(
        "/api/auth/login", json={"email": "b@example.com", "password": "password12"}
    ).status_code == 200
    notes = client_b.get("/api/notifications")
    assert notes.status_code == 200
    body = notes.json()
    assert body["unread"] >= 1
    assert any(n["type"] == "friend_request" for n in body["notifications"])

    friends_b = client_b.get("/api/friends").json()
    assert len(friends_b["incoming"]) == 1
    requester_id = friends_b["incoming"][0]["user_id"]

    accepted = client_b.post(f"/api/friends/{requester_id}/accept")
    assert accepted.status_code == 200, accepted.text

    client_a = _client(tmp_path, monkeypatch)
    assert client_a.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "password12"}
    ).status_code == 200
    friends_a = client_a.get("/api/friends").json()
    assert any(f["username"] == "bob" for f in friends_a["friends"])


def test_search_users_and_add_friend_to_room(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup(client, email="owner@example.com", name="Owner", username="owner")
    room = client.post("/api/rooms", json={"title": "Desk"}).json()
    room_id = room["room_id"]

    client2 = _client(tmp_path, monkeypatch)
    _signup(client2, email="pal@example.com", name="Pal", username="pal_friend")

    client = _client(tmp_path, monkeypatch)
    assert client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "password12"}
    ).status_code == 200
    search = client.get("/api/users/search?q=pal")
    assert search.status_code == 200
    users = search.json()["users"]
    assert any(u["username"] == "pal_friend" for u in users)

    assert client.post("/api/friends/request", json={"username": "pal_friend"}).status_code == 200

    client2 = _client(tmp_path, monkeypatch)
    assert client2.post(
        "/api/auth/login", json={"email": "pal@example.com", "password": "password12"}
    ).status_code == 200
    owner_id = client2.get("/api/friends").json()["incoming"][0]["user_id"]
    assert client2.post(f"/api/friends/{owner_id}/accept").status_code == 200

    client = _client(tmp_path, monkeypatch)
    assert client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "password12"}
    ).status_code == 200
    pal_id = client.get("/api/friends").json()["friends"][0]["user_id"]
    added = client.post(
        f"/api/rooms/{room_id}/members", json={"user_id": pal_id}
    )
    assert added.status_code == 200, added.text
    members = client.get(f"/api/rooms/{room_id}/members").json()["members"]
    assert any(m["username"] == "pal_friend" for m in members)


def test_profile_can_claim_username(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    created = client.post(
        "/api/auth/signup",
        json={"email": "c@example.com", "password": "password12", "display_name": "Casey"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body.get("username")
    token = body["dev_verify_url"].split("token=")[-1]
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert client.post(
        "/api/auth/login", json={"email": "c@example.com", "password": "password12"}
    ).status_code == 200
    updated = client.patch(
        "/api/auth/profile",
        json={"display_name": "Casey", "username": "casey_ok"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["username"] == "casey_ok"


def test_friends_ui_markers_present():
    root = Path(__file__).resolve().parents[1]
    html = (root / "messenger" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "messenger" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="notifications"' in html
    assert 'id="tab-notifications"' in html
    assert 'id="settings-username"' in html
    assert 'id="profile-friends-list"' in html
    assert 'id="friend-search"' in html
    assert "Add friends" in html
    assert 'api("/api/friends/request"' in js
    assert "loadNotificationsTab" in js
