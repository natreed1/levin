"""Master setup chat: rooms/agents/workflows via website APIs + room workspace."""

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
    monkeypatch.setenv("ANALYST_CHAT_ROUTER", "off")
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


def _signup_and_login(client, *, email: str = "master@example.com"):
    created = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "password12", "display_name": "MasterUser"},
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


def _wait_job(client, job_id: str, *, timeout: float = 12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/api/agent-chats/jobs/{job_id}")
        assert res.status_code == 200, res.text
        job = res.json().get("job") or res.json()
        status = job.get("status")
        if status in {"completed", "failed", "cancelled", "done", "error"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_workspace_patch_encrypts_and_public_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "ledger"))
    from messenger.master_setup import apply_workspace_patch, public_workspace

    cfg = apply_workspace_patch(
        {},
        repo_url="https://github.com/org/repo",
        needs=[
            {
                "id": "github_token",
                "kind": "github_token",
                "label": "GitHub token",
                "hint": "repo read",
            }
        ],
        secrets={"github_token": "ghp_test_secret_value"},
    )
    assert cfg["workspace"]["repo_url"].endswith("/repo")
    assert cfg["workspace_secrets"]["github_token"].startswith("enc:v1:")
    pub = public_workspace(cfg)
    assert pub["needs"][0]["filled"] is True
    assert "ghp_test" not in str(pub)


def test_stub_plan_coding_room():
    from messenger.master_setup import stub_plan_from_message

    plan = stub_plan_from_message(
        "Create a coding room that reviews GitHub PRs then scans for exploits"
    )
    names = [t["name"] for t in plan["tools"]]
    assert "create_room" in names
    assert names.count("hire_agent") >= 2
    create = next(t for t in plan["tools"] if t["name"] == "create_room")
    assert any(n.get("kind") == "github_token" for n in create["args"]["needs"])


def test_hire_agent_accepts_name_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "ledger"))
    from messenger.master_setup import execute_tool

    class _Store:
        pass

    for alias, value in (
        ("title", "Title Agent"),
        ("agent_name", "Agent Name Bot"),
        ("label", "Label Specialist"),
    ):
        result = execute_tool(
            "hire_agent",
            {
                alias: value,
                "capability_ids": ["web_research"],
                "prompt": f"You are {value}.",
            },
            store=_Store(),
            user_id="u_test",
        )
        assert result.get("ok"), result
        assert result.get("name") == value


def test_assign_agent_creates_display_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "ledger"))
    from messenger.db import MessageStore
    from messenger.master_setup import execute_tool

    store = MessageStore(db_path=tmp_path / "messages.sqlite3")
    user_id = "u_assign_test"
    store.create_user(
        user_id,
        email="a@example.com",
        password_hash="hash",
        display_name="Assigner",
    )
    room_id = "room_assign_1"
    store.create_room(room_id, "Assign test", "hash", owner_user_id=user_id, kind="people")

    result = execute_tool(
        "assign_agent",
        {"room_id": room_id, "agent_id": "Filings Scout", "role": "research"},
        store=store,
        user_id=user_id,
    )
    assert result.get("ok"), result
    agent_id = result.get("agent_id")
    assert agent_id
    room = store.room(room_id)
    agents = (room.get("config") or {}).get("agents") or []
    assert agent_id in agents


def test_master_chat_creates_coding_room_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client)

    threads = client.get("/api/agent-chats")
    assert threads.status_code == 200, threads.text
    master = next(t for t in threads.json()["threads"] if t.get("master"))
    thread_id = master["session_id"]
    assert master["title"] in {"Master", "Master workflows"}

    posted = client.post(
        "/api/agent-chats/message",
        json={
            "thread_id": thread_id,
            "content": "Create a coding room that reviews GitHub PRs then scans for exploits",
            "stub": True,
        },
    )
    assert posted.status_code == 200, posted.text
    job = posted.json()["job"]
    assert job["kind"] == "master_chat"
    finished = _wait_job(client, job["job_id"])
    assert finished["status"] == "completed", finished
    assert not finished.get("error"), finished

    rooms = client.get("/api/rooms/mine")
    assert rooms.status_code == 200
    room_list = rooms.json()["rooms"]
    assert room_list, "Master should have created a team"
    coding = room_list[-1]
    ws = (coding.get("config") or {}).get("workspace") or {}
    needs = ws.get("needs") or []
    assert any(n.get("kind") == "github_token" for n in needs)
    assert "workspace_secrets" not in (coding.get("config") or {})

    agents = client.get("/api/registry/agents")
    assert agents.status_code == 200
    names = {a.get("name") for a in agents.json().get("agents") or []}
    assert "Diff Reviewer" in names
    assert "Security Critic" in names

    msgs = client.get(f"/api/agent-chats/messages?thread_id={thread_id}")
    assert msgs.status_code == 200
    bodies = []
    for m in msgs.json().get("messages") or []:
        payload = m.get("payload") if isinstance(m.get("payload"), dict) else {}
        bodies.append(str(payload.get("content") or m.get("content") or ""))
    assert any(
        "Room settings" in b or "Created team" in b or "Hiring" in b for b in bodies
    ), bodies


def test_room_config_accepts_workspace_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="ws@example.com")

    room = client.post("/api/rooms", json={"title": "Integrations", "name": "WS"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]

    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "workspace": {
                "repo_url": "https://github.com/acme/app",
                "default_ref": "main",
                "notes": "Need token for PR review",
                "needs": [
                    {
                        "id": "github_token",
                        "kind": "github_token",
                        "label": "GitHub token",
                        "hint": "repo scope",
                    }
                ],
            },
            "workspace_secrets": {"github_token": "ghp_live_test_key_9999"},
        },
    )
    assert patched.status_code == 200, patched.text
    cfg = patched.json()["config"]
    assert cfg["workspace"]["repo_url"].endswith("/app")
    assert cfg["workspace"]["needs"][0]["filled"] is True
    assert "workspace_secrets" not in cfg
