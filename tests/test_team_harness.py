"""Hire → Teams → Harness Phase 1: config, draft-from-prompt, workflow posts."""

from __future__ import annotations

import time
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


def _signup_and_login(client, *, email: str = "harness@example.com"):
    created = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "password12", "display_name": "Harness"},
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


def test_harness_config_roles_orchestrator_and_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client)

    room = client.post("/api/rooms", json={"title": "Store UI", "name": "Harness"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]

    client.post(f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen-bull"})
    client.post(f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen-bear"})

    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Ship the storefront",
            "prompts": "Prefer primary sources",
            "skills": ["sec_filings_check", "note_digest"],
            "orchestrator": "workflow",
            "agents": ["qwen-bull", "qwen-bear"],
            "roles": {"qwen-bull": "SEO research", "qwen-bear": "UI critique"},
        },
    )
    assert patched.status_code == 200, patched.text
    cfg = patched.json()["config"]
    assert cfg["orchestrator"] == "workflow"
    assert cfg["roles"]["qwen-bull"] == "SEO research"
    assert cfg["skills"] == ["sec_filings_check", "note_digest"]
    assert set(cfg["agents"]) == {"qwen-bull", "qwen-bear"}

    loop = client.post(
        "/api/automations/from-chat",
        json={
            "name": "store_filings_loop",
            "steps": ["sec_filings_check", "note_digest"],
            "room_id": room_id,
            "schedule": "0 7 * * 1-5",
        },
    )
    assert loop.status_code == 200, loop.text
    assert loop.json()["ritual_id"] == "store_filings_loop"

    autos = client.get("/api/automations")
    assert autos.status_code == 200, autos.text
    bound = [a for a in autos.json()["automations"] if a.get("room_id") == room_id]
    assert any(a.get("ritual_id") == "store_filings_loop" for a in bound)


def test_draft_from_prompt_allowlist_and_out_of_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="draft@example.com")

    ok = client.post(
        "/api/registry/capabilities/draft-from-prompt",
        json={"description": "Scan SEC filings for NVDA material changes", "stub": True},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["in_scope"] is True
    assert body["capability"]["approved"] is False
    assert body["capability"]["executable"] is True
    assert "sec_filings_check" in (body.get("mapped_from") or [])

    oos = client.post(
        "/api/registry/capabilities/draft-from-prompt",
        json={
            "description": "Source products off Alibaba and track cargo shipments overseas",
            "stub": True,
        },
    )
    assert oos.status_code == 200, oos.text
    assert oos.json()["in_scope"] is False
    assert oos.json()["needs_script"] is True

    caps = client.get("/api/registry/capabilities")
    assert caps.status_code == 200, caps.text
    rows = caps.json()["capabilities"]
    by_id = {c["id"]: c for c in rows}
    assert by_id["sec_filings_check"]["executable"] is True
    assert by_id["web_research"]["executable"] is True
    # Draft we just created should be present and not yet approved
    drafts = [c for c in rows if c.get("proposed_by") == "hire_describe"]
    assert drafts
    assert drafts[0]["approved"] is False


def test_workflow_harness_posts_to_team_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="workflow@example.com")

    room = client.post("/api/rooms", json={"title": "Filings desk", "name": "WF"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]

    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Keep filings current",
            "skills": ["sec_filings_check"],
            "orchestrator": "workflow",
            "agents": ["qwen"],
        },
    )
    assert patched.status_code == 200, patched.text
    client.post(f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen"})

    run = client.post(
        f"/api/rooms/{room_id}/harness-run",
        json={"stub": True},
    )
    assert run.status_code == 200, run.text
    assert run.json()["job"]["action"] == "workflow"

    # Wait briefly for background stub posts
    deadline = time.time() + 5
    bodies = []
    while time.time() < deadline:
        msgs = client.get(f"/api/messages?room_id={room_id}")
        assert msgs.status_code == 200, msgs.text
        bodies = [m.get("body") or "" for m in msgs.json().get("messages") or []]
        if any("Workflow" in b or "sec_filings_check" in b or "Harness" in b for b in bodies):
            break
        time.sleep(0.15)
    assert any(
        "Workflow" in b or "sec_filings_check" in b or "Harness" in b for b in bodies
    ), bodies

    auto = client.post(
        f"/api/rooms/{room_id}/autonomy",
        json={"enabled": True, "stub": True},
    )
    # May be busy from prior harness-run; either starts or reports busy/already
    assert auto.status_code in {200, 400}, auto.text
