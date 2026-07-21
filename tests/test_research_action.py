import pytest

import analyst_ledger.research_action as ra
from analyst_ledger.actionable import detect_actionable
from analyst_ledger.ledger import Ledger
from analyst_ledger.schema import Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    return Ledger()


def _chat_thread(ledger, sensitivity=Sensitivity.INTERNAL.value):
    return ledger.start_background_session(
        title="Master",
        surface=Surface.CHAT.value,
        sensitivity=sensitivity,
        desk_tag="chat:master",
    )


def _last_message(ledger, sid):
    return ledger.list_chat_messages(sid)[-1]


def test_stub_posts_unapproved_draft(ledger: Ledger):
    thread = _chat_thread(ledger)
    d = detect_actionable("look into Acme AI")
    assert d.matched
    res = ra.execute_research_draft(ledger, thread.session_id, d, stub=True)
    assert res["status"] == "ok"
    assert res["web_used"] is False

    payload = _last_message(ledger, thread.session_id)["payload"]
    assert payload["kind"] == "research_draft"
    meta = payload["metadata"]
    assert meta["approved"] is False
    assert "state:done" in meta["labels"]
    assert "entity:acme-ai" in meta["labels"]
    assert "intent:research" in meta["labels"]


def test_live_search_monkeypatched(ledger: Ledger, monkeypatch):
    thread = _chat_thread(ledger)
    canned = [
        {"title": "Acme AI raises seed", "url": "https://reuters.com/x", "snippet": "..."}
    ]
    monkeypatch.setattr(ra, "_gather_sources", lambda decision, symbol=None: (canned, ["q"]))
    d = detect_actionable("we should look into Acme AI")
    res = ra.execute_research_draft(ledger, thread.session_id, d, stub=False)
    assert res["web_used"] is True
    assert res["source_count"] == 1

    payload = _last_message(ledger, thread.session_id)["payload"]
    assert "reuters.com/x" in payload["content"]
    assert payload["metadata"]["sources"][0]["url"] == "https://reuters.com/x"


def test_sensitivity_gate_skips_web(ledger: Ledger, monkeypatch):
    thread = _chat_thread(ledger, sensitivity=Sensitivity.CONFIDENTIAL.value)
    monkeypatch.setattr(
        ra, "_gather_sources", lambda decision, symbol=None: ([{"title": "x", "url": "u"}], ["q"])
    )
    d = detect_actionable("look into Acme AI")
    res = ra.execute_research_draft(ledger, thread.session_id, d, stub=False)
    assert res["web_used"] is False
    assert res["source_count"] == 0
