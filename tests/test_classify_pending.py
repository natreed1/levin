"""Background sweep: classify captured user messages that lack a kind label."""

import pytest

from analyst_ledger.classify import classify_pending
from analyst_ledger.ledger import Ledger
from analyst_ledger.schema import Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("ANALYST_CLASSIFY_QWEN", "off")  # deterministic-only in tests
    return Ledger()


def _thread(ledger):
    return ledger.start_background_session(
        title="Master",
        surface=Surface.CHAT.value,
        sensitivity=Sensitivity.INTERNAL.value,
        desk_tag="chat:master",
    )


def test_classify_pending_tags_and_is_idempotent(ledger: Ledger):
    thread = _thread(ledger)
    msg = ledger.append_chat_message(
        thread.session_id, role="user", content="look into NVDA earnings", kind="message"
    )

    first = classify_pending(ledger)
    assert first["classified"] == 1

    labels = [
        e
        for e in ledger.list_events(session_id=thread.session_id, limit=50)
        if e["type"] == "label"
    ]
    assert labels
    payload = labels[0]["payload"]
    assert any(str(lbl).startswith("kind:") for lbl in payload["labels"])
    assert payload["target_event_id"] == msg.event_id

    # Re-running does nothing — the message already has a kind.
    second = classify_pending(ledger)
    assert second["classified"] == 0


def test_classify_pending_skips_assistant_and_smalltalk(ledger: Ledger):
    thread = _thread(ledger)
    ledger.append_chat_message(
        thread.session_id, role="assistant", content="look into NVDA", kind="message"
    )  # assistant -> skipped
    ledger.append_chat_message(
        thread.session_id, role="user", content="haha nice one", kind="message"
    )  # small talk -> no kind (deterministic off) -> not recorded

    result = classify_pending(ledger)
    assert result["classified"] == 0
