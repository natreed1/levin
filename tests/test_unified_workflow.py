"""Account auth, per-user ledger scoping, and unified workflow APIs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from messenger.auth import hash_password, verify_password
from messenger.db import MessageStore
from messenger.scheduler import cron_matches
from messenger.tenancy import user_context, user_data_dir


def test_password_hash_roundtrip():
    h = hash_password("password12")
    assert verify_password("password12", h)
    assert not verify_password("wrong-password", h)


def test_user_ledger_isolation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    with user_context("alice") as ledger_a:
        ledger_a.start_session(title="Alice session", surface="notes", sensitivity="internal")
        a_sessions = ledger_a.list_sessions(limit=10)
    with user_context("bob") as ledger_b:
        b_sessions = ledger_b.list_sessions(limit=10)
    assert len(a_sessions) == 1
    assert len(b_sessions) == 0
    assert (user_data_dir("alice") / "ledger.sqlite3").exists()
    assert user_data_dir("alice") != user_data_dir("bob")


def test_cron_matches_weekday():
    # 2026-07-19 is a Sunday (cron dow=0)
    when = datetime(2026, 7, 19, 7, 0, tzinfo=timezone.utc)
    assert cron_matches("0 7 * * 0", when)
    assert cron_matches("0 7 * * 1-5", when) is False
    assert cron_matches("0 8 * * 0", when) is False


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
    assert body.get("dev_verify_url")
    token = body["dev_verify_url"].split("token=")[-1]
    verified = client.post("/api/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    logged = client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    assert logged.status_code == 200, logged.text
    return body


def test_signup_login_and_tracking(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    bad = client.post(
        "/api/auth/signup",
        json={"email": "bad", "password": "short", "display_name": "Nat"},
    )
    assert bad.status_code == 400

    created = client.post(
        "/api/auth/signup",
        json={
            "email": "nat@example.com",
            "password": "password12",
            "display_name": "Nat",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["ok"] is True
    assert body["user_id"]
    assert body["email"] == "nat@example.com"
    assert body["email_verified"] is False
    assert body.get("dev_verify_url")

    # Not logged in until verified
    me = client.get("/api/auth/me")
    assert me.status_code == 401

    blocked = client.post(
        "/api/auth/login",
        json={"email": "nat@example.com", "password": "password12"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"] == "email_unverified"

    token = body["dev_verify_url"].split("token=")[-1]
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200

    logged = client.post(
        "/api/auth/login",
        json={"email": "nat@example.com", "password": "password12"},
    )
    assert logged.status_code == 200

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["email_verified"] is True

    started = client.post(
        "/api/tracking/session/start",
        json={"title": "Morning scan", "capture_scope": "all_tabs"},
    )
    assert started.status_code == 200, started.text
    body_start = started.json()
    assert body_start["session"]["title"] == "Morning scan"
    assert body_start["capture_scope"] == "all_tabs"

    summary = client.get("/api/tracking/summary")
    assert summary.status_code == 200
    active = summary.json()["active_session"]
    assert active is not None
    assert active["capture_scope"] == "all_tabs"

    # Agent master thread
    threads = client.get("/api/agent-chats")
    assert threads.status_code == 200
    assert any(t.get("master") for t in threads.json()["threads"])

    autos = client.get("/api/automations")
    assert autos.status_code == 200
    assert "automations" in autos.json()


def test_forgot_and_reset_password(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="reset@example.com", name="Reset")

    forgot = client.post(
        "/api/auth/forgot-password",
        json={"email": "reset@example.com"},
    )
    assert forgot.status_code == 200
    assert forgot.json().get("dev_reset_url")
    token = forgot.json()["dev_reset_url"].split("reset=")[-1]

    bad = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "short"},
    )
    assert bad.status_code == 400

    # Re-issue because bad password still consumed? Actually we consume only on success...
    # Wait - consume happens before password hash check. Need to fix that!
    # Looking at reset_password - we hash_password first (can fail), then consume.
    # Good - short password fails before consume. Token still valid.
    ok = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "newpassword99"},
    )
    assert ok.status_code == 200, ok.text
    assert client.get("/api/auth/me").status_code == 401
    reused = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "anotherpassword99"},
    )
    assert reused.status_code == 400

    old = client.post(
        "/api/auth/login",
        json={"email": "reset@example.com", "password": "password12"},
    )
    assert old.status_code == 401
    new = client.post(
        "/api/auth/login",
        json={"email": "reset@example.com", "password": "newpassword99"},
    )
    assert new.status_code == 200


def test_owner_scoped_room_membership(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="owner@example.com", name="Owner")
    created = client.post("/api/rooms", json={"title": "Desk", "name": "Owner"})
    assert created.status_code == 200
    room_id = created.json()["room_id"]

    mine = client.get("/api/rooms/mine")
    assert mine.status_code == 200
    assert any(r["room_id"] == room_id for r in mine.json()["rooms"])

    store = MessageStore(db_path=tmp_path / "messages.sqlite3")
    room = store.room(room_id)
    assert room and room["owner_user_id"]


def test_room_agents_can_be_added_drag_target_style(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="agents@example.com", name="Agents")
    created = client.post("/api/rooms", json={"title": "Research", "name": "Agents"})
    assert created.status_code == 200
    room_id = created.json()["room_id"]

    unknown = client.post(
        f"/api/rooms/{room_id}/agents", json={"agent_id": "unknown"}
    )
    assert unknown.status_code == 400

    added = client.post(
        f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen-bull"}
    )
    assert added.status_code == 200, added.text
    assert added.json()["agents"] == ["qwen-bull"]

    mine = client.get("/api/rooms/mine")
    room = next(r for r in mine.json()["rooms"] if r["room_id"] == room_id)
    assert room["config"]["agents"] == ["qwen-bull"]
    assert room["config"]["specialists"] == ["qwen-bull"]
    assert "compute" in room

    invite = client.post(f"/api/rooms/{room_id}/invite", json={})
    assert invite.status_code == 200
    assert f"room={room_id}" in invite.json()["share_url"]

    removed = client.delete(f"/api/rooms/{room_id}/agents/qwen-bull")
    assert removed.status_code == 200
    assert removed.json()["agents"] == []


def test_specialist_workshop_create_and_debate(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="spec@example.com", name="Spec")
    specs = client.get("/api/specialists")
    assert specs.status_code == 200
    ids = [s["id"] for s in specs.json()["specialists"]]
    assert "qwen-bull" in ids and "qwen-contrarian" in ids

    bad = client.post(
        "/api/rooms",
        json={
            "title": "Too few",
            "name": "Spec",
            "kind": "specialist",
            "specialists": ["qwen-bull"],
        },
    )
    assert bad.status_code == 400

    created = client.post(
        "/api/rooms",
        json={
            "title": "Bull vs Bear",
            "name": "Spec",
            "kind": "specialist",
            "specialists": ["qwen-bull", "qwen-contrarian", "qwen-synthesizer"],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["kind"] == "specialist"
    assert len(body["config"]["specialists"]) == 3
    room_id = body["room_id"]

    run = client.post(
        f"/api/rooms/{room_id}/specialist-run",
        json={
            "action": "debate",
            "topic": "Is the thesis underpriced?",
            "stub": True,
            "rounds": 2,
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["started"] is True
    assert run.json()["rounds"] == 2

    # Give the background thread a moment to post turns.
    import time

    time.sleep(1.2)
    msgs = client.get("/api/messages?limit=50")
    assert msgs.status_code == 200
    bodies = [m.get("body") or "" for m in msgs.json()["messages"]]
    authors = {m["author"] for m in msgs.json()["messages"]}
    assert "Moderator" in authors
    assert "Qwen Bull" in authors or "Qwen Contrarian" in authors
    assert any("Round 1/2" in b for b in bodies)
    assert any("Round 2/2" in b for b in bodies)


def test_specialist_continuous_loop_and_stop(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="loop@example.com", name="Loop")
    created = client.post(
        "/api/rooms",
        json={
            "title": "Loop room",
            "name": "Loop",
            "kind": "specialist",
            "specialists": ["qwen-bull", "qwen-contrarian", "qwen-synthesizer"],
        },
    )
    assert created.status_code == 200, created.text
    room_id = created.json()["room_id"]

    run = client.post(
        f"/api/rooms/{room_id}/specialist-run",
        json={
            "action": "debate",
            "topic": "Keep arguing",
            "stub": True,
            "continuous": True,
        },
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["started"] is True
    assert body["continuous"] is True
    assert body["job"]["job_id"]
    assert "leave" in (body.get("message") or "").lower()

    status = client.get(f"/api/rooms/{room_id}/specialist-status")
    assert status.status_code == 200
    assert status.json()["running"] is True

    # Second start while running should conflict.
    conflict = client.post(
        f"/api/rooms/{room_id}/specialist-run",
        json={
            "action": "debate",
            "topic": "Again",
            "stub": True,
            "continuous": True,
        },
    )
    assert conflict.status_code == 409

    import time

    time.sleep(0.6)
    stop = client.post(f"/api/rooms/{room_id}/specialist-stop")
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True

    # Wait for the worker to honor the stop event.
    for _ in range(30):
        st = client.get(f"/api/rooms/{room_id}/specialist-status")
        if not st.json().get("running"):
            break
        time.sleep(0.15)
    else:
        raise AssertionError("job did not stop")

    msgs = client.get("/api/messages?limit=80")
    bodies = [m.get("body") or "" for m in msgs.json()["messages"]]
    assert any("loop" in b.lower() or "Loop" in b for b in bodies)
    assert any("Stopped" in b for b in bodies)


def test_companion_link_requires_account(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    forbidden = client.post(
        "/api/companion/link",
        json={"base_url": "http://127.0.0.1:8791", "token": "tok"},
    )
    assert forbidden.status_code == 401

    _signup_and_login(client, email="c@example.com", name="C")
    missing_token = client.post(
        "/api/companion/link",
        json={"base_url": "http://127.0.0.1:8791"},
    )
    assert missing_token.status_code == 400
    assert missing_token.json()["error"] == "token_required"

    # Token is stored even when the companion isn't running (verify may be unreachable).
    linked = client.post(
        "/api/companion/link",
        json={"base_url": "http://127.0.0.1:59999", "token": "test-companion-token"},
    )
    assert linked.status_code == 200
    body = linked.json()
    assert body["ok"] is True
    assert body.get("reachable") is False
    status = client.get("/api/companion/status")
    assert status.status_code == 200
    assert status.json()["linked"] is True
    assert status.json()["base_url"] == "http://127.0.0.1:59999"
