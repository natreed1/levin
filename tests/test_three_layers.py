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


def test_registry_studio_compose_and_room_objective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="studio@example.com")

    lens = client.post(
        "/api/registry/lenses",
        json={"name": "Cautious", "prompt": "Flag uncertainty."},
    )
    assert lens.status_code == 200, lens.text
    lens_id = lens.json()["lens"]["id"]

    cap = client.post(
        "/api/registry/capabilities",
        json={"name": "Clip notes", "summary": "Summarize clips"},
    )
    assert cap.status_code == 200, cap.text

    agent = client.post(
        "/api/registry/agents",
        json={
            "name": "Cautious scout",
            "lens_ids": [lens_id],
            "capability_ids": ["web_research"],
        },
    )
    assert agent.status_code == 200, agent.text
    agent_id = agent.json()["agent"]["id"]

    agents = client.get("/api/registry/agents")
    assert any(a["id"] == agent_id for a in agents.json()["agents"])

    room = client.post("/api/rooms", json={"title": "Desk", "name": "Layers"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]

    added = client.post(
        f"/api/rooms/{room_id}/agents",
        json={"agent_id": agent_id},
    )
    assert added.status_code == 200, added.text

    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Keep NVDA filings current",
            "prompts": "Prefer primary sources\nCall out uncertainty",
            "skills": ["sec_filings_check"],
        },
    )
    assert patched.status_code == 200, patched.text
    cfg = patched.json()["config"]
    assert cfg["objective"] == "Keep NVDA filings current"
    assert "Prefer primary sources" in cfg["prompts"]
    assert cfg["skills"] == ["sec_filings_check"]

    # Need a second agent for continuous debate; add a builtin.
    client.post(f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen-bull"})
    auto = client.post(
        f"/api/rooms/{room_id}/autonomy",
        json={"enabled": True, "stub": True},
    )
    assert auto.status_code == 200, auto.text
    assert auto.json()["autonomy"]["enabled"] is True

    stop = client.post(
        f"/api/rooms/{room_id}/autonomy",
        json={"enabled": False},
    )
    assert stop.status_code == 200, stop.text
    assert stop.json()["autonomy"]["enabled"] is False


def test_registry_agent_update_and_bulk_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="bulk@example.com")

    a1 = client.post(
        "/api/registry/agents",
        json={"name": "Alpha scout", "prompt": "Be brief.", "lens_ids": [], "capability_ids": []},
    )
    assert a1.status_code == 200, a1.text
    id1 = a1.json()["agent"]["id"]
    a2 = client.post(
        "/api/registry/agents",
        json={"name": "Beta scout", "prompt": "Be skeptical.", "capability_ids": ["web_research"]},
    )
    assert a2.status_code == 200, a2.text
    id2 = a2.json()["agent"]["id"]

    patched = client.patch(
        f"/api/registry/agents/{id1}",
        json={"name": "Alpha revised", "capability_ids": ["note_digest"], "prompt": "Revised."},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["agent"]["name"] == "Alpha revised"
    assert "note_digest" in patched.json()["agent"]["capabilities"]

    builtin = client.delete("/api/registry/agents/qwen-bull")
    assert builtin.status_code == 404

    deleted = client.post("/api/registry/agents/delete", json={"ids": [id1, id2, "qwen-bull"]})
    assert deleted.status_code == 200, deleted.text
    assert set(deleted.json()["deleted"]) == {id1, id2}

    listing = client.get("/api/registry/agents")
    ids = {a["id"] for a in listing.json()["agents"]}
    assert id1 not in ids and id2 not in ids
    assert "qwen-bull" in ids
