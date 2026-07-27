"""Mention + research fixes for Hire agents on Teams."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_mention_resolves_agent_id_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    from analyst_ledger.friend_personalities import mentioned_personalities
    from analyst_ledger.registry import create_composed_agent
    from analyst_ledger.paths import use_data_dir

    root = tmp_path / "user"
    root.mkdir()
    with use_data_dir(root):
        agent = create_composed_agent(
            name="Research Associate",
            capability_ids=["web_research"],
            prompt="You research carefully.",
        )
        found = mentioned_personalities(
            f"@{agent.id} find me news on NIO",
            extra_agent_ids=[agent.id],
        )
        assert [p.id for p in found] == [agent.id]
        found2 = mentioned_personalities(
            "@ResearchAssociate find news",
            extra_agent_ids=[agent.id],
        )
        assert [p.id for p in found2] == [agent.id]


def test_research_request_triggers_for_custom_mention_only():
    from analyst_ledger.friend_qwen import _is_research_request

    assert _is_research_request(
        "@agent_research_associate find me news on Nio"
    )
    assert not _is_research_request("find me news on Nio")  # no @mention
