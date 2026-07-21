"""Tests for the sensitivity gate at the model-call boundary and the
note_digest local confidential tier (destination="qwen")."""

from __future__ import annotations

import pytest

from analyst_ledger.ledger import Ledger
from analyst_ledger.orchestration import ClaudeGateway
from analyst_ledger.runners import run_note_digest
from analyst_ledger.schema import Event, Sensitivity, Surface
from analyst_ledger.synthesize import assert_destination_allowed, run_synthesis


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Ledger()


# --- assert_destination_allowed ---------------------------------------------


def test_gate_external_destinations_cap_at_internal():
    assert_destination_allowed("anthropic", Sensitivity.INTERNAL)
    assert_destination_allowed("bedrock", Sensitivity.PUBLIC)
    with pytest.raises(RuntimeError, match="confidential"):
        assert_destination_allowed("anthropic", Sensitivity.CONFIDENTIAL)
    with pytest.raises(RuntimeError):
        assert_destination_allowed("bedrock", Sensitivity.CONFIDENTIAL)


def test_gate_local_destinations_allow_confidential():
    assert_destination_allowed("qwen", Sensitivity.CONFIDENTIAL)
    assert_destination_allowed("local_stub", Sensitivity.CONFIDENTIAL)


def test_gate_restricted_blocked_everywhere():
    for dest in ("anthropic", "bedrock", "qwen", "local_stub"):
        with pytest.raises(RuntimeError):
            assert_destination_allowed(dest, Sensitivity.RESTRICTED)


def test_gate_unknown_destination_treated_as_external():
    # run_synthesis routes unknown destinations to Anthropic, so the gate must too.
    with pytest.raises(RuntimeError):
        assert_destination_allowed("mystery", Sensitivity.CONFIDENTIAL)


# --- run_synthesis / ClaudeGateway wiring ------------------------------------


def test_run_synthesis_refuses_confidential_to_anthropic(ledger: Ledger):
    s = ledger.start_session("conf session", sensitivity=Sensitivity.CONFIDENTIAL.value)
    with pytest.raises(RuntimeError, match="anthropic"):
        run_synthesis(
            ledger,
            s.session_id,
            "summarize",
            max_sensitivity=Sensitivity.CONFIDENTIAL,
            destination="anthropic",
        )
    # Same ceiling on a local destination is fine.
    result = run_synthesis(
        ledger,
        s.session_id,
        "summarize",
        max_sensitivity=Sensitivity.CONFIDENTIAL,
        destination="local_stub",
    )
    assert result["status"] == "ok"


def test_gateway_refuses_confidential_on_claude_allows_on_qwen(ledger: Ledger):
    claude_gw = ClaudeGateway(ledger, responder=lambda m, t, s: "never reached")
    with pytest.raises(RuntimeError):
        claude_gw.complete(
            [{"role": "user", "content": "hi"}],
            kind="test",
            max_sensitivity=Sensitivity.CONFIDENTIAL,
        )

    qwen_gw = ClaudeGateway(
        ledger, responder=lambda m, t, s: "local ok", model="qwen3-8b"
    )
    result = qwen_gw.complete(
        [{"role": "user", "content": "hi"}],
        kind="test",
        max_sensitivity=Sensitivity.CONFIDENTIAL,
    )
    assert result.text == "local ok"


# --- note_digest local confidential tier -------------------------------------


def _seed_notes(ledger: Ledger):
    s = ledger.start_session("research", surface="notes")
    ledger.add_note("internal margin thesis")
    ledger.append_event(
        Event(
            type="note",
            surface="notes",
            session_id=s.session_id,
            sensitivity=Sensitivity.CONFIDENTIAL.value,
            payload={"text": "confidential channel checks"},
        )
    )
    ledger.append_event(
        Event(
            type="note",
            surface="notes",
            session_id=s.session_id,
            sensitivity=Sensitivity.RESTRICTED.value,
            payload={"text": "MNPI deal room content"},
        )
    )
    ledger.end_session(session_id=s.session_id, tags=["idea"])


def test_note_digest_qwen_includes_confidential_and_escalates(ledger, monkeypatch):
    _seed_notes(ledger)
    monkeypatch.setattr(
        "analyst_ledger.synthesize._call_openai_compatible_messages",
        lambda messages, **kw: "themes summary",
    )
    result = run_note_digest(ledger=ledger, ritual_id="note_digest", destination="qwen")
    assert result["status"] == "ok"
    assert "internal margin thesis" in result["note"]
    assert "confidential channel checks" in result["note"]
    assert "MNPI" not in result["note"]
    assert "themes summary" in result["note"]
    # Digest carrying confidential content is itself stamped confidential.
    events = ledger.list_events(session_id=result["session_id"], limit=50)
    note_event = next(e for e in events if e["type"] == "note")
    assert note_event["sensitivity"] == Sensitivity.CONFIDENTIAL.value
    run_event = next(e for e in events if e["type"] == "ritual_run")
    assert run_event["sensitivity"] == Sensitivity.CONFIDENTIAL.value


def test_note_digest_without_qwen_still_excludes_confidential(ledger):
    _seed_notes(ledger)
    result = run_note_digest(ledger=ledger, ritual_id="note_digest")
    assert "internal margin thesis" in result["note"]
    assert "confidential channel checks" not in result["note"]
    assert result["note_count"] == 1
    events = ledger.list_events(session_id=result["session_id"], limit=50)
    note_event = next(e for e in events if e["type"] == "note")
    assert note_event["sensitivity"] == Sensitivity.INTERNAL.value


def test_note_digest_qwen_offline_is_graceful(ledger, monkeypatch):
    monkeypatch.setenv("ANALYST_QWEN_BASE_URL", "http://127.0.0.1:9/v1")
    _seed_notes(ledger)
    result = run_note_digest(ledger=ledger, ritual_id="note_digest", destination="qwen")
    assert result["status"] == "ok"
    assert "internal margin thesis" in result["note"]
    assert "Local model offline" in result["note"]
