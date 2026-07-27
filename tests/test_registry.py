"""Registry SoR: Capability / Agent / Automation are one catalog."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_builtin_web_research_and_bullish_share_registry():
    from analyst_ledger.registry import (
        agent_has_capability,
        get_agent,
        get_capability,
        list_agents_public,
        list_capabilities_public,
        list_room_palette_public,
    )

    research = get_capability("web_research")
    assert research is not None
    assert research.name == "Web research"
    assert any(c["id"] == "web_research" for c in list_capabilities_public())

    bull = get_agent("qwen-bull")
    assert bull is not None
    assert bull.kind == "lens"
    assert bull.capabilities == ()
    assert agent_has_capability("qwen-bull", "web_research") is False
    assert agent_has_capability("qwen", "web_research") is True

    agents = {a["id"]: a for a in list_agents_public()}
    assert "qwen-bull" in agents
    assert agents["qwen-bull"]["kind"] == "lens"
    assert "web_research" in agents["qwen"]["capabilities"]

    palette = {a["id"]: a for a in list_room_palette_public()}
    assert "qwen-bull" in palette
    assert "master" not in palette  # operator thread only


def test_friend_personalities_derived_from_registry():
    from analyst_ledger.friend_personalities import PERSONALITIES_BY_ID, specialists_public
    from analyst_ledger.registry import get_agent

    assert PERSONALITIES_BY_ID["qwen-bull"].name == get_agent("qwen-bull").name
    assert PERSONALITIES_BY_ID["qwen"].prompt == get_agent("qwen").prompt
    ids = {row["id"] for row in specialists_public()}
    assert ids == {"qwen", "qwen-bull", "qwen-contrarian", "qwen-synthesizer"}


def test_create_automation_persists_registry_capability(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "ledger"))
    from analyst_ledger.paths import use_data_dir
    from analyst_ledger.registry import (
        create_automation_from_chat,
        list_capabilities,
        list_automations,
    )
    from analyst_ledger.ledger import Ledger

    with use_data_dir(tmp_path / "ledger"):
        spec = create_automation_from_chat(
            name="desk_loop",
            capability_ids=["web_research", "sec_filings_check"],
            schedule="0 7 * * 1-5",
            room_id="r1",
            transcript="automate morning",
        )
        assert spec["approved"] is False
        caps = {c.id: c for c in list_capabilities()}
        assert "desk_loop" in caps
        assert caps["desk_loop"].kind == "user"
        assert caps["desk_loop"].steps == ("web_research", "sec_filings_check")
        assert list_automations(ledger=Ledger()) == []


def test_compose_custom_agent_from_lenses_and_caps(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "ledger"))
    from analyst_ledger.paths import use_data_dir
    from analyst_ledger.registry import (
        create_composed_agent,
        create_lens,
        create_user_capability,
        get_agent,
        known_agent_ids,
        list_lenses,
        list_room_palette_public,
    )
    from analyst_ledger.friend_personalities import resolve_specialists

    with use_data_dir(tmp_path / "ledger"):
        lens = create_lens(name="Filings hawk", prompt="Prefer primary SEC sources.")
        assert lens.id in {ln.id for ln in list_lenses()}
        cap = create_user_capability(
            name="Desk note", summary="Draft a short desk note", runner="note_digest"
        )
        agent = create_composed_agent(
            name="Filings scout",
            lens_ids=[lens.id],
            capability_ids=["sec_filings_check", cap.id],
        )
        assert agent.id.startswith("agent_")
        loaded = get_agent(agent.id)
        assert loaded is not None
        assert "sec_filings_check" in loaded.capabilities
        assert "Prefer primary SEC sources" in loaded.prompt
        assert agent.id in known_agent_ids()
        palette_ids = {a["id"] for a in list_room_palette_public()}
        assert agent.id in palette_ids
        resolved = resolve_specialists([agent.id, "qwen-bull"])
        assert {p.id for p in resolved} == {agent.id, "qwen-bull"}


def test_room_guidance_includes_objective():
    from messenger.specialist_room import _room_guidance

    text = _room_guidance(
        {
            "config": {
                "objective": "Keep NVDA filings current",
                "prompts": ["Prefer primary sources"],
                "skills": ["sec_filings_check"],
            }
        }
    )
    assert "Keep NVDA filings current" in text
    assert "Prefer primary sources" in text
    assert "sec_filings_check" in text
