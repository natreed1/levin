"""Dual-run arena: simultaneous lanes, isolation from workflow chat, grading."""

from __future__ import annotations

import json
import time
from io import BytesIO

import pytest

from analyst_ledger.arena import (
    comparisons_path,
    create_trial,
    load_trial,
    save_grade,
    save_trial,
)
from analyst_ledger.dashboard import make_app
from analyst_ledger.ledger import Ledger
from analyst_ledger.orchestration import ClaudeGateway
from analyst_ledger.paths import ritual_specs_dir
from analyst_ledger.schema import Surface
from analyst_ledger.workflow_engine import JobManager, WorkflowEngine


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    return Ledger()


def _spec(ritual_id: str = "weekly_notes_review") -> None:
    path = ritual_specs_dir() / f"{ritual_id}.json"
    path.write_text(
        json.dumps(
            {
                "name": ritual_id,
                "version": 1,
                "approved": True,
                "enabled": True,
                "model": "claude",
                "runner": "note_digest",
                "watchlist": [],
                "steps": [{"recent_notes": {"days": 7}}],
                "budget": {"max_steps": 2, "max_minutes": 1, "max_tokens": 8000},
            }
        ),
        encoding="utf-8",
    )


def _wsgi(app, method: str, path: str, *, qs: str = "", body: dict | None = None):
    raw = json.dumps(body or {}).encode() if body is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": qs,
        "wsgi.input": BytesIO(raw),
        "CONTENT_LENGTH": str(len(raw)),
    }
    status: list[str] = []
    out = b"".join(app(environ, lambda s, _h: status.append(s)))
    return status[0], out


def test_arena_threads_hidden_from_chat_list(ledger: Ledger):
    _spec()
    workflow = ledger.get_or_create_chat_thread("weekly_notes_review")
    trial = create_trial(
        ledger,
        ritual_id="weekly_notes_review",
        request="Summarize recent notes",
        model_a="claude",
        model_b="qwen3-8b",
        source_thread_id=workflow.session_id,
    )
    threads = ledger.list_chat_threads()
    ids = {t["session_id"] for t in threads}
    assert workflow.session_id in ids
    assert trial.lanes["a"].thread_id not in ids
    assert trial.lanes["b"].thread_id not in ids


def test_arena_run_skips_master_handoff(ledger: Ledger):
    source = ledger.start_session("Research notes", surface=Surface.NOTES.value)
    ledger.add_note("Margins need a follow-up", session_id=source.session_id)
    ledger.end_session(session_id=source.session_id)
    _spec()
    trial = create_trial(
        ledger,
        ritual_id="weekly_notes_review",
        request="What needs follow-up?",
        model_a="claude",
        model_b="qwen3-8b",
    )
    replies = iter(
        [
            '{"action":"recent_notes"}',
            "Arena A synthesis.",
            '{"action":"recent_notes"}',
            "Arena B synthesis.",
        ]
    )
    gateway = ClaudeGateway(
        ledger, responder=lambda _m, _t, _s: next(replies)
    )
    result_a = WorkflowEngine(ledger, gateway).run(
        "weekly_notes_review",
        request="What needs follow-up?",
        stub=True,
        model_override="claude",
        thread_id=trial.lanes["a"].thread_id,
        handoff=False,
    )
    result_b = WorkflowEngine(ledger, gateway).run(
        "weekly_notes_review",
        request="What needs follow-up?",
        stub=True,
        model_override="qwen3-8b",
        thread_id=trial.lanes["b"].thread_id,
        handoff=False,
    )
    assert "Arena A" in result_a["output"]
    assert "Arena B" in result_b["output"]
    master = ledger.get_or_create_chat_thread(master=True)
    handoffs = ledger.list_events(
        session_id=master.session_id, limit=20, types=["workflow_handoff"]
    )
    assert handoffs == []
    durable = ledger.get_or_create_chat_thread("weekly_notes_review")
    durable_msgs = ledger.list_chat_messages(durable.session_id)
    assert not any(
        "Arena A" in str((m.get("payload") or {}).get("content") or "")
        for m in durable_msgs
    )


def test_simultaneous_arena_jobs_and_grade(ledger: Ledger):
    source = ledger.start_session("Research notes", surface=Surface.NOTES.value)
    ledger.add_note("Check NVDA margins", session_id=source.session_id)
    ledger.end_session(session_id=source.session_id)
    _spec()
    trial = create_trial(
        ledger,
        ritual_id="weekly_notes_review",
        request="What needs follow-up?",
        model_a="claude",
        model_b="qwen3-8b",
    )
    def make_responder(label: str):
        state = {"n": 0}

        def responder(_messages, _max_tokens, _system):
            state["n"] += 1
            if state["n"] == 1:
                return '{"action":"recent_notes"}'
            return f"{label} lane output."

        return responder

    manager = JobManager()

    def run_lane(lane_key: str):
        lane = trial.lanes[lane_key]
        gateway = ClaudeGateway(
            ledger, responder=make_responder(lane.model_label), model=lane.model
        )

        def fn(job):
            return WorkflowEngine(ledger, gateway).run(
                "weekly_notes_review",
                request=trial.request,
                stub=True,
                job=job,
                model_override=lane.model,
                thread_id=lane.thread_id,
                handoff=False,
            )

        return fn

    job_a = manager.start(f"arena:{trial.trial_id}:a", "arena_run", run_lane("a"))
    job_b = manager.start(f"arena:{trial.trial_id}:b", "arena_run", run_lane("b"))
    deadline = time.time() + 5
    while time.time() < deadline:
        a = manager.get(job_a.job_id)
        b = manager.get(job_b.job_id)
        if (
            a
            and b
            and a.status in {"completed", "failed"}
            and b.status in {"completed", "failed"}
        ):
            break
        time.sleep(0.05)
    assert manager.get(job_a.job_id).status == "completed"
    assert manager.get(job_b.job_id).status == "completed"
    trial.lanes["a"].output = manager.get(job_a.job_id).result["output"]
    trial.lanes["b"].output = manager.get(job_b.job_id).result["output"]
    save_trial(trial)

    graded = save_grade(
        ledger,
        trial.trial_id,
        winner="a",
        scores_a={
            "helpfulness": 5,
            "correctness": 4,
            "research_quality": 4,
            "concision": 3,
        },
        scores_b={
            "helpfulness": 3,
            "correctness": 3,
            "research_quality": 3,
            "concision": 4,
        },
        notes_a="Clearer",
        notes_b="Shorter",
        training_note="Prefer Claude for this ritual",
    )
    assert graded["status"] == "ok"
    reloaded = load_trial(trial.trial_id)
    assert reloaded.grade["winner"] == "a"
    lines = comparisons_path().read_text(encoding="utf-8").strip().splitlines()
    assert lines and json.loads(lines[-1])["winner"] == "a"


def test_arena_page_and_api_start(ledger: Ledger, monkeypatch):
    source = ledger.start_session("Research notes", surface=Surface.NOTES.value)
    ledger.add_note("Check NVDA margins", session_id=source.session_id)
    ledger.end_session(session_id=source.session_id)
    _spec()

    def fake_complete(self, messages, **kwargs):
        from analyst_ledger.orchestration import ModelResult

        kind = kwargs.get("kind") or ""
        if kind == "workflow_decision":
            text = '{"action":"recent_notes"}'
        else:
            text = f"Synthesis from {self.model}."
        return ModelResult(
            text=text,
            estimated_input_tokens=10,
            estimated_output_tokens=10,
            audit_id="audit_test",
        )

    monkeypatch.setattr(ClaudeGateway, "complete", fake_complete)
    app = make_app(ledger)
    status, out = _wsgi(
        app,
        "POST",
        "/api/arena/start",
        body={
            "ritual_id": "weekly_notes_review",
            "request": "What needs follow-up?",
            "model_a": "claude",
            "model_b": "qwen3-8b",
            "stub": True,
        },
    )
    assert status.startswith("200"), out.decode()
    payload = json.loads(out.decode())
    trial_id = payload["trial_id"]
    assert payload["job_a"] and payload["job_b"]

    deadline = time.time() + 5
    while time.time() < deadline:
        st, body = _wsgi(app, "GET", f"/api/arena/{trial_id}")
        assert st.startswith("200")
        trial = json.loads(body.decode())
        if trial.get("both_done"):
            break
        time.sleep(0.05)
    assert trial["both_done"]

    st, page = _wsgi(app, "GET", "/chats/arena", qs=f"trial_id={trial_id}")
    assert st.startswith("200")
    assert b"Grading mode" in page
    assert b"Open grading" in page

    # Opt-in control lives on workflow chat, not in the coding/timeline surface.
    st, chats = _wsgi(app, "GET", "/chats", qs="ritual_id=weekly_notes_review")
    assert st.startswith("200")
    assert b"Compare two agents" in chats
    assert b"Run simultaneously" in chats

    st, graded = _wsgi(
        app,
        "POST",
        f"/api/arena/{trial_id}/grade",
        body={
            "winner": "b",
            "scores_a": {"helpfulness": 2, "correctness": 2, "research_quality": 2, "concision": 2},
            "scores_b": {"helpfulness": 5, "correctness": 5, "research_quality": 4, "concision": 4},
            "training_note": "Qwen won this notes ritual",
        },
    )
    assert st.startswith("200"), graded.decode()
    assert json.loads(graded.decode())["grade"]["winner"] == "b"


def test_create_trial_rejects_same_model(ledger: Ledger):
    _spec()
    with pytest.raises(ValueError, match="two different models"):
        create_trial(
            ledger,
            ritual_id="weekly_notes_review",
            request="x",
            model_a="claude",
            model_b="claude",
        )
