"""Human corrections feed the training loop."""

import pytest

from analyst_ledger.labels import LabelError
from analyst_ledger.ledger import Ledger
from analyst_ledger.schema import Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    return Ledger()


def _thread(ledger):
    return ledger.start_background_session(
        title="Master",
        surface=Surface.CHAT.value,
        sensitivity=Sensitivity.INTERNAL.value,
        desk_tag="chat:master",
    )


def test_human_correction_supersedes_auto(ledger: Ledger):
    thread = _thread(ledger)
    msg = ledger.append_chat_message(
        thread.session_id, role="user", content="look into NVDA", kind="message"
    )
    # auto tag first
    ledger.record_ask_labels(
        thread.session_id,
        ["kind:research"],
        source="chat_classify",
        meta={"target_event_id": msg.event_id},
    )
    assert ledger.latest_kind_for(msg.event_id) == "research"

    # human corrects to build
    ledger.correct_message_kind(
        thread.session_id, msg.event_id, "build", auto_kind="research"
    )
    assert ledger.latest_kind_for(msg.event_id) == "build"

    feedback = [
        e
        for e in ledger.list_events(session_id=thread.session_id, limit=50)
        if e["type"] == "label_feedback"
    ]
    assert feedback
    assert feedback[0]["payload"]["auto_kind"] == "research"
    assert feedback[0]["payload"]["corrected_kind"] == "build"


def test_confirmed_examples_collects_corrections(ledger: Ledger):
    thread = _thread(ledger)
    msg = ledger.append_chat_message(
        thread.session_id, role="user", content="the sync broke", kind="message"
    )
    ledger.correct_message_kind(thread.session_id, msg.event_id, "build")
    examples = ledger.confirmed_kind_examples(limit=10)
    assert {"text": "the sync broke", "kind": "build"} in examples


def test_correction_rejects_unknown_kind(ledger: Ledger):
    thread = _thread(ledger)
    msg = ledger.append_chat_message(
        thread.session_id, role="user", content="x", kind="message"
    )
    with pytest.raises(LabelError):
        ledger.correct_message_kind(thread.session_id, msg.event_id, "not-a-kind")


def test_api_label_correct(ledger: Ledger):
    from analyst_ledger.dashboard import _api_label_correct

    thread = _thread(ledger)
    msg = ledger.append_chat_message(
        thread.session_id, role="user", content="look into NVDA", kind="message"
    )
    res = _api_label_correct(
        ledger,
        {"session_id": thread.session_id, "event_id": msg.event_id, "kind": "build"},
    )
    assert res["ok"] is True
    assert res["kind"] == "build"

    with pytest.raises(Exception):
        _api_label_correct(
            ledger,
            {"session_id": thread.session_id, "event_id": msg.event_id, "kind": "bogus"},
        )
