from analyst_ledger.browser import normalize_url_key, parse_url
from analyst_ledger.session_insights import (
    collapse_events,
    recent_url_focus_same,
    summarize_session_events,
)


def test_normalize_strips_trailing_slash():
    a = normalize_url_key("https://finance.yahoo.com/quote/TSM/")
    b = normalize_url_key("https://finance.yahoo.com/quote/TSM")
    assert a == b
    assert parse_url("https://finance.yahoo.com/quote/TSM/")["url"].endswith("/quote/TSM")


def test_summarize_dedupes_urls_and_skips_surface_spam():
    events = [
        {
            "ts": "2026-07-16T10:00:05Z",
            "type": "url_focus",
            "surface": "browser",
            "payload": {
                "host": "finance.yahoo.com",
                "path": "/quote/TSM",
                "symbol": "TSM",
                "section": "quote",
                "url": "https://finance.yahoo.com/quote/TSM/",
            },
        },
        {
            "ts": "2026-07-16T10:00:04Z",
            "type": "url_focus",
            "surface": "browser",
            "payload": {
                "host": "finance.yahoo.com",
                "path": "/quote/TSM/",
                "symbol": "TSM",
                "section": "quote",
                "url": "https://finance.yahoo.com/quote/TSM",
            },
        },
        {
            "ts": "2026-07-16T10:00:03Z",
            "type": "url_focus",
            "surface": "browser",
            "payload": {
                "host": "finance.yahoo.com",
                "path": "/",
                "section": "home",
                "url": "https://finance.yahoo.com/",
            },
        },
        {
            "ts": "2026-07-16T10:00:02Z",
            "type": "url_focus",
            "surface": "browser",
            "payload": {
                "host": "finance.yahoo.com",
                "path": "/",
                "section": "home",
                "url": "https://finance.yahoo.com/",
            },
        },
        {
            "ts": "2026-07-16T10:00:01Z",
            "type": "session_start",
            "surface": "tradingview",
            "payload": {"title": "AM research"},
        },
    ]
    # newest-first like list_events
    events = sorted(events, key=lambda e: e["ts"], reverse=True)
    out = summarize_session_events(events)
    assert out["unique_pages"] == 2
    assert out["pages"][0]["visits"] >= 1
    tsm = next(p for p in out["pages"] if p.get("symbol") == "TSM")
    assert tsm["visits"] == 2
    assert not any(c["kind"] == "surface" and c["label"] == "browser" for c in out["chips"])
    assert "TSM" in out["summary_line"]
    # home section should not dominate chips when quote exists
    assert not any(c["label"] == "/home" for c in out["chips"])


def test_collapse_consecutive_url_focus():
    events = [
        {
            "ts": "t3",
            "type": "url_focus",
            "payload": {"url": "https://finance.yahoo.com/quote/TSM/"},
        },
        {
            "ts": "t2",
            "type": "url_focus",
            "payload": {"url": "https://finance.yahoo.com/quote/TSM"},
        },
        {
            "ts": "t1",
            "type": "url_focus",
            "payload": {"url": "https://finance.yahoo.com/"},
        },
    ]
    collapsed = collapse_events(events)
    assert len(collapsed) == 2
    assert collapsed[0]["visit_count"] == 2


def test_recent_url_focus_same_window():
    events = [
        {
            "ts": "2026-07-16T10:00:00Z",
            "type": "url_focus",
            "payload": {"url": "https://finance.yahoo.com/quote/TSM"},
        }
    ]
    hit = recent_url_focus_same(
        events,
        "https://finance.yahoo.com/quote/TSM",
        within_seconds=90,
        now_ts="2026-07-16T10:00:30Z",
    )
    assert hit is not None
    miss = recent_url_focus_same(
        events,
        "https://finance.yahoo.com/quote/TSM",
        within_seconds=90,
        now_ts="2026-07-16T10:05:00Z",
    )
    assert miss is None
