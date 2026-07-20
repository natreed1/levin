"""Tests for the local file finder (deterministic, roots-gated, Qwen-only summaries)."""

from __future__ import annotations

import json
import os
import time
from io import BytesIO
from pathlib import Path

import pytest

from analyst_ledger.file_search import (
    build_query,
    execute_file_search,
    match_file_request,
    search_files,
)
from analyst_ledger.ledger import Ledger
from analyst_ledger.paths import file_search_roots


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("ANALYST_FILE_SEARCH_ROOTS", raising=False)
    monkeypatch.delenv("ANALYST_CHAT_ROUTER", raising=False)
    return Ledger()


def _plant(root: Path, rel: str, content: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- matcher -----------------------------------------------------------------


def test_matcher_positives():
    q1 = match_file_request("where are the quarterly reports for TSLA?")
    assert q1 is not None
    assert q1.symbols == ["TSLA"]
    assert "quarterly" in q1.periods
    assert q1.noun_kind == "report"

    q2 = match_file_request("find the NVDA earnings deck")
    assert q2 is not None and q2.symbols == ["NVDA"]

    q3 = match_file_request("do we have a 10-K pdf for apple?")
    assert q3 is not None and "AAPL" in q3.symbols


def test_matcher_negatives():
    assert match_file_request("any TSLA filings?") is None
    assert match_file_request("run the morning scan") is None
    assert match_file_request("digest my notes") is None
    assert match_file_request("what do you think about margins?") is None


def test_summary_flag():
    q = match_file_request("find and summarize the TSLA report")
    assert q is not None and q.wants_summary


# --- roots config ------------------------------------------------------------


def test_file_search_roots_pathsep(tmp_path, monkeypatch):
    a = tmp_path / "roots_a"
    b = tmp_path / "roots_b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv(
        "ANALYST_FILE_SEARCH_ROOTS", f"{a}{os.pathsep}{b}{os.pathsep}{tmp_path / 'missing'}"
    )
    roots = file_search_roots()
    assert roots == [a.resolve(), b.resolve()]


def test_file_search_roots_unset(monkeypatch):
    monkeypatch.delenv("ANALYST_FILE_SEARCH_ROOTS", raising=False)
    assert file_search_roots() == []


# --- ranking -----------------------------------------------------------------


def test_ranking_and_skip_dirs(tmp_path):
    root = tmp_path / "docs"
    _plant(root, "Reports/TSLA_Q2_2026_report.pdf")
    _plant(root, "random_notes.txt")
    _plant(root, ".git/TSLA_secret_report.pdf")

    query = build_query("quarterly reports for TSLA")
    matches = search_files(query, roots=[root])
    rels = [m.rel_path for m in matches]
    assert rels[0] == "Reports/TSLA_Q2_2026_report.pdf"
    assert all(".git" not in r for r in rels)
    top = matches[0]
    assert any("ticker TSLA" in r for r in top.reasons)
    assert any("period" in r for r in top.reasons)
    assert "abs" not in top.public()
    assert str(tmp_path) not in json.dumps(top.public())


def test_zero_score_files_excluded(tmp_path):
    root = tmp_path / "docs"
    _plant(root, "unrelated_recipe.txt")
    matches = search_files(build_query("quarterly reports for TSLA"), roots=[root])
    assert matches == []


# --- chat API integration ----------------------------------------------------


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


def _wait_for_kind(ledger: Ledger, thread_id: str, kind: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for msg in ledger.list_chat_messages(thread_id):
            payload = msg.get("payload") or {}
            if payload.get("kind") == kind:
                return payload
        time.sleep(0.05)
    return None


def test_chat_file_search_end_to_end(ledger: Ledger, tmp_path, monkeypatch):
    from analyst_ledger.dashboard import make_app

    root = tmp_path / "docs"
    _plant(root, "Reports/TSLA_Q2_2026_report.pdf")
    monkeypatch.setenv("ANALYST_FILE_SEARCH_ROOTS", str(root))

    thread = ledger.get_or_create_chat_thread(master=True)
    app = make_app(ledger)
    st, job = _call(
        app,
        "POST",
        "/api/chats/message",
        {"thread_id": thread.session_id, "content": "where are the quarterly reports for TSLA?"},
    )
    assert st.startswith("200")
    assert job["kind"] == "file_search"

    payload = _wait_for_kind(ledger, thread.session_id, "file_search")
    assert payload is not None, "file_search reply never appeared"
    assert "Reports/TSLA_Q2_2026_report.pdf" in payload["content"]
    assert str(root) not in payload["content"]  # relative paths only
    meta = payload.get("metadata") or {}
    assert meta["roots_count"] == 1
    assert meta["matches"][0]["rel_path"] == "Reports/TSLA_Q2_2026_report.pdf"
    # audit event logged
    types = {e["type"] for e in ledger.list_events(session_id=thread.session_id, limit=50)}
    assert "file_search" in types


def test_chat_falls_through_when_roots_unset(ledger: Ledger):
    from analyst_ledger.dashboard import make_app

    thread = ledger.get_or_create_chat_thread(master=True)
    app = make_app(ledger)
    st, job = _call(
        app,
        "POST",
        "/api/chats/message",
        {"thread_id": thread.session_id, "content": "where are the quarterly reports for TSLA?"},
    )
    assert st.startswith("200")
    assert job["key"] == "chat:master"


def test_summary_local_model_and_offline(ledger: Ledger, tmp_path, monkeypatch):
    root = tmp_path / "docs"
    _plant(root, "TSLA_Q2_2026_report.md", "Revenue up 12%. Margins stable.")
    monkeypatch.setenv("ANALYST_FILE_SEARCH_ROOTS", str(root))
    thread = ledger.get_or_create_chat_thread(master=True)

    monkeypatch.setattr(
        "analyst_ledger.synthesize._call_openai_compatible_messages",
        lambda messages, **kw: "- Revenue up 12%\n- Margins stable",
    )
    query = match_file_request("find and summarize the quarterly TSLA report")
    result = execute_file_search(ledger, thread.session_id, query)
    assert result["status"] == "ok"
    payload = _wait_for_kind(ledger, thread.session_id, "file_search", timeout=1.0)
    assert "Summary (local model)" in payload["content"]
    assert "Attached: TSLA_Q2_2026_report.md" in payload["content"]

    # Offline endpoint degrades gracefully (fresh thread to avoid stale match)
    thread2 = ledger.get_or_create_chat_thread(ritual_id="filesearch_offline")

    def _down(messages, **kw):
        raise RuntimeError("Qwen endpoint unreachable at http://127.0.0.1:11434/v1")

    monkeypatch.setattr(
        "analyst_ledger.synthesize._call_openai_compatible_messages", _down
    )
    result2 = execute_file_search(ledger, thread2.session_id, query)
    assert result2["status"] == "ok"
    payload2 = _wait_for_kind(ledger, thread2.session_id, "file_search", timeout=1.0)
    assert "Local model offline" in payload2["content"] or "local model offline" in payload2["content"].lower()
    assert "TSLA_Q2_2026_report.md" in payload2["content"]


def test_workflow_action_find_files(ledger: Ledger, tmp_path, monkeypatch):
    from analyst_ledger.orchestration import ALLOWED_ACTIONS, validate_workflow_spec
    from analyst_ledger.workflow_engine import WorkflowEngine

    assert "find_files" in ALLOWED_ACTIONS
    validate_workflow_spec(
        {
            "name": "find_reports",
            "runner": "note_digest",
            "steps": [{"find_files": {"query": "quarterly report", "limit": 5}}],
        }
    )

    spec = {
        "watchlist": ["TSLA"],
        "steps": [{"find_files": {"query": "quarterly report", "limit": 5}}],
    }
    engine = WorkflowEngine(ledger)
    rows = engine._execute_action("find_files", spec=spec, stub=True, job=None)
    assert rows and rows[0]["rel_path"].endswith("TSLA_report.md")

    root = tmp_path / "docs"
    _plant(root, "TSLA_quarterly_report.md")
    monkeypatch.setenv("ANALYST_FILE_SEARCH_ROOTS", str(root))
    rows_live = engine._execute_action("find_files", spec=spec, stub=False, job=None)
    assert rows_live and rows_live[0]["rel_path"] == "TSLA_quarterly_report.md"
    assert "abs_path" not in rows_live[0]
