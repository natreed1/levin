"""Tests for chat mining: gap detection, clustering, proposals, review integration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

from analyst_ledger.chat_mining import (
    chat_context,
    cluster_asks,
    gather_chat_asks,
    proposals_from_clusters,
)
from analyst_ledger.ledger import Ledger
from analyst_ledger.review import run_review
from analyst_ledger.schema import Event, Sensitivity


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANALYST_MESSENGER_URL", raising=False)
    monkeypatch.delenv("ANALYST_MESSENGER_INVITE", raising=False)
    return Ledger()


def _ts(days_ago: int, hour: int = 12, minute: int = 0) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(days=days_ago))
        .replace(hour=hour, minute=minute, second=0, microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def _msg(
    ledger: Ledger,
    sid: str,
    role: str,
    content: str,
    kind: str = "message",
    ts: str = None,
    sensitivity: str = Sensitivity.INTERNAL.value,
):
    kwargs = {"ts": ts} if ts else {}
    ledger.append_event(
        Event(
            type="chat_message",
            surface="chat",
            session_id=sid,
            sensitivity=sensitivity,
            payload={"role": role, "content": content, "kind": kind, "metadata": {}},
            **kwargs,
        )
    )


def _seed_gap_asks(ledger: Ledger, sid: str, text: str = "any TSLA filings?", n: int = 3):
    """n asks across n distinct days, each answered by the model (synthesis)."""
    for day in range(n, 0, -1):
        _msg(ledger, sid, "user", text, ts=_ts(day, hour=9))
        _msg(ledger, sid, "assistant", "Started research", kind="status", ts=_ts(day, hour=9, minute=1))
        _msg(ledger, sid, "assistant", "model answer here", kind="synthesis", ts=_ts(day, hour=9, minute=2))


def test_outcome_classification(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _msg(ledger, sid, "user", "modeled ask", ts=_ts(1, 8))
    _msg(ledger, sid, "assistant", "reply", kind="synthesis", ts=_ts(1, 8, 1))
    _msg(ledger, sid, "user", "routed ask", ts=_ts(1, 9))
    _msg(ledger, sid, "assistant", "ran it", kind="routed_run", ts=_ts(1, 9, 1))
    _msg(ledger, sid, "user", "file ask", ts=_ts(1, 10))
    _msg(ledger, sid, "assistant", "found it", kind="file_search", ts=_ts(1, 10, 1))
    _msg(ledger, sid, "user", "status only ask", ts=_ts(1, 11))
    _msg(ledger, sid, "assistant", "Started", kind="status", ts=_ts(1, 11, 1))
    _msg(ledger, sid, "user", "errored ask", ts=_ts(1, 12))
    _msg(ledger, sid, "system", "failed", kind="error", ts=_ts(1, 12, 1))

    asks, friend = gather_chat_asks(ledger, days=7)
    assert friend is False
    outcomes = {a.text: a.outcome for a in asks}
    assert outcomes["modeled ask"] == "modeled"
    assert outcomes["routed ask"] == "routed"
    assert outcomes["file ask"] == "file_search"
    assert outcomes["status only ask"] == "unanswered"
    assert outcomes["errored ask"] == "unanswered"


def test_window_and_restricted_excluded(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _msg(ledger, sid, "user", "ancient ask", ts=_ts(30))
    _msg(ledger, sid, "user", "MNPI secret ask", ts=_ts(1), sensitivity=Sensitivity.RESTRICTED.value)
    _msg(ledger, sid, "user", "fresh ask", ts=_ts(1))
    context = chat_context(ledger, days=7)
    blob = json.dumps(context)
    assert "ancient ask" not in blob
    assert "MNPI" not in blob
    assert context["ask_count"] == 1


def test_cluster_repeated_gap_ask(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _seed_gap_asks(ledger, sid, n=3)
    asks, _ = gather_chat_asks(ledger, days=7)
    clusters = cluster_asks(asks)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.count == 3
    assert c.day_count == 3
    assert c.symbols == ("TSLA",)
    assert c.modeled_count == 3
    assert c.routed_count == 0


def test_symbol_intent_rule_merges_paraphrases(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    # Low token overlap, same symbols + same intent -> merge
    _msg(ledger, sid, "user", "TSLA price please", ts=_ts(2, 9))
    _msg(ledger, sid, "assistant", "r", kind="synthesis", ts=_ts(2, 9, 1))
    _msg(ledger, sid, "user", "current quote TSLA today", ts=_ts(1, 9))
    _msg(ledger, sid, "assistant", "r", kind="synthesis", ts=_ts(1, 9, 1))
    # Same symbol, different intent -> separate cluster
    _msg(ledger, sid, "user", "TSLA filing documents overview", ts=_ts(1, 10))
    _msg(ledger, sid, "assistant", "r", kind="synthesis", ts=_ts(1, 10, 1))
    asks, _ = gather_chat_asks(ledger, days=7)
    clusters = cluster_asks(asks)
    sizes = sorted(c.count for c in clusters)
    assert sizes == [1, 2]


def test_proposal_shape_from_gap_cluster(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _seed_gap_asks(ledger, sid, n=3)
    context = chat_context(ledger, days=7)
    props = proposals_from_clusters(context["clusters"], [])
    assert len(props) == 1
    p = props[0]
    assert p["ritual_id"] == "chat_filings_tsla"
    assert p["runner"] == "sec_filings_check"
    assert p["watchlist"] == ["TSLA"]
    assert p["source"] == "chat_mining"
    assert "3x over 3 day(s)" in p["rationale"]


def test_routed_traffic_not_proposed(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    for day in range(3, 0, -1):
        _msg(ledger, sid, "user", "any TSLA filings?", ts=_ts(day, 9))
        _msg(ledger, sid, "assistant", "ran", kind="routed_run", ts=_ts(day, 9, 1))
    context = chat_context(ledger, days=7)
    assert proposals_from_clusters(context["clusters"], []) == []


def test_thresholds(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    # Only 2 asks -> below MIN_COUNT
    for day in (2, 1):
        _msg(ledger, sid, "user", "any NVDA filings?", ts=_ts(day, 9))
        _msg(ledger, sid, "assistant", "r", kind="synthesis", ts=_ts(day, 9, 1))
    context = chat_context(ledger, days=7)
    assert proposals_from_clusters(context["clusters"], []) == []

    # 3 asks all on the same day -> below MIN_DAYS
    sid2 = ledger.get_or_create_chat_thread(ritual_id="sameday").session_id
    for hour in (9, 10, 11):
        _msg(ledger, sid2, "user", "any AMD filings?", ts=_ts(1, hour))
        _msg(ledger, sid2, "assistant", "r", kind="synthesis", ts=_ts(1, hour, 1))
    context2 = chat_context(ledger, days=7)
    rids = [p["ritual_id"] for p in proposals_from_clusters(context2["clusters"], [])]
    assert "chat_filings_amd" not in rids


def test_existing_approved_automation_suppresses(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _seed_gap_asks(ledger, sid, n=3)
    context = chat_context(ledger, days=7)
    automations = [
        {"approved": True, "runner": "sec_filings_check", "watchlist": ["TSLA", "NVDA"]}
    ]
    assert proposals_from_clusters(context["clusters"], automations) == []


def _friend_msg(content: str, days_ago: int, msg_id: int):
    return {
        "event_id": f"friend_{msg_id}",
        "ts": _ts(days_ago),
        "type": "chat_message",
        "surface": "chat",
        "session_id": "friend",
        "sensitivity": "internal",
        "payload": {
            "role": "user",
            "content": content,
            "kind": "message",
            "metadata": {"friend": True, "author": "me", "messenger_id": msg_id},
        },
    }


def test_friend_asks_counted_not_gap(ledger: Ledger, monkeypatch):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    # 2 modeled dashboard asks + 1 friend ask -> mixed cluster proposes
    for day in (3, 2):
        _msg(ledger, sid, "user", "any TSLA filings?", ts=_ts(day, 9))
        _msg(ledger, sid, "assistant", "r", kind="synthesis", ts=_ts(day, 9, 1))
    monkeypatch.setattr(
        "analyst_ledger.messenger_bridge.messenger_configured", lambda: True
    )
    monkeypatch.setattr(
        "analyst_ledger.messenger_bridge.list_friend_messages",
        lambda limit=200: [_friend_msg("any TSLA filings?", 1, 1)],
    )
    asks, friend = gather_chat_asks(ledger, days=7)
    assert friend is True
    assert {a.source for a in asks} == {"dashboard", "friend"}
    assert all(a.outcome == "friend" for a in asks if a.source == "friend")

    context = chat_context(ledger, days=7)
    props = proposals_from_clusters(context["clusters"], [])
    assert [p["ritual_id"] for p in props] == ["chat_filings_tsla"]

    # Friend-only repetition never proposes (no modeled gap)
    monkeypatch.setattr(
        "analyst_ledger.messenger_bridge.list_friend_messages",
        lambda limit=200: [
            _friend_msg("weekly AMD filings check?", d, d) for d in (1, 2, 3)
        ],
    )
    ledger2_data = ledger  # same ledger; dashboard asks unchanged
    context2 = chat_context(ledger2_data, days=7)
    rids = [p["ritual_id"] for p in proposals_from_clusters(context2["clusters"], [])]
    assert "chat_filings_amd" not in rids


def test_friend_error_and_unconfigured(ledger: Ledger, monkeypatch):
    from analyst_ledger.messenger_bridge import MessengerBridgeError

    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _msg(ledger, sid, "user", "hello there", ts=_ts(1))

    # Configured but erroring -> graceful skip, dashboard asks intact
    monkeypatch.setattr(
        "analyst_ledger.messenger_bridge.messenger_configured", lambda: True
    )

    def _boom(limit=200):
        raise MessengerBridgeError("down", status=500)

    monkeypatch.setattr("analyst_ledger.messenger_bridge.list_friend_messages", _boom)
    asks, friend = gather_chat_asks(ledger, days=7)
    assert friend is False
    assert len(asks) == 1

    # Unconfigured -> bridge never called
    monkeypatch.setattr(
        "analyst_ledger.messenger_bridge.messenger_configured", lambda: False
    )

    def _never(limit=200):
        raise AssertionError("bridge called while unconfigured")

    monkeypatch.setattr("analyst_ledger.messenger_bridge.list_friend_messages", _never)
    asks2, friend2 = gather_chat_asks(ledger, days=7)
    assert friend2 is False and len(asks2) == 1


def test_run_review_stub_end_to_end(ledger: Ledger):
    from analyst_ledger.paths import ritual_specs_dir

    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _seed_gap_asks(ledger, sid, n=3)
    result = run_review(ledger, days=7)
    assert result["status"] == "ok"
    assert result["chat"]["gap_count"] >= 3
    assert "chat_filings_tsla" in result["chat"]["chat_proposals"]
    spec = json.loads(
        (ritual_specs_dir() / "chat_filings_tsla.json").read_text(encoding="utf-8")
    )
    assert spec["approved"] is False
    assert spec["proposed_by"] == "chat_mining"
    memo = Path(result["memo_path"]).read_text(encoding="utf-8")
    assert "## Chat asks" in memo
    assert "filings:TSLA" in memo


def test_approved_chat_spec_never_overwritten(ledger: Ledger):
    from analyst_ledger.paths import ritual_specs_dir

    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _seed_gap_asks(ledger, sid, n=3)
    run_review(ledger, days=7)
    path = ritual_specs_dir() / "chat_filings_tsla.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["approved"] = True
    spec["watchlist"] = ["KEEPME"]
    path.write_text(json.dumps(spec), encoding="utf-8")

    run_review(ledger, days=7)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["approved"] is True
    assert after["watchlist"] == ["KEEPME"]


def test_review_page_shows_chat_proposal(ledger: Ledger):
    from analyst_ledger.dashboard import make_app

    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _seed_gap_asks(ledger, sid, n=3)
    run_review(ledger, days=7)
    app = make_app(ledger)
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/review",
        "QUERY_STRING": "",
        "wsgi.input": BytesIO(b""),
        "CONTENT_LENGTH": "0",
    }
    status = []
    html = b"".join(app(environ, lambda s, _h: status.append(s))).decode()
    assert status[0].startswith("200")
    assert "chat_filings_tsla" in html
    assert "[chat]" in html
    assert "approve-btn" in html


def test_redaction(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    for day in (3, 2, 1):
        _msg(ledger, sid, "user", "email client@example.com the TSLA filings", ts=_ts(day, 9))
        _msg(ledger, sid, "assistant", "r", kind="synthesis", ts=_ts(day, 9, 1))
    context = chat_context(ledger, days=7)
    blob = json.dumps(context)
    assert "client@example.com" not in blob
    assert "[REDACTED]" in blob
    result = run_review(ledger, days=7)
    memo = Path(result["memo_path"]).read_text(encoding="utf-8")
    assert "client@example.com" not in memo


def test_chat_mining_failure_never_breaks_review(ledger: Ledger, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("mining exploded")

    monkeypatch.setattr("analyst_ledger.chat_mining.chat_context", _boom)
    result = run_review(ledger, days=7)
    assert result["status"] == "ok"
    assert result["chat"]["ask_count"] == 0


def test_determinism(ledger: Ledger):
    sid = ledger.get_or_create_chat_thread(master=True).session_id
    _seed_gap_asks(ledger, sid, n=3)
    a = json.dumps(chat_context(ledger, days=7), sort_keys=True)
    b = json.dumps(chat_context(ledger, days=7), sort_keys=True)
    assert a == b
