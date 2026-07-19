"""Friend-thread bridge helpers (no live network)."""

from __future__ import annotations

import analyst_ledger.friend_qwen as fq
import analyst_ledger.messenger_bridge as bridge
from analyst_ledger.friend_personalities import (
    match_personality,
    mentioned_personalities,
)


def test_friend_thread_meta():
    meta = bridge.friend_thread_meta()
    assert meta["session_id"] == bridge.FRIEND_THREAD_ID
    assert meta["friend"] is True
    assert meta["desk_tag"] == "chat:friend"


def test_messenger_configured_requires_url_and_invite(monkeypatch):
    monkeypatch.delenv("ANALYST_MESSENGER_URL", raising=False)
    monkeypatch.delenv("ANALYST_MESSENGER_INVITE", raising=False)
    assert bridge.messenger_configured() is False
    monkeypatch.setenv("ANALYST_MESSENGER_URL", "https://levin.fly.dev")
    assert bridge.messenger_configured() is False
    monkeypatch.setenv("ANALYST_MESSENGER_INVITE", "secret")
    assert bridge.messenger_configured() is True


def test_qwen_mention_detection():
    assert fq.MENTION_RE.search("hey @Qwen what time is it?")
    assert fq.MENTION_RE.search("@qwen ping")
    assert fq.MENTION_RE.search("@Qwen-Contrarian challenge this")
    assert not fq.MENTION_RE.search("@Qwen Contrarian challenge this")
    assert not fq.MENTION_RE.search("qwen without at")
    assert not fq.MENTION_RE.search("email@qwen.com")


def test_personality_mentions_use_exact_longest_match():
    assert match_personality("@Qwen answer this").id == "qwen"
    assert (
        match_personality("@Qwen-Contrarian challenge this").id
        == "qwen-contrarian"
    )
    assert match_personality("@Qwen Contrarian") is None
    found = mentioned_personalities(
        "@Qwen-Contrarian take the other side, then @Qwen summarize"
    )
    assert [personality.id for personality in found] == [
        "qwen-contrarian",
        "qwen",
    ]


def test_find_pending_mention_skips_old_and_self():
    raw = [
        {"id": 1, "author": "Nat", "body": "@Qwen hi"},
        {"id": 2, "author": "Qwen", "body": "hello"},
        {"id": 3, "author": "Friend", "body": "no mention"},
        {"id": 4, "author": "Friend", "body": "@Qwen again"},
    ]
    assert fq._find_pending_mention(raw, last_replied_id=1)["id"] == 4
    assert fq._find_pending_mention(raw, last_replied_id=4) is None


def test_probe_qwen_endpoint_reports_unreachable(monkeypatch):
    monkeypatch.setenv("ANALYST_QWEN_BASE_URL", "http://127.0.0.1:1")
    probe = fq.probe_qwen_endpoint()
    assert probe["reachable"] is False
    assert probe["ok"] is False


def test_old_qwen_env_is_migrated_to_qwen3(monkeypatch):
    monkeypatch.setenv("ANALYST_QWEN_MODEL", "qwen2.5:7b")
    assert fq.qwen_endpoint_info()["model"] == "qwen3:8b"
