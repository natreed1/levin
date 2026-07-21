"""Session-level topical labels: persistence, validation, and migration."""

from __future__ import annotations

import sqlite3

import pytest

from analyst_ledger.labels import LabelError
from analyst_ledger.ledger import Ledger
from analyst_ledger.paths import sqlite_path


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ANALYST_INBOX", str(tmp_path / "inbox"))
    return Ledger()


def test_add_labels_persists_and_records_event(ledger: Ledger):
    s = ledger.start_session("NVDA earnings", surface="notes")
    ledger.add_labels(["topic:Semiconductors", "project:Q2 Earnings"])
    got = ledger.get_session(s.session_id)
    assert got.labels == ["project:q2-earnings", "topic:semiconductors"]

    types = {e["type"] for e in ledger.list_events(session_id=s.session_id, limit=50)}
    assert "label" in types

    listed = {x["session_id"]: x for x in ledger.list_sessions()}[s.session_id]
    assert "topic:semiconductors" in listed["labels"]


def test_add_labels_merges_and_dedupes(ledger: Ledger):
    s = ledger.start_session("x")
    ledger.add_labels(["topic:earnings"])
    ledger.add_labels(["topic:earnings", "entity:Acme AI"])
    got = ledger.get_session(s.session_id)
    assert got.labels == ["entity:acme-ai", "topic:earnings"]


def test_add_labels_rejects_unknown_controlled(ledger: Ledger):
    ledger.start_session("x")
    with pytest.raises(LabelError):
        ledger.add_labels(["topic:not-a-real-theme"])


def test_migration_adds_column_to_pre_labels_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    p = sqlite_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Simulate a ledger created before the labels column existed.
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE sessions ("
        "session_id TEXT PRIMARY KEY, title TEXT NOT NULL, surface TEXT NOT NULL, "
        "sensitivity TEXT NOT NULL, desk_tag TEXT, started_at TEXT NOT NULL, "
        "ended_at TEXT, tags_json TEXT NOT NULL DEFAULT '[]', "
        "status TEXT NOT NULL DEFAULT 'open')"
    )
    conn.commit()
    conn.close()

    led = Ledger()  # opening should ALTER-add labels_json without error
    with led._connect() as check:
        cols = {r["name"] for r in check.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "labels_json" in cols
