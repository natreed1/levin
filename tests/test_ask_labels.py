"""Framework capture: an actionable ask is tagged, not acted on."""

import pytest

from analyst_ledger.actionable import detect_actionable
from analyst_ledger.ledger import Ledger
from analyst_ledger.schema import Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    return Ledger()


def _chat_thread(ledger):
    return ledger.start_background_session(
        title="Master",
        surface=Surface.CHAT.value,
        sensitivity=Sensitivity.INTERNAL.value,
        desk_tag="chat:master",
    )


def test_record_ask_labels_tags_without_acting(ledger: Ledger):
    thread = _chat_thread(ledger)
    decision = detect_actionable("look into Acme AI")
    assert decision.matched

    event = ledger.record_ask_labels(
        thread.session_id,
        decision.labels,
        source="chat_actionable",
        meta={"actionable": decision.public()},
    )
    assert event.type == "label"

    # A label event with the open-ask signal exists.
    labels_events = [
        e
        for e in ledger.list_events(session_id=thread.session_id, limit=50)
        if e["type"] == "label"
    ]
    assert labels_events
    payload = labels_events[0]["payload"]
    assert "intent:research" in payload["labels"]
    assert "entity:acme-ai" in payload["labels"]
    assert "state:open" in payload["labels"]  # nothing acted -> stays open
    assert payload["source"] == "chat_actionable"

    # The session's own topical labels are untouched (this is a message-level ask).
    assert ledger.get_session(thread.session_id).labels == []

    # No research draft was posted — the agent did not act.
    msgs = ledger.list_chat_messages(thread.session_id)
    assert all(m["payload"].get("kind") != "research_draft" for m in msgs)
