from analyst_ledger.browser import merge_quote_scrape, parse_url


def test_yahoo_subtab_sections():
    stats = parse_url("https://finance.yahoo.com/quote/TSM/key-statistics")
    assert stats["symbol"] == "TSM"
    assert stats["section"] == "statistics"
    assert stats["path"].endswith("/key-statistics")

    news = parse_url("https://finance.yahoo.com/quote/TSM/news")
    assert news["section"] == "news"

    hist = parse_url("https://finance.yahoo.com/quote/TSM/historical-data")
    assert hist["section"] == "history"


def test_merge_quote_scrape():
    parsed = parse_url("https://finance.yahoo.com/quote/TSM")
    out = merge_quote_scrape(
        parsed,
        {
            "price": 419.48,
            "change_pct": -0.22,
            "change": -0.91,
            "earnings": "TSM Q2 2026 earnings call",
        },
    )
    assert out["quote"]["price"] == 419.48
    assert out["quote"]["change_pct"] == -0.22
    assert "earnings" in out["quote"]["earnings"].lower()
