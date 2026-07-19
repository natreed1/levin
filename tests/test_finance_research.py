"""Structured stock-outlook research contracts."""

from __future__ import annotations

import urllib.error

import analyst_ledger.finance_research as fr
import analyst_ledger.morning_yf as morning
import analyst_ledger.web_search as ws


def test_resolve_symbol_from_company_name_and_context():
    assert fr.resolve_symbol("research Apple stock outlook") == "AAPL"
    assert fr.resolve_symbol("do more research", "We were discussing Apple stock") == "AAPL"
    assert fr.resolve_symbol("research $NVDA") == "NVDA"
    assert fr.resolve_symbol("API key not set", allow_network=False) is None


def test_classify_finance_intents():
    assert fr.classify_finance_intent("is Apple a buy?") == "outlook"
    assert fr.classify_finance_intent("latest 10-Q filing") == "filings"
    assert fr.classify_finance_intent("latest catalyst news") == "news"
    assert fr.classify_finance_intent("AAPL price snapshot") == "snapshot"


def test_finance_queries_and_outlook_source_ranking():
    queries = ws.finance_search_queries("AAPL", "Apple", intent="outlook")
    assert any("guidance" in query and "AAPL" in query for query in queries)
    assert any("site:sec.gov" in query for query in queries)

    ranked = ws.rank_search_hits(
        [
            {
                "title": "Apple",
                "url": "https://en.wikipedia.org/wiki/Apple_Inc.",
                "snippet": "Overview",
                "published_at": "",
            },
            {
                "title": "Apple filing",
                "url": "https://www.sec.gov/Archives/example",
                "snippet": "10-Q",
                "published_at": "2026-05-01T00:00:00+00:00",
            },
            {
                "title": "Apple report",
                "url": "https://www.reuters.com/technology/apple-example",
                "snippet": "Guidance",
                "published_at": "2026-07-01T00:00:00+00:00",
            },
        ],
        intent="outlook",
    )
    assert [hit["title"] for hit in ranked] == ["Apple filing", "Apple report"]


def test_sec_trends_select_same_calendar_quarter(monkeypatch):
    monkeypatch.setattr(fr, "_sec_cik_for_symbol", lambda symbol: "0000320193")
    monkeypatch.setattr(
        fr,
        "_get_json",
        lambda *args, **kwargs: {
            "entityName": "Apple Inc.",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {
                                    "form": "10-Q",
                                    "frame": "CY2025Q1",
                                    "start": "2024-12-29",
                                    "end": "2025-03-29",
                                    "filed": "2025-05-02",
                                    "val": 100,
                                },
                                {
                                    "form": "10-Q",
                                    "frame": "CY2026Q1",
                                    "start": "2025-12-28",
                                    "end": "2026-03-28",
                                    "filed": "2026-05-01",
                                    "val": 110,
                                },
                            ]
                        }
                    },
                    "EarningsPerShareDiluted": {
                        "units": {
                            "USD/shares": [
                                {
                                    "form": "10-Q",
                                    "frame": "CY2025Q1",
                                    "end": "2025-03-29",
                                    "filed": "2025-05-02",
                                    "val": 2.0,
                                },
                                {
                                    "form": "10-Q",
                                    "frame": "CY2026Q1",
                                    "end": "2026-03-28",
                                    "filed": "2026-05-01",
                                    "val": 2.2,
                                },
                            ]
                        }
                    },
                }
            },
        },
    )
    result = fr.fetch_sec_trends("AAPL")
    assert round(result["revenue"]["growth_pct"], 2) == 10.0
    assert round(result["eps"]["growth_pct"], 2) == 10.0
    assert result["revenue"]["prior"]["frame"] == "CY2025Q1"


def test_outlook_evidence_contract_and_provider_fallback(monkeypatch):
    monkeypatch.delenv("ANALYST_FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(
        fr,
        "fetch_yahoo_chart_snapshot",
        lambda symbol: {"symbol": symbol, "price": 100, "source_url": "https://yahoo"},
    )
    monkeypatch.setattr(
        fr,
        "fetch_sec_financial_snapshot",
        lambda symbol: {"symbol": symbol, "source_url": "https://sec/facts"},
    )
    monkeypatch.setattr(
        fr,
        "fetch_sec_trends",
        lambda symbol: {
            "symbol": symbol,
            "revenue": {"latest": {"val": 110}, "growth_pct": 5},
            "eps": {"latest": {"val": 2.2}, "growth_pct": 4},
            "source_url": "https://sec/trends",
        },
    )
    monkeypatch.setattr(
        fr,
        "fetch_recent_filings",
        lambda symbol: [{"form": "10-Q", "filed": "2026-05-01", "url": "https://sec/q"}],
    )
    monkeypatch.setattr(
        fr,
        "_yahoo_history",
        lambda symbol: {
            "symbol": symbol,
            "return_pct": 20 if symbol == "AAPL" else 8,
            "source_url": f"https://yahoo/{symbol}",
        },
    )
    evidence = fr.build_outlook_evidence("AAPL")
    assert evidence["ready"] is True
    assert evidence["relative_return_pct"] == 12
    assert evidence["provider"]["available"] is False
    assert "API_KEY" in evidence["provider"]["reason"]


def test_morning_quote_uses_shared_chart_fallback(monkeypatch):
    def unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError("https://yahoo", 401, "unauthorized", {}, None)

    monkeypatch.setattr(morning.urllib.request, "urlopen", unauthorized)
    monkeypatch.setattr(
        ws,
        "fetch_yahoo_chart_snapshot",
        lambda symbol: {
            "symbol": symbol,
            "name": "Apple Inc.",
            "price": 300,
            "volume": 10,
            "previous_close": 295,
        },
    )
    monkeypatch.setattr(morning, "fetch_yahoo_headlines", lambda *args, **kwargs: [])
    result = morning.fetch_yahoo_quote("AAPL")
    assert result["price"] == 300
    assert result["source"] == "yahoo_chart_fallback"


def test_deterministic_outlook_has_required_sections():
    evidence = {
        "symbol": "AAPL",
        "ready": True,
        "market": {
            "price": 300,
            "currency": "USD",
            "as_of": "2026-07-17T20:00:00+00:00",
            "fifty_two_week_low": 190,
            "fifty_two_week_high": 310,
            "source_url": "https://yahoo/AAPL",
        },
        "sec_trends": {
            "revenue": {
                "latest": {"val": 110, "unit": "USD"},
                "prior": {"val": 100, "unit": "USD"},
                "growth_pct": 10,
            },
            "eps": {
                "latest": {"val": 2.2, "unit": "USD/shares"},
                "prior": {"val": 2.0, "unit": "USD/shares"},
                "growth_pct": 10,
            },
            "source_url": "https://sec/AAPL",
        },
        "performance": {"return_pct": 20, "source_url": "https://yahoo/AAPL/history"},
        "benchmark": {"return_pct": 8, "source_url": "https://yahoo/SPY/history"},
        "relative_return_pct": 12,
        "filings": [{"form": "10-Q", "filed": "2026-05-01", "url": "https://sec/q"}],
        "provider": {"available": False, "reason": "API key not set"},
    }
    balanced = fr.render_outlook_brief(evidence)
    contrarian = fr.render_outlook_brief(evidence, contrarian=True)
    for section in (
        "Verified facts",
        "Bull scenario",
        "Bear scenario",
        "Catalysts",
        "Risks",
        "What would change the view",
    ):
        assert section in balanced
        assert section in contrarian
    assert "constructive scenario" in balanced
    assert "trap is extrapolating" in contrarian
    assert "should buy" not in balanced.casefold()
