"""Security patches: XSS input rejection, session revoke, login rate limit."""

from __future__ import annotations

from pathlib import Path

import pytest

from messenger.auth import normalize_name, normalize_title
from messenger.emailer import send_verification_email


def _client(tmp_path: Path, monkeypatch, *, login_max: int | None = None):
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
    if login_max is not None:
        monkeypatch.setattr(app_module, "LOGIN_RATE_LIMIT_MAX", login_max)
        monkeypatch.setattr(app_module, "LOGIN_RATE_LIMIT_WINDOW", 900.0)
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


def test_normalize_rejects_html():
    assert normalize_name('<script>alert(1)</script>') is None
    assert normalize_name('Nat <b>Reed</b>') is None
    assert normalize_name("Nat") == "Nat"
    assert normalize_title('<img src=x onerror=alert(1)>') is None
    assert normalize_title("Desk Notes") == "Desk Notes"


def test_email_html_escapes_display_name(monkeypatch):
    captured: dict = {}

    def _fake_send(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "backend": "console"}

    monkeypatch.setattr("messenger.emailer.send_email", _fake_send)
    send_verification_email(
        to="a@example.com",
        verify_url="https://example.com/v",
        display_name='<script>x</script>',
    )
    assert "&lt;script&gt;" in captured["html"]
    assert "<script>" not in captured["html"]


def test_signup_rejects_script_display_name(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    res = client.post(
        "/api/auth/signup",
        json={
            "email": "xss@example.com",
            "password": "password12",
            "display_name": "<script>alert(1)</script>",
        },
    )
    assert res.status_code == 400
    assert res.json()["error"] == "bad_name"


def test_create_room_rejects_script_title(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="owner@example.com", name="Owner")
    res = client.post(
        "/api/rooms",
        json={"title": "<script>alert(1)</script>", "name": "Owner"},
    )
    assert res.status_code == 400
    assert res.json()["error"] == "bad_title"


def test_logout_without_session_is_401(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    res = client.post("/api/auth/logout")
    assert res.status_code == 401
    assert res.json()["error"] == "unauthorized"
    legacy = client.post("/api/logout")
    assert legacy.status_code == 401


def test_logout_invalidates_cookie(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="sess@example.com", name="Sess")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True

    cookie = client.cookies.get("messenger_session")
    assert cookie

    out = client.post("/api/auth/logout")
    assert out.status_code == 200

    # Replay the stolen pre-logout cookie value.
    replay = client.get(
        "/api/auth/me",
        headers={"Cookie": f"messenger_session={cookie}"},
    )
    assert replay.status_code == 401


def test_login_rate_limited(tmp_path: Path, monkeypatch):
    client = _client(tmp_path / "rate", monkeypatch, login_max=3)
    email = "brute@example.com"
    _signup_and_login(client, email=email, name="Brute")
    client.post("/api/auth/logout")

    codes = []
    for _ in range(5):
        res = client.post(
            "/api/auth/login",
            json={"email": email, "password": "wrong-password"},
        )
        codes.append(res.status_code)
    assert 429 in codes
    assert codes[-1] == 429
    assert client.post(
        "/api/auth/login",
        json={"email": email, "password": "wrong-password"},
    ).json()["error"] == "rate_limited"
