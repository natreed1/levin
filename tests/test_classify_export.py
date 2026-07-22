import json

import pytest

from analyst_ledger.classify_export import export_kind_pairs
from analyst_ledger.ledger import Ledger
from analyst_ledger.schema import Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    return Ledger()


def test_export_kind_pairs(ledger: Ledger, tmp_path):
    thread = ledger.start_background_session(
        title="M",
        surface=Surface.CHAT.value,
        sensitivity=Sensitivity.INTERNAL.value,
        desk_tag="chat:master",
    )
    msg = ledger.append_chat_message(
        thread.session_id, role="user", content="fix the dashboard bug", kind="message"
    )
    ledger.correct_message_kind(thread.session_id, msg.event_id, "build")

    out = export_kind_pairs(ledger, out_path=tmp_path / "pairs.jsonl")
    assert out.exists()
    line = json.loads(out.read_text(encoding="utf-8").strip().splitlines()[0])
    assert line["kind"] == "build"
    assert line["text"] == "fix the dashboard bug"
