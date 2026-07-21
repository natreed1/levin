"""Account Settings → Models: multi-profile registry, encryption, tenancy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from messenger.model_link import (
    ModelLinkRegistry,
    destination_for_profile,
    profile_is_local,
)
from messenger.secrets_crypto import decrypt_secret, encrypt_secret
from messenger.companion_app import discover_candidates


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "test-secret-abc")
    enc = encrypt_secret("sk-ant-secret-key-9999")
    assert enc.startswith("enc:v1:")
    assert "sk-ant" not in enc
    assert decrypt_secret(enc) == "sk-ant-secret-key-9999"
    assert decrypt_secret("plaintext-legacy-key") == "plaintext-legacy-key"


def test_migrate_legacy_single_link(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "mig-secret")
    path = tmp_path / "model_links.json"
    path.write_text(
        json.dumps(
            {
                "user_a": {
                    "user_id": "user_a",
                    "provider": "anthropic",
                    "kind": "anthropic",
                    "base_url": "",
                    "api_key": "sk-ant-legacy-key-1234",
                    "model": "claude-sonnet-4-20250514",
                    "linked_at": "2026-01-01T00:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )
    reg = ModelLinkRegistry(path)
    listed = reg.list_profiles("user_a")
    assert len(listed["profiles"]) == 1
    pub = listed["profiles"][0]
    assert pub["provider"] == "anthropic"
    assert pub["setup_complete"] is True
    assert pub["api_key_set"] is True
    assert "sk-ant" not in json.dumps(pub)
    ep = reg.endpoint_for_call("user_a")
    assert ep is not None
    assert ep["api_key"] == "sk-ant-legacy-key-1234"
    assert ep["provider"] == "anthropic"


def test_multi_profile_activate_and_endpoint(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "multi-secret")
    reg = ModelLinkRegistry(tmp_path / "links.json")
    a = reg.add_frontier(
        "u1",
        provider="anthropic",
        api_key="sk-ant-aaaa-bbbb-cccc",
        model="claude-sonnet-4-20250514",
        activate=True,
    )
    b = reg.add_frontier(
        "u1",
        provider="openai",
        api_key="sk-openai-dddd-eeee",
        model="gpt-4o",
        activate=False,
    )
    assert reg.endpoint_for_call("u1")["provider"] == "anthropic"
    reg.activate("u1", b["id"])
    assert reg.endpoint_for_call("u1")["provider"] == "openai"
    assert reg.endpoint_for_call("u1")["model"] == "gpt-4o"
    # Public payloads never leak raw keys
    listed = reg.list_profiles("u1")
    profiles_blob = json.dumps(listed["profiles"])
    assert "sk-ant-aaaa" not in profiles_blob
    assert "sk-openai-dddd" not in profiles_blob
    assert a["id"] != b["id"]


def test_open_source_pipeline_enable_disable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "os-secret")
    reg = ModelLinkRegistry(tmp_path / "links.json")
    draft = reg.add_open_source_draft(
        "u2",
        candidate_id="ollama:qwen3:8b",
        runtime="ollama",
        model="qwen3:8b",
        label="Local Qwen",
    )
    assert draft["setup_complete"] is False
    assert reg.endpoint_for_call("u2") is None

    with pytest.raises(ValueError, match="Finish"):
        reg.enable_open_source("u2", draft["id"])

    ready = reg.set_pipeline_route(
        "u2",
        draft["id"],
        base_url="http://127.0.0.1:11435/v1",
        api_key="gateway-token-long-enough",
        gateway_mode="loopback",
    )
    assert ready["setup_complete"] is True
    assert ready["pipeline_route"]["base_url"].endswith("/v1")

    enabled = reg.enable_open_source("u2", draft["id"])
    assert enabled["enabled"] is True
    ep = reg.endpoint_for_call("u2")
    assert ep is not None
    assert ep["is_local"] == "1"
    assert ep["destination"] == "qwen"
    assert ep["api_key"] == "gateway-token-long-enough"
    assert profile_is_local(reg.get_profile("u2", draft["id"]) or {})
    assert destination_for_profile(reg.get_profile("u2", draft["id"])) == "qwen"

    reg.disable_open_source("u2", draft["id"])
    assert reg.endpoint_for_call("u2") is None


def test_tenancy_isolation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "tenancy")
    reg = ModelLinkRegistry(tmp_path / "links.json")
    reg.add_frontier(
        "alice",
        provider="anthropic",
        api_key="sk-ant-alice-key-zzzz",
        model="claude-sonnet-4-20250514",
    )
    reg.add_frontier(
        "bob",
        provider="openai",
        api_key="sk-openai-bob-key-yyyy",
        model="gpt-4o",
    )
    assert reg.endpoint_for_call("alice")["api_key"].endswith("zzzz")
    assert reg.endpoint_for_call("bob")["api_key"].endswith("yyyy")
    assert reg.get_profile("alice", (reg.list_profiles("bob")["profiles"][0]["id"])) is None


def test_discover_candidates_shape():
    result = discover_candidates()
    assert result["ok"] is True
    assert "candidates" in result
    assert "ollama" in result
    assert "recommended_model" in result


def test_gateway_health_requires_auth_and_adopts_shared_token(tmp_path: Path, monkeypatch):
    """Regression: /healthz is open; probe must use /v1/models, and reuse shared token."""
    import messenger.companion_app as ca

    shared = ca._shared_gateway_token_path()
    if not shared.exists():
        pytest.skip("no local gateway token on this machine")
    working = shared.read_text(encoding="utf-8").strip()
    if not ca._gateway_healthy(working):
        pytest.skip("gateway not reachable on :11435")
    assert not ca._gateway_healthy("wrong-token-xxxxxxxxxxxx")
    resolved = ca._resolve_working_gateway_token("wrong-token-xxxxxxxxxxxx")
    assert resolved == working


def test_settings_api_auth_and_crud(tmp_path: Path, monkeypatch):
    from tests.test_unified_workflow import _client, _signup_and_login

    client = _client(tmp_path, monkeypatch)
    forbidden = client.get("/api/settings/models")
    assert forbidden.status_code == 401

    _signup_and_login(client, email="settings@example.com", name="Settings")
    listed = client.get("/api/settings/models")
    assert listed.status_code == 200
    body = listed.json()
    assert body["ok"] is True
    assert "profiles" in body
    assert "companion" in body

    created = client.post(
        "/api/settings/models",
        json={
            "provider": "anthropic",
            "api_key": "sk-ant-test-key-12345678",
            "model": "claude-sonnet-4-20250514",
        },
    )
    assert created.status_code == 200
    profile = created.json()["profile"]
    assert profile["category"] == "frontier"
    assert "api_key" not in profile or not str(profile.get("api_key", "")).startswith("sk-")

    status = client.get("/api/model/status")
    assert status.status_code == 200
    assert status.json().get("linked") is True

    draft = client.post(
        "/api/settings/models/open-source/draft",
        json={
            "candidate_id": "ollama:qwen3:8b",
            "runtime": "ollama",
            "model": "qwen3:8b",
            "label": "Qwen",
        },
    )
    assert draft.status_code == 200
    draft_id = draft.json()["profile"]["id"]
    enable_fail = client.post(f"/api/settings/models/{draft_id}/enable")
    assert enable_fail.status_code == 400

    deleted = client.delete(f"/api/settings/models/{draft_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
