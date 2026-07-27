"""Tests for team TF routing, Orchestrator stub, fact-checker, room KG, tiers."""

from __future__ import annotations

import json

import pytest

from analyst_ledger.ledger import Ledger
from analyst_ledger.paths import ritual_specs_dir
from analyst_ledger.registry import get_agent, list_agents
from messenger import fact_checker, orch_grader, room_kg, task_tiers, team_orchestrator, team_router


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("ANALYST_CHAT_ROUTER", raising=False)
    monkeypatch.delenv("ANALYST_TEAM_COMPLEX", raising=False)
    return Ledger()


def _spec(ritual_id: str, runner: str, watchlist=None) -> None:
    path = ritual_specs_dir() / f"{ritual_id}.json"
    path.write_text(
        json.dumps(
            {
                "name": ritual_id,
                "version": 1,
                "approved": True,
                "enabled": True,
                "runner": runner,
                "watchlist": watchlist or [],
                "steps": [{"draft_note": "t"}],
            }
        ),
        encoding="utf-8",
    )


def test_builtin_agents_have_stage_skills(ledger: Ledger):
    analyst = get_agent("qwen")
    bull = get_agent("qwen-bull")
    orch = get_agent("orchestrator")
    assert analyst is not None and analyst.stage == "research"
    assert "web research" in analyst.skills
    assert bull is not None and bull.stage == "critique"
    assert orch is not None and orch.stage == "orchestrate"
    assert analyst.label_tokens()


def test_complex_routes_to_orchestrator(ledger: Ledger):
    roster = [a for a in list_agents() if a.id in {"qwen", "qwen-bull", "qwen-contrarian", "orchestrator"}]
    d = team_router.route_team_message(
        "any TSLA filings and what do bull and bear think?",
        roster_agents=roster,
    )
    assert d.path == "orchestrator"
    assert d.complex is True


def test_simple_filings_routes_automation(ledger: Ledger):
    _spec("sec_filings_check", "sec_filings_check", watchlist=["TSLA"])
    roster = [a for a in list_agents() if a.room_palette]
    d = team_router.route_team_message(
        "any TSLA filings?",
        roster_agents=roster,
        allow_automations=True,
    )
    assert d.path == "automation"
    assert d.ritual_id == "sec_filings_check"
    assert d.chip.startswith("TF→")


def test_explicit_single_mention_path(ledger: Ledger):
    roster = [get_agent("qwen"), get_agent("qwen-bull")]
    d = team_router.route_team_message(
        "@Bullish steelman the upside on NVDA",
        roster_agents=[a for a in roster if a],
    )
    assert d.path == "mention"
    assert d.agent_id == "qwen-bull"


def test_task_tiers_mapping():
    r = task_tiers.map_step("retrieve")
    assert r.backend == "local"
    assert r.power == "low"
    f = task_tiers.map_step("fact_check")
    assert f.backend == "frontier"
    assert f.power == "high"
    degraded = task_tiers.map_step(
        "retrieve", frontier_available=True, local_available=False
    )
    assert degraded.backend == "frontier"


def test_fact_checker_stub_contradict(ledger: Ledger):
    checks = fact_checker.check_claims(
        "This invented-claim-marker says revenue was 999.",
        evidence="Nothing about that.",
        stub=True,
    )
    assert checks
    assert any(c.verdict == "contradicted" for c in checks)


def test_fact_checker_stub_support_number(ledger: Ledger):
    checks = fact_checker.check_claims(
        "Tesla filed an 8-K showing 12 percent growth.",
        evidence="The 8-K mentions 12 percent growth in the period.",
        stub=True,
    )
    assert checks[0].verdict == "supported"


def test_room_kg_fact_gated_writes(ledger: Ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "kgdata"))
    kg = room_kg.load_kg("room_test")
    n = room_kg.apply_fact_check(
        kg,
        [
            {
                "claim": "TSLA filed an 8-K",
                "verdict": "supported",
                "sources": ["https://sec.gov/x"],
            },
            {
                "claim": "Made-up revenue of 9e12",
                "verdict": "contradicted",
                "sources": [],
            },
        ],
    )
    assert n == 2
    room_kg.save_kg(kg)
    loaded = room_kg.load_kg("room_test")
    statuses = {c["text"]: c["status"] for c in loaded.claims}
    assert statuses["TSLA filed an 8-K"] == "verified"
    assert statuses["Made-up revenue of 9e12"] == "disputed"
    snap = room_kg.compress_snapshot(loaded)
    assert "Verified" in snap or "TSLA" in snap


def test_orch_grader_process_metrics():
    g = orch_grader.grade_run(
        path="orchestrator",
        rounds=2,
        dry=True,
        questions_opened=3,
        questions_resolved=2,
        fact_checks=[
            {"verdict": "supported"},
            {"verdict": "supported"},
            {"verdict": "contradicted"},
        ],
        stages=["retrieve", "fact_check", "critique"],
    )
    assert g.letter() in {"A", "B", "C", "D", "F"}
    assert g.fact_contradicted == 1
    assert "path=orchestrator" in orch_grader.format_grade_chip(g)


def test_draw_org_research_critique_loop(ledger: Ledger):
    roster = [
        a
        for a in list_agents()
        if a.id in {"qwen", "qwen-bull", "qwen-contrarian", "qwen-synthesizer"}
    ]
    plan = team_orchestrator.draw_org(
        "filings then bull and bear on TSLA",
        roster=roster,
        room_skills=["sec_filings_check"],
    )
    assert plan.pattern == "research_critique_loop"
    kinds = [s.kind for s in plan.stages]
    assert kinds == ["retrieve", "fact_check", "critique", "synthesize"]
    assert plan.stages[0].capability_ids[0] == "sec_filings_check"
    assert "Bullish" in plan.stages[2].label or "Contrarian" in plan.stages[2].label


def test_discussion_outputs_final_consensus_only(ledger: Ledger):
    roster = [
        a
        for a in list_agents()
        if a.id in {"qwen", "qwen-bull", "qwen-contrarian", "qwen-synthesizer", "orchestrator"}
    ]
    store = _FakeStore(
        "r_disc",
        {
            "agents": [a.id for a in roster if a.id != "orchestrator"],
            "skills": ["sec_filings_check"],
        },
    )
    result = team_orchestrator.start_orchestrator_job(
        store=store,
        hub=None,
        room_id="r_disc",
        author="Nat",
        text="any TSLA filings and what do bull and bear think?",
        roster=roster,
        stub=True,
    )
    authors = [m["author"] for m in store.messages]
    # Quiet mode: only Orchestrator posts the final consensus (no stage spam)
    assert authors == ["Orchestrator"]
    body = store.messages[0]["body"]
    assert "Consensus report" in body
    assert "Process" in body or "Fact-check" in body
    assert result.get("report")
    assert "Consensus" in (result.get("report") or "")


def test_extract_questions_skips_user_echo():
    qs = team_orchestrator._extract_questions(
        "On: any TSLA filings and what do bull and bear think?",
        exclude_text="any TSLA filings and what do bull and bear think?",
    )
    assert qs == []
    qs2 = team_orchestrator._extract_questions(
        "Follow-ups:\n- What is the most recent 8-K item that would confirm growth?\n",
        exclude_text="any TSLA filings?",
    )
    assert any("8-K" in q for q in qs2)


class _FakeStore:
    def __init__(self, room_id: str, config: dict):
        self.room_id = room_id
        self._config = config
        self.messages = []

    def room(self, room_id):
        return {"id": room_id, "config": self._config}

    def add_message(self, *, author, body, room_id):
        msg = {"author": author, "body": body, "room_id": room_id}
        self.messages.append(msg)
        return msg

    def list_messages(self, limit=12, room_id=None):
        return list(self.messages)[-limit:]


def test_orchestrator_stub_execute_posts_progress(ledger: Ledger):
    roster = [
        a
        for a in list_agents()
        if a.id in {"qwen", "qwen-bull", "qwen-contrarian", "qwen-synthesizer", "orchestrator"}
    ]
    store = _FakeStore(
        "r1",
        {
            "agents": [a.id for a in roster if a.id != "orchestrator"],
            "skills": ["sec_filings_check"],
        },
    )
    result = team_orchestrator.start_orchestrator_job(
        store=store,
        hub=None,
        room_id="r1",
        author="Nat",
        text="TSLA filings then what do bull and bear think?",
        roster=roster,
        owner_user_id=None,
        stub=True,
    )
    assert result.get("ok")
    assert len(store.messages) == 1
    assert store.messages[0]["author"] == "Orchestrator"
    assert "Consensus report" in store.messages[0]["body"]
    assert "Process" in store.messages[0]["body"] or "Fact-check" in store.messages[0]["body"]
