"""Tests for the Claude reviewer pipeline (stub destination)."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from analyst_ledger.ledger import Ledger
from analyst_ledger.review import (
    build_review_prompt,
    gather_review_context,
    list_reviews,
    run_review,
)
from analyst_ledger.schema import Event, Sensitivity


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Ledger()


def _seed(ledger: Ledger) -> None:
    s = ledger.start_session("NVDA morning look", surface="browser")
    ledger.add_note("checking margin trend vs peers")
    ledger.add_note("compare NVDA capex guidance vs hyperscalers")
    ledger.add_note("client@example.com asked about NVDA")  # should be redacted
    ledger.append_event(
        Event(
            type="note",
            surface="notes",
            session_id=s.session_id,
            sensitivity=Sensitivity.RESTRICTED.value,
            payload={"text": "MNPI deal room content"},
        )
    )
    ledger.end_session(session_id=s.session_id, tags=["followup"])


def test_context_and_prompt_exclude_sensitive(ledger: Ledger):
    _seed(ledger)
    context = gather_review_context(ledger, days=7)
    blob = json.dumps(context)
    assert "MNPI" not in blob
    assert "client@example.com" not in blob
    assert "[REDACTED]" in blob
    prompt = build_review_prompt(context)
    assert "MNPI" not in prompt
    assert "STRICT JSON" in prompt


def test_run_review_stub_writes_memo_and_proposals(ledger: Ledger):
    _seed(ledger)
    result = run_review(ledger, days=7)
    assert result["status"] == "ok"
    assert result["destination"] == "local_stub"
    memo = Path(result["memo_path"])
    assert memo.exists()
    assert "MNPI" not in memo.read_text(encoding="utf-8")
    # 3+ notes and no digest automation → weekly_note_digest proposal
    rids = [p["ritual_id"] for p in result["proposals_written"]]
    assert "weekly_note_digest" in rids
    spec = json.loads(
        (Path(result["proposals_written"][0]["spec_path"])).read_text(encoding="utf-8")
    )
    assert spec["approved"] is False
    assert spec["proposed_by"] == "claude_review"
    assert list_reviews()[0]["path"] == str(memo)
    # Egress audited + review_run event logged
    assert ledger.summary()["egress_audits"] >= 1
    assert any(
        e["type"] == "review_run" for e in ledger.list_events(limit=20)
    )


def test_review_never_overwrites_approved_spec(ledger: Ledger, tmp_path):
    from analyst_ledger.paths import ritual_specs_dir

    _seed(ledger)
    human_spec = {
        "name": "weekly_note_digest",
        "approved": True,
        "runner": "note_digest",
        "watchlist": ["KEEPME"],
    }
    path = ritual_specs_dir() / "weekly_note_digest.json"
    path.write_text(json.dumps(human_spec), encoding="utf-8")

    run_review(ledger, days=7)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["approved"] is True
    assert after["watchlist"] == ["KEEPME"]


def test_review_api_and_page(ledger: Ledger):
    from analyst_ledger.dashboard import make_app

    _seed(ledger)
    app = make_app(ledger)

    def call(method, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_TYPE": "application/json",
            "wsgi.input": BytesIO(raw),
            "CONTENT_LENGTH": str(len(raw)),
        }
        status = []
        out = b"".join(app(environ, lambda s, _h: status.append(s)))
        return status[0], out

    st, out = call("POST", "/api/review/run", {"days": 7})
    assert st.startswith("200")
    assert json.loads(out.decode())["status"] == "ok"

    st, out = call("GET", "/review")
    html = out.decode()
    assert st.startswith("200")
    assert "Run review now" in html
    assert "weekly_note_digest" in html  # proposal shows with Approve button
    assert "approve-btn" in html
