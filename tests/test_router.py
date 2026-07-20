"""Tests for the deterministic chat router (no model, no network)."""

from __future__ import annotations

import json
import time
from io import BytesIO

import pytest

from analyst_ledger.ledger import Ledger
from analyst_ledger.paths import ritual_specs_dir
from analyst_ledger.router import (
    build_route_index,
    route_message,
    router_enabled,
)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("ANALYST_CHAT_ROUTER", raising=False)
    return Ledger()


def _spec(
    ritual_id: str,
    runner: str,
    watchlist=None,
    approved: bool = True,
    enabled: bool = True,
) -> None:
    path = ritual_specs_dir() / f"{ritual_id}.json"
    path.write_text(
        json.dumps(
            {
                "name": ritual_id,
                "version": 1,
                "approved": approved,
                "enabled": enabled,
                "runner": runner,
                "watchlist": watchlist or [],
                "steps": [{"draft_note": "t"}],
            }
        ),
        encoding="utf-8",
    )


def test_clear_match_filings_ticker(ledger: Ledger):
    _spec("sec_filings_check", "sec_filings_check", watchlist=["TSLA"])
    _spec("weekly_note_digest", "note_digest")
    decision = route_message("any TSLA filings?")
    assert decision.matched
    assert decision.ritual_id == "sec_filings_check"
    assert decision.runner == "sec_filings_check"
    assert decision.watchlist_override is None
    assert any("ticker TSLA" in r for r in decision.reasons)
    assert decision.score >= 5.0


def test_ticker_override(ledger: Ledger):
    _spec("sec_filings_check", "sec_filings_check", watchlist=["NVDA"])
    decision = route_message("any TSLA filings?")
    assert decision.matched
    assert decision.watchlist_override == ["TSLA"]


def test_company_alias_match_offline(ledger: Ledger, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("network call attempted during routing")

    monkeypatch.setattr("analyst_ledger.web_search._get_json", _boom)
    monkeypatch.setattr("analyst_ledger.finance_research._get_json", _boom)
    _spec("sec_filings_check", "sec_filings_check", watchlist=["TSLA"])
    decision = route_message("anything from tesla filings today?")
    assert decision.matched
    assert decision.ritual_id == "sec_filings_check"
    assert any("alias TSLA" in r or "company alias TSLA" in r for r in decision.reasons)


def test_ambiguous_and_low_score_no_match(ledger: Ledger):
    # Two identically-shaped specs -> equal scores -> margin blocks the match
    _spec("morning_scan_a", "morning_yf_scan", watchlist=["NVDA"])
    _spec("morning_scan_b", "morning_yf_scan", watchlist=["NVDA"])
    tie = route_message("morning scan please")
    assert not tie.matched
    assert tie.score == tie.runner_up_score

    low = route_message("what is going on there")
    assert not low.matched
    assert low.score < 5.0


def test_index_excludes_unapproved_disabled_unknown(ledger: Ledger):
    _spec("ok_scan", "morning_yf_scan", approved=True)
    _spec("draft_scan", "morning_yf_scan", approved=False)
    _spec("off_scan", "morning_yf_scan", approved=True, enabled=False)
    _spec("weird", "shell_exec", approved=True)
    rids = {e.ritual_id for e in build_route_index()}
    assert rids == {"ok_scan"}


def test_restrict_to_other_ritual_never_hijacks(ledger: Ledger):
    _spec("sec_filings_check", "sec_filings_check", watchlist=["TSLA"])
    _spec("weekly_note_digest", "note_digest")
    decision = route_message("any TSLA filings?", restrict_to="weekly_note_digest")
    assert not decision.matched or decision.ritual_id == "weekly_note_digest"
    assert decision.ritual_id != "sec_filings_check"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("ANALYST_CHAT_ROUTER", "off")
    assert not router_enabled()
    monkeypatch.setenv("ANALYST_CHAT_ROUTER", "on")
    assert router_enabled()


# --- API integration ---------------------------------------------------------


def _call(app, method: str, path: str, body=None):
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
    return status[0], json.loads(out.decode()) if out else {}


def _wait_for_routed_reply(ledger: Ledger, thread_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for msg in ledger.list_chat_messages(thread_id):
            payload = msg.get("payload") or {}
            if payload.get("kind") == "routed_run":
                return payload
        time.sleep(0.05)
    return None


def test_chat_message_routes_deterministically(ledger: Ledger):
    from analyst_ledger.dashboard import make_app

    _spec("sec_filings_check", "sec_filings_check", watchlist=["TSLA"])
    thread = ledger.get_or_create_chat_thread(master=True)
    app = make_app(ledger)

    st, job = _call(
        app,
        "POST",
        "/api/chats/message",
        {"thread_id": thread.session_id, "content": "any TSLA filings?", "stub": True},
    )
    assert st.startswith("200")
    assert job["kind"] == "workflow_run"
    assert job["key"] == "workflow:sec_filings_check"

    payload = _wait_for_routed_reply(ledger, thread.session_id)
    assert payload is not None, "routed reply never appeared in thread"
    assert payload["role"] == "assistant"
    assert payload["content"].startswith("Routed deterministically")
    meta = payload.get("metadata") or {}
    assert meta["router"]["matched"] is True
    assert meta["ritual_id"] == "sec_filings_check"
    run_sid = meta.get("run_session_id")
    assert run_sid and run_sid != thread.session_id
    run_events = {
        e["type"] for e in ledger.list_events(session_id=run_sid, limit=50)
    }
    assert "ritual_run" in run_events


def test_chat_message_falls_through_to_model_path(ledger: Ledger):
    from analyst_ledger.dashboard import make_app

    _spec("sec_filings_check", "sec_filings_check", watchlist=["TSLA"])
    thread = ledger.get_or_create_chat_thread(master=True)
    app = make_app(ledger)

    st, job = _call(
        app,
        "POST",
        "/api/chats/message",
        {"thread_id": thread.session_id, "content": "tell me a joke about macro"},
    )
    assert st.startswith("200")
    assert job["key"] == "chat:master"
    assert job["kind"] == "master_chat"
    time.sleep(0.2)
    kinds = {
        (m.get("payload") or {}).get("kind")
        for m in ledger.list_chat_messages(thread.session_id)
    }
    assert "routed_run" not in kinds


def test_kill_switch_api(ledger: Ledger, monkeypatch):
    from analyst_ledger.dashboard import make_app

    monkeypatch.setenv("ANALYST_CHAT_ROUTER", "off")
    _spec("sec_filings_check", "sec_filings_check", watchlist=["TSLA"])
    thread = ledger.get_or_create_chat_thread(master=True)
    app = make_app(ledger)

    st, job = _call(
        app,
        "POST",
        "/api/chats/message",
        {"thread_id": thread.session_id, "content": "any TSLA filings?"},
    )
    assert st.startswith("200")
    assert job["key"] == "chat:master"
