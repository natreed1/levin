"""Local passwordless dev login — must never activate on Fly."""

from __future__ import annotations

from pathlib import Path

import pytest


def _client(tmp_path: Path, monkeypatch, **env: str):
    monkeypatch.setenv("MESSENGER_INVITE_TOKEN", "server-secret")
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("MESSENGER_DB_PATH", str(tmp_path / "messages.sqlite3"))
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESSENGER_SCHEDULER", "0")
    monkeypatch.delenv("MESSENGER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("MESSENGER_RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MESSENGER_SMTP_HOST", raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    starlette_testclient = pytest.importorskip("starlette.testclient")
    import importlib

    import messenger.app as app_module

    importlib.reload(app_module)
    return starlette_testclient.TestClient(app_module.app)


def test_dev_login_disabled_by_default(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MESSENGER_DEV_AUTO_LOGIN="0")
    denied = client.post("/api/auth/dev-login")
    assert denied.status_code == 403
    me = client.get("/api/me")
    assert me.status_code == 401
    assert me.json().get("dev_auto_login") is False


def test_dev_login_works_locally(tmp_path, monkeypatch):
    client = _client(
        tmp_path,
        monkeypatch,
        MESSENGER_DEV_AUTO_LOGIN="1",
        MESSENGER_DEV_EMAIL="dev@example.com",
        MESSENGER_DEV_NAME="Dev",
    )
    first = client.post("/api/auth/dev-login")
    assert first.status_code == 200
    body = first.json()
    assert body["ok"] is True
    assert body["email"] == "dev@example.com"
    assert body["display_name"] == "Dev"
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["email"] == "dev@example.com"
    # Idempotent — second call reuses the same account.
    second = client.post("/api/auth/dev-login")
    assert second.status_code == 200
    assert second.json()["user_id"] == body["user_id"]


def test_dev_login_blocked_on_fly(tmp_path, monkeypatch):
    client = _client(
        tmp_path,
        monkeypatch,
        MESSENGER_DEV_AUTO_LOGIN="1",
        FLY_APP_NAME="levin",
    )
    denied = client.post("/api/auth/dev-login")
    assert denied.status_code == 403
    me = client.get("/api/me")
    assert me.json().get("dev_auto_login") is False
