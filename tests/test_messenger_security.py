"""Security patches: XSS input rejection, session revoke, login rate limit."""

from __future__ import annotations

from pathlib import Path

import pytest

from messenger.auth import normalize_name, normalize_title, utc_expiry_iso
from messenger.db import MessageStore
from messenger.emailer import send_verification_email


def _client(
    tmp_path: Path,
    monkeypatch,
    *,
    login_max: int | None = None,
    auth_email_max: int | None = None,
):
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
    if auth_email_max is not None:
        monkeypatch.setattr(app_module, "AUTH_EMAIL_RATE_LIMIT_MAX", auth_email_max)
        monkeypatch.setattr(app_module, "AUTH_EMAIL_RATE_LIMIT_WINDOW", 900.0)
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


@pytest.mark.parametrize("endpoint", ["forgot-password", "resend-verification"])
def test_auth_email_endpoints_are_rate_limited(
    tmp_path: Path, monkeypatch, endpoint: str
):
    client = _client(tmp_path / endpoint, monkeypatch, auth_email_max=2)
    codes = [
        client.post(
            f"/api/auth/{endpoint}",
            json={"email": "unknown@example.com"},
        ).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


def test_fly_signup_fails_closed_without_email_provider(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLY_APP_NAME", "levin")
    monkeypatch.setenv("MESSENGER_AUTO_VERIFY", "0")
    client = _client(tmp_path / "fly", monkeypatch)
    monkeypatch.setenv("FLY_APP_NAME", "levin")
    monkeypatch.setenv("MESSENGER_AUTO_VERIFY", "0")
    monkeypatch.setenv("MESSENGER_EMAIL_DEV_EXPOSE", "0")

    result = client.post(
        "/api/auth/signup",
        json={
            "email": "new@example.com",
            "password": "password12",
            "display_name": "New",
        },
    )

    assert result.status_code == 503
    assert result.json()["error"] == "email_delivery_unavailable"


@pytest.mark.parametrize("endpoint", ["forgot-password", "resend-verification"])
def test_fly_email_actions_fail_closed_without_email_provider(
    tmp_path: Path, monkeypatch, endpoint: str
):
    monkeypatch.setenv("FLY_APP_NAME", "levin")
    monkeypatch.setenv("MESSENGER_AUTO_VERIFY", "0")
    monkeypatch.setenv("MESSENGER_EMAIL_DEV_EXPOSE", "0")
    client = _client(tmp_path / f"fly-{endpoint}", monkeypatch)
    monkeypatch.setenv("FLY_APP_NAME", "levin")
    monkeypatch.setenv("MESSENGER_AUTO_VERIFY", "0")
    monkeypatch.setenv("MESSENGER_EMAIL_DEV_EXPOSE", "0")

    result = client.post(f"/api/auth/{endpoint}", json={"email": "someone@example.com"})
    assert result.status_code == 503
    assert result.json()["error"] == "email_delivery_unavailable"


def test_account_settings_update_profile_and_password(tmp_path: Path, monkeypatch):
    client = _client(tmp_path / "account", monkeypatch)
    created = _signup_and_login(
        client, email="account@example.com", password="password12", name="Before"
    )
    store = MessageStore(db_path=tmp_path / "account" / "messages.sqlite3")
    store.create_session(
        sid="other-browser",
        user_id=created["user_id"],
        expires_at=utc_expiry_iso(hours=1),
    )

    bad_name = client.patch(
        "/api/auth/profile", json={"display_name": "<script>bad</script>"}
    )
    assert bad_name.status_code == 400

    profile = client.patch(
        "/api/auth/profile", json={"display_name": "After"}
    )
    assert profile.status_code == 200, profile.text
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["display_name"] == "After"
    assert me.json()["name"] == "After"
    assert me.json()["session_count"] == 1
    assert me.json()["created_at"]

    wrong = client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong-password", "new_password": "newpassword99"},
    )
    assert wrong.status_code == 401
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "password12", "new_password": "newpassword99"},
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    old = client.post(
        "/api/auth/login",
        json={"email": "account@example.com", "password": "password12"},
    )
    assert old.status_code == 401
    new = client.post(
        "/api/auth/login",
        json={"email": "account@example.com", "password": "newpassword99"},
    )
    assert new.status_code == 200


def test_logout_other_sessions_keeps_current_session(tmp_path: Path, monkeypatch):
    client = _client(tmp_path / "sessions", monkeypatch)
    created = _signup_and_login(client, email="sessions@example.com", name="Sessions")
    store = MessageStore(db_path=tmp_path / "sessions" / "messages.sqlite3")
    store.create_session(
        sid="other-browser",
        user_id=created["user_id"],
        expires_at=utc_expiry_iso(hours=1),
    )

    result = client.post("/api/auth/logout-other-sessions")

    assert result.status_code == 200
    assert result.json()["revoked"] == 1
    assert client.get("/api/auth/me").status_code == 200


def test_email_2fa_login_requires_otp(tmp_path: Path, monkeypatch):
    client = _client(tmp_path / "twofa", monkeypatch)
    _signup_and_login(client, email="twofa@example.com", name="TwoFA")

    enabled = client.post(
        "/api/auth/email-2fa",
        json={"enabled": True, "password": "password12"},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["email_2fa_enabled"] is True
    assert client.get("/api/auth/me").json()["email_2fa_enabled"] is True

    client.post("/api/auth/logout")
    challenge = client.post(
        "/api/auth/login",
        json={"email": "twofa@example.com", "password": "password12"},
    )
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    assert body["requires_2fa"] is True
    assert body["challenge_id"]
    assert body["dev_otp_code"]
    assert client.get("/api/auth/me").status_code == 401

    bad = client.post(
        "/api/auth/verify-2fa",
        json={"challenge_id": body["challenge_id"], "code": "000000"},
    )
    assert bad.status_code == 401

    ok = client.post(
        "/api/auth/verify-2fa",
        json={"challenge_id": body["challenge_id"], "code": body["dev_otp_code"]},
    )
    assert ok.status_code == 200, ok.text
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "twofa@example.com"


def test_email_2fa_resend_issues_new_code(tmp_path: Path, monkeypatch):
    client = _client(tmp_path / "twofa-resend", monkeypatch)
    _signup_and_login(client, email="resend2fa@example.com", name="Resend")
    assert client.post(
        "/api/auth/email-2fa",
        json={"enabled": True, "password": "password12"},
    ).status_code == 200
    client.post("/api/auth/logout")

    first = client.post(
        "/api/auth/login",
        json={"email": "resend2fa@example.com", "password": "password12"},
    ).json()
    old_code = first["dev_otp_code"]
    resent = client.post(
        "/api/auth/resend-2fa",
        json={"challenge_id": first["challenge_id"]},
    )
    assert resent.status_code == 200, resent.text
    new_code = resent.json()["dev_otp_code"]
    assert new_code
    assert client.post(
        "/api/auth/verify-2fa",
        json={"challenge_id": first["challenge_id"], "code": old_code},
    ).status_code == 401
    assert client.post(
        "/api/auth/verify-2fa",
        json={"challenge_id": first["challenge_id"], "code": new_code},
    ).status_code == 200


def test_email_2fa_disable_with_password(tmp_path: Path, monkeypatch):
    client = _client(tmp_path / "twofa-off", monkeypatch)
    _signup_and_login(client, email="off2fa@example.com", name="Off")
    assert client.post(
        "/api/auth/email-2fa",
        json={"enabled": True, "password": "password12"},
    ).status_code == 200
    disabled = client.post(
        "/api/auth/email-2fa",
        json={"enabled": False, "password": "password12"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["email_2fa_enabled"] is False
    client.post("/api/auth/logout")
    login = client.post(
        "/api/auth/login",
        json={"email": "off2fa@example.com", "password": "password12"},
    )
    assert login.status_code == 200
    assert login.json().get("requires_2fa") is not True
    assert client.get("/api/auth/me").status_code == 200
