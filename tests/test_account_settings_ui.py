"""Flyleaf account settings page static wiring."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "messenger" / "static"


def test_account_settings_sections_are_present():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for section in (
        "profile",
        "security",
        "preferences",
        "models",
        "privacy",
        "danger",
    ):
        assert f'id="settings-panel-{section}"' in html
        assert f'data-settings-panel="{section}"' in html
    assert 'id="profile-settings-form"' in html
    assert 'id="change-password-form"' in html
    assert 'id="email-2fa-form"' in html
    assert 'id="otp-form"' in html
    assert 'id="logout-other-sessions-btn"' in html


def test_account_settings_actions_and_layout_are_wired():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert 'api("/api/auth/profile"' in js
    assert 'api("/api/auth/change-password"' in js
    assert 'api("/api/auth/logout-other-sessions"' in js
    assert 'api("/api/auth/email-2fa"' in js
    assert 'api("/api/auth/verify-2fa"' in js
    assert "flyleaf-theme" in js
    assert ".settings-layout" in css
    assert 'html[data-theme="light"]' in css


def test_signup_auto_verified_enters_app():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "data?.auto_verified" in js
    assert "await bootstrap()" in js
