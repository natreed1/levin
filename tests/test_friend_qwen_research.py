"""Background @Qwen research path."""

from __future__ import annotations

import threading
import time

import analyst_ledger.friend_qwen as fq
import analyst_ledger.web_search as ws
from analyst_ledger.web_search import format_hits_for_prompt


def test_research_intent_detection():
    assert fq._is_research_request("@Qwen research this")
    assert fq._is_research_request("hey @qwen look up NVDA news")
    assert fq._is_research_request("@Qwen search for recent filings")
    assert fq._is_research_request("@Qwen dig into that")
    assert not fq._is_research_request("@Qwen ping")
    assert not fq._is_research_request("research this without mention")
    assert not fq._is_research_request("email@qwen.com research")


def test_research_intent_natural_finance_phrasing():
    # Natural asks to find/verify/check current data must trigger research.
    assert fq._is_research_request(
        "@Qwen can you find Alibaba's recent filings and verify their margins"
    )
    assert fq._is_research_request("@Qwen check the latest news on TSLA")
    assert fq._is_research_request("@Qwen-Contrarian verify their revenue")
    # Opinions and small talk must not trigger a research pass.
    assert not fq._is_research_request("@Qwen what do you think about pizza")
    assert not fq._is_research_request("@Qwen I think Alibaba is a great buy")


def test_alibaba_resolves_to_baba_offline():
    from analyst_ledger.finance_research import resolve_symbol

    assert resolve_symbol("find Alibaba filings", allow_network=False) == "BABA"
    assert resolve_symbol("what about Netflix", allow_network=False) == "NFLX"


def test_nonanswer_fallback_uses_deterministic_brief(monkeypatch):
    monkeypatch.setattr(
        fq, "build_financial_brief", lambda s: f"Verified evidence\n- {s} price"
    )
    assert fq._looks_like_nonanswer("Hello everyone! How can I help?", "BABA")
    assert not fq._looks_like_nonanswer(
        "BABA trades at 114.97 with a 52-week range of 91.99 to 192.67.", "BABA"
    )
    brief = fq._deterministic_research_brief(
        "BABA", [{"title": "Yahoo BABA", "url": "https://finance.yahoo.com/quote/BABA/"}]
    )
    assert "Verified evidence" in brief
    assert "https://finance.yahoo.com/quote/BABA/" in brief


def test_context_snippet_includes_last_lines():
    raw = [
        {"id": 1, "author": "Nat", "body": "one"},
        {"id": 2, "author": "Friend", "body": "two"},
        {"id": 3, "author": "Nat", "body": "@Qwen research this"},
        {"id": 4, "author": "Nat", "body": "after"},
    ]
    trigger = raw[2]
    snippet = fq._context_snippet(raw, trigger, n=8)
    assert [m["id"] for m in snippet] == [1, 2, 3]
    assert "after" not in {m["body"] for m in snippet}


def test_qwen_status_exposes_research_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    fq.save_state(
        {
            "enabled": True,
            "last_replied_id": 2,
            "research_status": "researching",
            "research_progress": "Searching…",
            "research_query": "NVDA earnings",
            "research_started_at": 1.0,
            "research_trigger_id": 9,
            "research_error": "",
        }
    )
    monkeypatch.setattr(
        fq,
        "probe_qwen_endpoint",
        lambda: {"ok": True, "reachable": True, "model": "x", "model_present": True},
    )
    monkeypatch.setattr(fq, "messenger_configured", lambda: True)
    st = fq.qwen_status()
    assert st["research_status"] == "researching"
    assert st["research_progress"] == "Searching…"
    assert st["research_query"] == "NVDA earnings"
    assert "research" in st["hint"].lower()


def test_finance_context_round_trips_in_state(tmp_path, monkeypatch):
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    state = {
        **fq._default_state(),
        "last_finance_symbol": "AAPL",
        "last_finance_intent": "outlook",
    }
    fq.save_state(state)
    loaded = fq.load_state()
    assert loaded["last_finance_symbol"] == "AAPL"
    assert loaded["last_finance_intent"] == "outlook"
    fq._update_research(research_progress="testing")
    loaded = fq.load_state()
    assert loaded["last_finance_symbol"] == "AAPL"
    assert loaded["last_finance_intent"] == "outlook"


def test_research_path_acks_and_returns_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    fq.save_state({**fq._default_state(), "enabled": True, "last_replied_id": 0})

    posts: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def slow_job(trigger, context, *args):
        started.set()
        release.wait(timeout=2)
        fq._update_research(
            research_status="idle",
            research_progress="",
            research_query="",
            research_error="",
            research_started_at=None,
            research_trigger_id=None,
        )

    monkeypatch.setattr(fq, "messenger_configured", lambda: True)
    monkeypatch.setattr(fq, "list_bot_rooms", lambda: [{"room_id": "legacy"}])
    monkeypatch.setattr(
        fq,
        "_load_room_messages",
        lambda room_id="legacy": [
            {"id": 1, "author": "Nat", "body": "talking about AI chips"},
            {"id": 2, "author": "Nat", "body": "@Qwen research this"},
        ],
    )
    monkeypatch.setattr(
        fq,
        "_post_as_qwen",
        lambda body, room_id="legacy": posts.append(body)
        or {"id": 10 + len(posts), "body": body},
    )
    monkeypatch.setattr(fq, "_run_research_job", slow_job)

    t0 = time.monotonic()
    result = fq.tick_qwen()
    elapsed = time.monotonic() - t0

    assert result["ok"] is True
    assert result.get("research") is True
    assert result.get("replied") is True
    assert elapsed < 0.5
    assert posts and "researching" in posts[0].lower()
    assert started.wait(timeout=1)
    st = fq.load_state()
    assert st["research_status"] == "researching"
    assert st["last_replied_id"] >= 2
    release.set()


def test_quick_reply_still_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    fq.save_state({**fq._default_state(), "enabled": True, "last_replied_id": 0})

    monkeypatch.setattr(fq, "messenger_configured", lambda: True)
    monkeypatch.setattr(fq, "list_bot_rooms", lambda: [{"room_id": "legacy"}])
    monkeypatch.setattr(
        fq,
        "_load_room_messages",
        lambda room_id="legacy": [
            {"id": 3, "author": "Friend", "body": "@Qwen ping"}
        ],
    )
    monkeypatch.setattr(
        fq,
        "_call_openai_compatible_messages",
        lambda *a, **k: "pong",
    )
    monkeypatch.setattr(
        fq, "_post_as_qwen", lambda body, room_id="legacy": {"id": 4, "body": body}
    )

    result = fq.tick_qwen()
    assert result["replied"] is True
    assert result.get("research") is False
    assert fq.load_state()["last_replied_id"] == 4


def test_contrarian_mention_routes_prompt_and_author(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    fq.save_state({**fq._default_state(), "enabled": True, "last_replied_id": 0})

    captured = {}
    monkeypatch.setattr(fq, "messenger_configured", lambda: True)
    monkeypatch.setattr(fq, "list_bot_rooms", lambda: [{"room_id": "legacy"}])
    monkeypatch.setattr(
        fq,
        "_load_room_messages",
        lambda room_id="legacy": [
            {
                "id": 3,
                "author": "Friend",
                "body": "@Qwen-Contrarian test this thesis",
            }
        ],
    )

    def complete(*args, **kwargs):
        captured["system"] = kwargs["system"]
        return "Here is the counterargument."

    def post(personality, body, room_id="legacy"):
        captured["personality"] = personality
        captured["body"] = body
        return {"id": 4, "body": body}

    monkeypatch.setattr(fq, "_call_openai_compatible_messages", complete)
    monkeypatch.setattr(fq, "_post_as_personality", post)

    result = fq.tick_qwen()
    assert result["personality"] == "qwen-contrarian"
    assert captured["personality"].name == "Qwen Contrarian"
    assert "evidence-led contrarian" in captured["system"]
    assert captured["body"] == "Here is the counterargument."


def test_tick_replies_in_created_room_with_per_room_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    fq.save_state({**fq._default_state(), "enabled": True, "last_replied_id": 0})

    monkeypatch.setattr(fq, "messenger_configured", lambda: True)
    monkeypatch.setattr(
        fq,
        "list_bot_rooms",
        lambda: [{"room_id": "legacy"}, {"room_id": "ROOM123"}],
    )

    room_messages = {
        "legacy": [],
        "ROOM123": [{"id": 7, "author": "Friend", "body": "@Qwen ping"}],
    }
    monkeypatch.setattr(
        fq, "_load_room_messages", lambda room_id="legacy": room_messages[room_id]
    )
    monkeypatch.setattr(
        fq, "_call_openai_compatible_messages", lambda *a, **k: "pong"
    )

    posted: list[tuple] = []

    def post(personality, body, room_id="legacy"):
        posted.append((room_id, body))
        return {"id": 8, "body": body}

    monkeypatch.setattr(fq, "_post_as_personality", post)

    result = fq.tick_qwen()

    assert result["replied"] is True
    assert result["room_id"] == "ROOM123"
    assert posted == [("ROOM123", "pong")]
    # Per-room last_replied advanced only for the created room.
    state = fq.load_state()
    assert fq.room_progress(state, "ROOM123")["last_replied_id"] == 8
    assert fq.room_progress(state, "legacy")["last_replied_id"] == 0


def test_format_hits_for_prompt():
    text = format_hits_for_prompt(
        [{"title": "A", "url": "https://a.example", "snippet": "s"}]
    )
    assert "A" in text and "https://a.example" in text
    assert format_hits_for_prompt([]) == "(no search results)"


def test_model_reply_removes_personality_label_and_mention():
    personality = fq.PERSONALITIES_BY_ID["qwen-contrarian"]
    assert (
        fq._clean_model_reply(
            "@Qwen-Contrarian: The evidence is incomplete.", personality
        )
        == "The evidence is incomplete."
    )
    assert (
        fq._clean_model_reply(
            "Qwen Contrarian: The downside is concentration.", personality
        )
        == "The downside is concentration."
    )


def test_financial_context_uses_deterministic_sources(monkeypatch):
    monkeypatch.setattr(
        ws,
        "fetch_yahoo_chart_snapshot",
        lambda symbol: {
            "symbol": symbol,
            "name": "Apple Inc.",
            "price": 300,
            "previous_close": 295,
            "day_high": 302,
            "day_low": 294,
            "volume": 10,
            "fifty_two_week_high": 310,
            "fifty_two_week_low": 190,
            "currency": "USD",
            "as_of": "2026-07-17T20:00:00+00:00",
            "source_url": "https://query1.finance.yahoo.com/example",
        },
    )
    monkeypatch.setattr(
        ws,
        "fetch_sec_financial_snapshot",
        lambda symbol: {
            "symbol": symbol,
            "entity": "Apple Inc.",
            "revenue": 111_000_000_000,
            "revenue_period_start": "2025-12-28",
            "revenue_period_end": "2026-03-28",
            "revenue_filed": "2026-05-01",
            "revenue_fiscal_period": "Q2",
            "revenue_accession": "abc",
            "diluted_eps": 2.01,
            "eps_period_end": "2026-03-28",
            "eps_filed": "2026-05-01",
            "eps_accession": "abc",
            "source_url": "https://data.sec.gov/example",
        },
    )
    text = ws.format_financial_context("Research AAPL revenue and EPS")
    assert "regular-market price: 300 USD" in text
    assert "latest discrete 10-Q revenue: 111000000000 USD" in text
    assert "diluted EPS for that quarter: 2.01" in text
    brief = ws.build_financial_brief("Research AAPL revenue and EPS")
    assert "AAPL regular-market price: 300 USD" in brief
    assert "Latest discrete SEC 10-Q revenue: $111.000 billion" in brief
    assert "do not establish fair value" in brief
    assert "Apple Car" not in brief


def test_bing_search_parses_rss(monkeypatch):
    rss = b"""<?xml version="1.0"?>
    <rss><channel><item>
      <title>NVIDIA &amp; AI</title>
      <link>https://example.com/nvidia</link>
      <description>Latest &lt;b&gt;chip&lt;/b&gt; news.</description>
    </item></channel></rss>"""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return rss

    monkeypatch.setattr(ws.urllib.request, "urlopen", lambda *a, **k: Response())
    hits = ws.bing_search("NVIDIA latest news", limit=3)
    assert hits == [
        {
            "title": "NVIDIA & AI",
            "url": "https://example.com/nvidia",
            "snippet": "Latest chip news.",
            "published_at": "",
        }
    ]


def test_skip_while_researching(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    fq.save_state(
        {
            **fq._default_state(),
            "enabled": True,
            "last_replied_id": 1,
            "research_status": "researching",
        }
    )
    monkeypatch.setattr(fq, "messenger_configured", lambda: True)
    monkeypatch.setattr(fq, "list_bot_rooms", lambda: [{"room_id": "legacy"}])
    monkeypatch.setattr(
        fq,
        "_load_room_messages",
        lambda room_id="legacy": [
            {"id": 5, "author": "Nat", "body": "@Qwen research more"}
        ],
    )
    started = []
    monkeypatch.setattr(fq, "_start_research", lambda *a, **k: started.append(1))

    result = fq.tick_qwen()
    assert result.get("skipped") == "researching"
    assert started == []


def test_followup_company_name_routes_to_outlook_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fq, "data_dir", lambda: tmp_path)
    fq.save_state({**fq._default_state(), "enabled": True, "last_replied_id": 0})
    captured = {}

    monkeypatch.setattr(fq, "_draft_search_queries", lambda *args: [])
    monkeypatch.setattr(
        fq,
        "finance_search_queries",
        lambda symbol, **kwargs: [f"{symbol} outlook"],
    )
    monkeypatch.setattr(fq, "bing_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(fq, "rank_search_hits", lambda hits, **kwargs: hits)
    monkeypatch.setattr(fq, "enrich_trusted_hits", lambda hits, **kwargs: hits)
    monkeypatch.setattr(
        fq,
        "build_outlook_evidence",
        lambda symbol: {
            "symbol": symbol,
            "market": {"source_url": "https://yahoo"},
            "sec_trends": {"source_url": "https://sec"},
            "filings": [{"url": "https://sec/q"}],
            "relative_return_pct": 5,
            "ready": True,
        },
    )

    def synthesize(request, evidence, hits_text, personality):
        captured["symbol"] = evidence["symbol"]
        captured["personality"] = personality.id
        return "Verified facts\nEvidence-backed outlook."

    monkeypatch.setattr(fq, "_synthesize_outlook", synthesize)
    monkeypatch.setattr(
        fq,
        "_post_as_personality",
        lambda personality, body, room_id="legacy": {"id": 9, "body": body},
    )

    fq._run_research_job(
        {"id": 8, "author": "Nat", "body": "@Qwen-Contrarian do more research use the web"},
        [
            {
                "id": 7,
                "author": "Nat",
                "body": "I like Apple stock as a buy",
            }
        ],
        fq.PERSONALITIES_BY_ID["qwen-contrarian"],
    )
    assert captured == {
        "symbol": "AAPL",
        "personality": "qwen-contrarian",
    }


def test_outlook_reply_validator_rejects_unknown_sources_and_missing_sections():
    evidence = {"market": {"source_url": "https://trusted.example/aapl"}}
    valid = """
Verified facts
Source: https://trusted.example/aapl
Bull scenario
Conditional upside.
Bear scenario
Conditional downside.
Catalysts
Next filing.
Risks
Evidence is incomplete.
What would change the view
More comparable periods.
"""
    assert fq._outlook_reply_valid(valid, evidence)
    assert not fq._outlook_reply_valid(
        valid.replace("https://trusted.example/aapl", "https://made-up.example"),
        evidence,
    )
    assert not fq._outlook_reply_valid("Verified facts only", evidence)
