from analyst_ledger.browser import host_denied, merge_quote_scrape, parse_url
import pytest


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


def test_allow_any_and_denylist():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_url("https://arxiv.org/abs/1234.5678")
    any_page = parse_url("https://arxiv.org/abs/1234.5678", allow_any=True)
    assert any_page["host"] == "arxiv.org"
    assert any_page["capture_mode"] == "any"

    assert host_denied("mail.google.com")
    with pytest.raises(ValueError, match="denied"):
        parse_url("https://mail.google.com/mail/u/0/", allow_any=True)
    with pytest.raises(ValueError, match="denied"):
        parse_url("http://127.0.0.1:8788/", allow_any=True)
    with pytest.raises(ValueError, match="scheme"):
        parse_url("chrome://extensions", allow_any=True)


def test_research_pubs_and_google_news():
    # Allowlisted after research-pub expansion
    assert parse_url("https://www.theverge.com/ai-artificial-intelligence")["host"] == "www.theverge.com"
    assert parse_url("https://english.news.cn/")["host"] == "english.news.cn"
    assert parse_url("https://news.google.com/home")["host"] == "news.google.com"
    # Google redirector unwraps to destination
    dest = parse_url(
        "https://www.google.com/url?q=https://www.theverge.com/2024/1/1/foo&sa=U",
        allow_any=True,
    )
    assert dest["host"] == "www.theverge.com"
    # Consent still denied
    assert host_denied("consent.google.com")
    with pytest.raises(ValueError, match="denied"):
        parse_url("https://consent.google.com/m?continue=https://news.google.com/", allow_any=True)
    # Company sites need deep research
    with pytest.raises(ValueError, match="allowlisted"):
        parse_url("https://www.tsmc.com/english")
    assert parse_url("https://www.tsmc.com/english", allow_any=True)["capture_mode"] == "any"
    # SEC always allowlisted
    assert parse_url("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany")["section"] == "edgar"

