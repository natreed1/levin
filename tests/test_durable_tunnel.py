"""Durable (named) Cloudflare tunnel config for Local Companion."""

from __future__ import annotations

from pathlib import Path

import messenger.companion_app as ca


def test_save_and_load_durable_tunnel(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path))
    public = ca.save_durable_tunnel(
        token="eyJtest-token",
        public_base_url="https://flyleaf-mac.example.com/v1",
    )
    assert public["configured"] is True
    assert public["mode"] == "named"
    assert public["public_base_url"] == "https://flyleaf-mac.example.com"
    loaded = ca.load_durable_tunnel()
    assert loaded["token"] == "eyJtest-token"
    assert loaded["public_base_url"] == "https://flyleaf-mac.example.com"


def test_prepare_cloud_link_prefers_named(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path))
    ca.save_durable_tunnel(
        token="eyJtest-token",
        public_base_url="https://stable.example.com",
    )
    monkeypatch.setattr(ca, "_cloudflared_bin", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(
        ca,
        "_try_named_cloudflared_tunnel",
        lambda port: "https://stable.example.com",
    )
    monkeypatch.setattr(
        ca,
        "_try_cloudflared_tunnel",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("quick tunnel should not run")),
    )
    result = ca.prepare_cloud_link()
    assert result["ok"] is True
    assert result["tunnel_mode"] == "named"
    assert result["stable"] is True
    assert result["public_base_url"] == "https://stable.example.com"


def test_prepare_falls_back_to_quick_when_unconfigured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ca, "_cloudflared_bin", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(ca, "_try_named_cloudflared_tunnel", lambda port: None)
    monkeypatch.setattr(
        ca,
        "_try_cloudflared_tunnel",
        lambda *a, **k: "https://random.trycloudflare.com",
    )
    result = ca.prepare_cloud_link()
    assert result["ok"] is True
    assert result["tunnel_mode"] == "quick"
    assert result["stable"] is False


def test_clear_durable_tunnel(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path))
    ca.save_durable_tunnel(
        token="eyJtest-token",
        public_base_url="https://stable.example.com",
    )
    cleared = ca.clear_durable_tunnel()
    assert cleared["configured"] is False
    assert ca.load_durable_tunnel() == {}
