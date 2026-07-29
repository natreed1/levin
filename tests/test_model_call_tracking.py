"""Token-usage tracking: record_model_call + token_usage_summary."""

import pytest

from analyst_ledger.labels import LabelError
from analyst_ledger.ledger import Ledger
from analyst_ledger.schema import Event, Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    return Ledger()


def test_record_model_call_writes_fact(ledger: Ledger):
    ev = ledger.record_model_call(
        call_site="graph-layer",
        model="claude-sonnet-5",
        tokens_in=120,
        tokens_out=340,
        latency_ms=850,
        room_id="room-1",
        agent_id="agent-1",
        user_id="user-1",
    )
    assert ev.type == "model_call"
    assert ev.payload["call_site"] == "graph-layer"
    assert ev.payload["model"] == "claude-sonnet-5"
    assert ev.payload["tokens_in"] == 120
    assert ev.payload["tokens_out"] == 340
    assert ev.payload["latency_ms"] == 850
    assert ev.payload["room_id"] == "room-1"
    assert ev.payload["agent_id"] == "agent-1"
    assert ev.payload["user_id"] == "user-1"


def test_record_model_call_rejects_unknown_call_site(ledger: Ledger):
    with pytest.raises(LabelError):
        ledger.record_model_call(call_site="specialist-room")


def test_record_model_call_room_id_never_becomes_session_id(ledger: Ledger):
    """room_id is a messenger concept; session_id is FK-enforced. A bogus
    room_id must never cause a session FK violation."""
    ev = ledger.record_model_call(call_site="team-orchestrator", room_id="not-a-real-session")
    assert ev.session_id is None
    assert ev.payload["room_id"] == "not-a-real-session"


def test_token_usage_summary_groups_by_call_site(ledger: Ledger):
    ledger.record_model_call(call_site="graph-layer", tokens_in=100, tokens_out=50)
    ledger.record_model_call(call_site="graph-layer", tokens_in=200, tokens_out=100)
    ledger.record_model_call(call_site="team-orchestrator", tokens_in=10, tokens_out=5)

    rows = ledger.token_usage_summary(group_by="call_site")
    by_key = {r["key"]: r for r in rows}
    assert by_key["graph-layer"]["call_count"] == 2
    assert by_key["graph-layer"]["tokens_in"] == 300
    assert by_key["graph-layer"]["tokens_out"] == 150
    assert by_key["team-orchestrator"]["call_count"] == 1
    # Sorted descending by total tokens — biggest spender first.
    assert rows[0]["key"] == "graph-layer"


def test_token_usage_summary_groups_by_model_and_room(ledger: Ledger):
    ledger.record_model_call(
        call_site="graph-layer", model="qwen3:8b", room_id="room-a",
        tokens_in=10, tokens_out=10,
    )
    ledger.record_model_call(
        call_site="graph-layer", model="claude-sonnet-5", room_id="room-b",
        tokens_in=20, tokens_out=20,
    )

    by_model = {r["key"]: r for r in ledger.token_usage_summary(group_by="model")}
    assert by_model["qwen3:8b"]["call_count"] == 1
    assert by_model["claude-sonnet-5"]["call_count"] == 1

    by_room = {r["key"]: r for r in ledger.token_usage_summary(group_by="room_id")}
    assert by_room["room-a"]["tokens_in"] == 10
    assert by_room["room-b"]["tokens_in"] == 20


def test_token_usage_summary_rejects_bad_group_by(ledger: Ledger):
    with pytest.raises(ValueError):
        ledger.token_usage_summary(group_by="agent_id")


def test_token_usage_summary_days_filter(ledger: Ledger):
    old = Event(
        type="model_call",
        surface=Surface.CHAT.value,
        sensitivity=Sensitivity.INTERNAL.value,
        payload={
            "call_site": "graph-layer",
            "tokens_in": 999,
            "tokens_out": 999,
        },
        ts="2020-01-01T00:00:00+00:00",
    )
    ledger.append_event(old)
    ledger.record_model_call(call_site="graph-layer", tokens_in=1, tokens_out=1)

    rows = ledger.token_usage_summary(days=7, group_by="call_site")
    assert rows[0]["tokens_in"] == 1
    assert rows[0]["call_count"] == 1

    rows_all = ledger.token_usage_summary(group_by="call_site")
    assert rows_all[0]["call_count"] == 2
