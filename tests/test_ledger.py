"""Tests for analyst ledger core flows."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from analyst_ledger.ledger import Ledger
from analyst_ledger.redact import build_synthesis_prompt, filter_events_for_egress, redact_text
from analyst_ledger.schema import Sensitivity, sensitivity_allows_egress
from analyst_ledger.sft_export import build_pairs, export_pairs
from analyst_ledger.synthesize import run_synthesis


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    return Ledger()


def test_session_note_end_roundtrip(ledger: Ledger):
    session = ledger.start_session("AM research — NVDA", surface="notes", desk_tag="tech")
    assert ledger.get_active_session_id() == session.session_id
    note = ledger.add_note("Checking earnings revision vs peers")
    assert note.type == "note"
    ledger.add_tag("idea")
    ended = ledger.end_session(tags=["followup"])
    assert ended.status == "closed"
    assert set(ended.tags) >= {"idea", "followup"}
    assert ledger.get_active_session_id() is None
    events = ledger.list_events(session_id=session.session_id, limit=50)
    types = {e["type"] for e in events}
    assert "session_start" in types
    assert "note" in types
    assert "session_end" in types


def test_restricted_never_egresses():
    assert not sensitivity_allows_egress(Sensitivity.RESTRICTED, Sensitivity.INTERNAL)
    assert sensitivity_allows_egress(Sensitivity.INTERNAL, Sensitivity.INTERNAL)
    assert not sensitivity_allows_egress(Sensitivity.CONFIDENTIAL, Sensitivity.INTERNAL)


def test_redact_and_filter(ledger: Ledger):
    ledger.start_session("test")
    ledger.add_note("Ping client@example.com about acct 12345678")
    ledger.append_event(
        __import__("analyst_ledger.schema", fromlist=["Event"]).Event(
            type="note",
            surface="notes",
            session_id=ledger.get_active_session_id(),
            sensitivity=Sensitivity.RESTRICTED.value,
            payload={"text": "MNPI deal room notes"},
        )
    )
    events = ledger.list_events(session_id=ledger.get_active_session_id(), limit=20)
    events.reverse()
    filtered = filter_events_for_egress(events, Sensitivity.INTERNAL)
    texts = [e["payload"].get("text", "") for e in filtered if e["type"] == "note"]
    assert any("[REDACTED]" in t for t in texts)
    assert not any("MNPI" in t for t in texts)
    assert "[REDACTED]" in redact_text("email me at a@b.co")


def test_synthesis_dry_run_and_stub(ledger: Ledger):
    session = ledger.start_session("NVDA block")
    ledger.add_note("Volume spike into close; want relative strength vs QQQ")
    dry = run_synthesis(ledger, session.session_id, "Draft memo", dry_run=True)
    assert dry["status"] == "dry_run"
    assert dry["prompt_chars"] > 50
    stub = run_synthesis(
        ledger,
        session.session_id,
        "Draft memo",
        destination="local_stub",
    )
    assert stub["status"] == "ok"
    assert "Research memo" in (stub.get("output") or "")
    summary = ledger.summary()
    assert summary["egress_audits"] >= 2


def test_feedback_sft_export(ledger: Ledger, tmp_path):
    session = ledger.start_session("SFT source")
    ledger.add_note("Thesis: margin expansion underappreciated")
    result = run_synthesis(
        ledger, session.session_id, "Draft", destination="local_stub"
    )
    ledger.add_feedback(
        "accept",
        session_id=session.session_id,
        synthesis_event_id=result["event_id"],
    )
    pairs = build_pairs(ledger)
    assert len(pairs) == 1
    assert pairs[0]["messages"][0]["role"] == "user"
    assert pairs[0]["meta"]["reward_family"] == "analyst_process"
    out = export_pairs(out_path=tmp_path / "pairs.jsonl")
    assert out.exists()
    line = out.read_text(encoding="utf-8").strip().splitlines()[0]
    assert json.loads(line)["meta"]["label"] == "accept"


def test_inbox_watcher(ledger: Ledger, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("ANALYST_INBOX", str(inbox))
    f = inbox / "clip.md"
    f.write_text("# note\nhello", encoding="utf-8")
    from analyst_ledger.inbox_watcher import scan_once

    n = scan_once(ledger)
    assert n == 1
    assert scan_once(ledger) == 0  # idempotent
    types = {e["type"] for e in ledger.list_events(limit=20)}
    assert "inbox_file" in types


def test_artifact_attach(ledger: Ledger, tmp_path):
    session = ledger.start_session("charts")
    chart = tmp_path / "nvda.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    art = ledger.attach_artifact(chart)
    assert art.sha256
    assert Path(art.path).exists()


def test_build_synthesis_prompt_structure(ledger: Ledger):
    session = ledger.start_session("x")
    ledger.add_note("check RSI divergence")
    ctx = ledger.session_context_for_synthesis(session.session_id)
    prompt = build_synthesis_prompt(ctx, "Draft memo")
    assert "buy-side research analyst" in prompt
    assert "check RSI divergence" in prompt
