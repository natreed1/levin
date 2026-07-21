"""Tests for Claude-planned automations, bounded runs, and chat persistence."""

from __future__ import annotations

import json
from io import BytesIO

import pytest

from analyst_ledger.dashboard import make_app
from analyst_ledger.ledger import Ledger
from analyst_ledger.orchestration import (
    ClaudeGateway,
    WorkflowValidationError,
    validate_workflow_spec,
)
from analyst_ledger.paths import ritual_specs_dir
from analyst_ledger.rituals import create_automations_with_claude
from analyst_ledger.schema import Event, Sensitivity, Surface
from analyst_ledger.workflow_engine import JobManager, WorkflowEngine


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    return Ledger()


def _spec(ritual_id: str, approved: bool = True, model=None) -> None:
    if model is None and approved:
        model = "claude"
    path = ritual_specs_dir() / f"{ritual_id}.json"
    path.write_text(
        json.dumps(
            {
                "name": ritual_id,
                "version": 1,
                "approved": approved,
                "enabled": True,
                "model": model,
                "runner": "note_digest",
                "watchlist": [],
                "steps": [{"recent_notes": {"days": 7}}],
                "budget": {"max_steps": 2, "max_minutes": 1, "max_tokens": 8000},
            }
        ),
        encoding="utf-8",
    )


def test_workflow_validation_rejects_executable_actions():
    with pytest.raises(WorkflowValidationError):
        validate_workflow_spec(
            {
                "name": "unsafe",
                "runner": "note_digest",
                "steps": [{"shell": "rm -rf /"}],
            }
        )


def test_claude_planner_filters_restricted_and_writes_unapproved(ledger: Ledger):
    session = ledger.start_session("Safe research", surface=Surface.NOTES.value)
    ledger.add_note("public workflow note", session_id=session.session_id)
    ledger.append_event(
        Event(
            type="note",
            surface=Surface.NOTES.value,
            session_id=session.session_id,
            sensitivity=Sensitivity.RESTRICTED.value,
            payload={"text": "never send this secret"},
        )
    )
    ledger.end_session(session_id=session.session_id)
    captured = {}

    class FakeGateway:
        def complete_json(self, messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return {
                "automations": [
                    {
                        "name": "weekly_notes_review",
                        "description": "Review recent analyst notes.",
                        "runner": "note_digest",
                        "schedule": "0 8 * * 1",
                        "watchlist": [],
                        "steps": [{"recent_notes": {"days": 7}}],
                        "budget": {"max_steps": 2},
                    }
                ]
            }

    result = create_automations_with_claude(ledger, gateway=FakeGateway())
    assert result["count"] == 1
    assert "public workflow note" in captured["prompt"]
    assert "never send this secret" not in captured["prompt"]
    spec = json.loads(
        (ritual_specs_dir() / "weekly_notes_review.json").read_text(encoding="utf-8")
    )
    assert spec["approved"] is False
    assert spec["proposed_by"] == "claude_api"


def test_public_web_search_action_stub(ledger: Ledger):
    path = ritual_specs_dir() / "osint_stub.json"
    path.write_text(
        json.dumps(
            {
                "name": "osint_stub",
                "version": 1,
                "approved": True,
                "enabled": True,
                "model": "claude",
                "runner": "note_digest",
                "watchlist": [],
                "steps": [{"public_web_search": ["Iran updates"]}],
                "budget": {"max_steps": 2, "max_minutes": 1, "max_tokens": 8000},
            }
        ),
        encoding="utf-8",
    )
    replies = iter(
        [
            '{"action":"public_web_search"}',
            "Summary\n\nPublic search returned stub headlines. Next check: verify sources.",
        ]
    )
    gateway = ClaudeGateway(
        ledger, responder=lambda _messages, _max_tokens, _system: next(replies)
    )
    result = WorkflowEngine(ledger, gateway).run(
        "osint_stub", request="Iran updates", stub=True
    )
    assert result["status"] == "ok"


def test_workflow_run_creates_chat_and_master_handoff(ledger: Ledger):
    source = ledger.start_session("Research notes", surface=Surface.NOTES.value)
    ledger.add_note("Margins need a follow-up", session_id=source.session_id)
    ledger.end_session(session_id=source.session_id)
    _spec("weekly_notes_review")
    replies = iter(
        [
            '{"action":"recent_notes"}',
            "Summary\n\nChecked recent notes. Next check: validate the margin evidence.",
        ]
    )
    gateway = ClaudeGateway(
        ledger, responder=lambda _messages, _max_tokens, _system: next(replies)
    )
    result = WorkflowEngine(ledger, gateway).run(
        "weekly_notes_review", request="What needs follow-up?", stub=True
    )
    assert result["status"] == "ok"
    messages = ledger.list_chat_messages(result["thread_id"])
    assert any(
        (m.get("payload") or {}).get("kind") == "synthesis" for m in messages
    )
    master = ledger.get_or_create_chat_thread(master=True)
    handoffs = ledger.list_events(
        session_id=master.session_id, limit=20, types=["workflow_handoff"]
    )
    assert handoffs and handoffs[0]["payload"]["from"] == "weekly_notes_review"
    assert ledger.summary()["egress_audits"] == 2


def test_unapproved_workflow_cannot_run(ledger: Ledger):
    _spec("draft_notes_review", approved=False)
    with pytest.raises(RuntimeError, match="not approved"):
        WorkflowEngine(ledger).run("draft_notes_review", stub=True)


def test_first_run_requires_agent_model(ledger: Ledger):
    _spec("notes_no_model", model="")
    with pytest.raises(RuntimeError, match="Choose an agent model"):
        WorkflowEngine(ledger).run("notes_no_model", stub=False)


def test_update_automation_persists_qwen_model(ledger: Ledger):
    from analyst_ledger.rituals import load_spec, update_automation

    _spec("notes_model_pick", model="")
    update_automation("notes_model_pick", model="qwen3-8b")
    assert load_spec("notes_model_pick")["model"] == "qwen3-8b"


def test_gateway_routes_qwen_destination(ledger: Ledger, monkeypatch):
    import sqlite3

    from analyst_ledger.paths import data_dir

    calls = {}

    def fake_qwen(messages, *, max_tokens=2048, system=None):
        calls["messages"] = messages
        return '{"ok": true}'

    monkeypatch.setattr(
        "analyst_ledger.orchestration._call_openai_compatible_messages", fake_qwen
    )
    gateway = ClaudeGateway(ledger, model="qwen3-8b")
    result = gateway.complete(
        [{"role": "user", "content": "ping"}], kind="test", max_tokens=32
    )
    assert result.text == '{"ok": true}'
    assert calls["messages"][0]["content"] == "ping"
    assert ledger.summary()["egress_audits"] >= 1
    conn = sqlite3.connect(data_dir() / "ledger.sqlite3")
    row = conn.execute(
        "SELECT destination FROM egress_audit ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row and row[0] == "qwen"


def test_job_manager_enforces_one_active_job_per_workflow():
    manager = JobManager()
    release = __import__("threading").Event()

    def wait(job):
        release.wait(timeout=2)
        return {"ok": True}

    first = manager.start("workflow:x", "workflow_run", wait)
    with pytest.raises(RuntimeError, match="already running"):
        manager.start("workflow:x", "workflow_run", wait)
    manager.cancel(first.job_id)
    release.set()


def test_chats_page_and_api(ledger: Ledger):
    _spec("weekly_notes_review")
    app = make_app(ledger)

    def call(path: str):
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "wsgi.input": BytesIO(b""),
            "CONTENT_LENGTH": "0",
        }
        status = []
        body = b"".join(app(environ, lambda s, _h: status.append(s)))
        return status[0], body

    status, body = call("/chats")
    assert status.startswith("200")
    assert b"Master workflows" in body
    assert b"Weekly Notes Review" in body
    status, body = call("/api/chats")
    assert status.startswith("200")
    assert any(row["master"] for row in json.loads(body.decode()))
