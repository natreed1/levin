"""Three-layer catalog: capabilities, agents, automation loops."""

from __future__ import annotations

from pathlib import Path

import pytest


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MESSENGER_INVITE_TOKEN", "server-secret")
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("MESSENGER_DB_PATH", str(tmp_path / "messages.sqlite3"))
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESSENGER_SCHEDULER", "0")
    monkeypatch.setenv("MESSENGER_CLASSIFY_SWEEP", "0")
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


def _signup_and_login(client, *, email: str = "layers@example.com"):
    created = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "password12", "display_name": "Layers"},
    )
    assert created.status_code == 200, created.text
    token = created.json()["dev_verify_url"].split("token=")[-1]
    verified = client.post("/api/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    logged = client.post(
        "/api/auth/login",
        json={"email": email, "password": "password12"},
    )
    assert logged.status_code == 200, logged.text


def test_capabilities_include_builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client)

    caps = client.get("/api/capabilities")
    assert caps.status_code == 200, caps.text
    rows = caps.json()["capabilities"]
    ids = {c["id"] for c in rows}
    assert "web_research" in ids
    assert "sec_filings_check" in ids
    assert all(c.get("kind") == "builtin" for c in rows if c["id"] == "web_research")


def test_agents_catalog_separates_lenses_and_operators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client)

    agents = client.get("/api/agents")
    assert agents.status_code == 200, agents.text
    by_id = {a["id"]: a for a in agents.json()["agents"]}
    assert by_id["qwen-bull"]["kind"] == "lens"
    assert by_id["qwen-bull"]["capabilities"] == []
    assert by_id["qwen"]["kind"] == "operator"
    assert "web_research" in by_id["qwen"]["capabilities"]
    assert by_id["master"]["kind"] == "operator"


def test_automate_from_chat_creates_draft_capability_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client)

    created = client.post(
        "/api/automations/from-chat",
        json={
            "name": "morning_brief_loop",
            "steps": ["web_research", "sec_filings_check", "note_digest"],
            "schedule": "0 7 * * 1-5",
            "room_id": "room_test",
            "transcript": "Alice: let's automate the morning brief",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["ritual_id"] == "morning_brief_loop"
    assert body["automation"]["approved"] is False
    assert body["automation"]["proposed_by"] == "room_automate"
    assert len(body["automation"]["steps"]) == 3

    caps = client.get("/api/capabilities")
    user_caps = [
        c for c in caps.json()["capabilities"] if c.get("id") == "morning_brief_loop"
    ]
    assert user_caps
    assert user_caps[0]["kind"] == "user"
    assert user_caps[0]["approved"] is False

    loops = client.get("/api/automations/loops")
    assert loops.status_code == 200
    assert all(a["id"] != "morning_brief_loop" for a in loops.json()["automations"])

    approved = client.post(
        "/api/capabilities/approve",
        json={"ritual_id": "morning_brief_loop"},
    )
    assert approved.status_code == 200, approved.text
    loops2 = client.get("/api/automations/loops")
    assert any(a["id"] == "morning_brief_loop" for a in loops2.json()["automations"])
