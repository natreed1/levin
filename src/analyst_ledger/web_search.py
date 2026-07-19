"""Lightweight public web search (Bing RSS) for Friend-room research."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List

_TAG_RE = re.compile(r"<[^>]+>")
_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z]{1,5})(?![A-Za-z0-9])")
_TICKER_STOPWORDS = {
    "AI",
    "AND",
    "API",
    "BE",
    "EPS",
    "ETF",
    "FOR",
    "FROM",
    "I",
    "IN",
    "KEY",
    "OR",
    "SEC",
    "THE",
    "TO",
    "TODAY",
    "URL",
    "US",
    "USD",
}
_HTTP_HEADERS = {
    "User-Agent": "AnalystLedger/0.1 local-public-research",
    "Accept": "application/json",
}
_PRIMARY_HOSTS = (
    "sec.gov",
    "apple.com",
    "microsoft.com",
    "nvidia.com",
    "aboutamazon.com",
    "investor.fb.com",
    "abc.xyz",
)
_CREDIBLE_HOSTS = (
    "reuters.com",
    "bloomberg.com",
    "cnbc.com",
    "finance.yahoo.com",
    "marketwatch.com",
)
_LOW_VALUE_HOSTS = (
    "wikipedia.org",
    "britannica.com",
    "answers.com",
    "quora.com",
)


def _strip_html(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def bing_search(query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Return up to ``limit`` public Bing RSS hits.

    Bing's RSS endpoint is stable, requires no API key, and avoids the
    anti-bot challenge pages returned by HTML search endpoints.
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit), 8))
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": q, "format": "rss", "count": limit}
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AnalystLedgerFriendResearch/0.1 (+local)",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        ET.ParseError,
    ):
        return []

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        title = _strip_html(item.findtext("title") or "")
        href = html.unescape((item.findtext("link") or "").strip())
        snippet = _strip_html(item.findtext("description") or "")
        published_at = ""
        pub_date = (item.findtext("pubDate") or "").strip()
        if pub_date:
            try:
                published_at = parsedate_to_datetime(pub_date).astimezone(
                    timezone.utc
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                published_at = pub_date
        if not title or not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        out.append(
            {
                "title": title,
                "url": href,
                "snippet": snippet,
                "published_at": published_at,
            }
        )
        if len(out) >= limit:
            break
    return out


def format_hits_for_prompt(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "(no search results)"
    lines = []
    for i, hit in enumerate(hits, 1):
        title = hit.get("title") or ""
        url = hit.get("url") or ""
        snippet = hit.get("snippet") or ""
        published = hit.get("published_at") or ""
        excerpt = hit.get("excerpt") or ""
        date_line = f"\n   Published: {published}" if published else ""
        excerpt_line = f"\n   Page excerpt: {excerpt}" if excerpt else ""
        lines.append(
            f"{i}. {title}\n   {url}{date_line}\n   {snippet}{excerpt_line}".rstrip()
        )
    return "\n".join(lines)


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url or "").hostname or "").casefold()


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def rank_search_hits(
    hits: List[Dict[str, Any]], *, intent: str = "general"
) -> List[Dict[str, Any]]:
    """Rank sources for research and remove encyclopedias from stock outlooks."""
    ranked: List[tuple[int, Dict[str, Any]]] = []
    for index, hit in enumerate(hits):
        host = _host(str(hit.get("url") or ""))
        if intent == "outlook" and _host_matches(host, _LOW_VALUE_HOSTS):
            continue
        score = -index
        if _host_matches(host, _PRIMARY_HOSTS):
            score += 100
        elif _host_matches(host, _CREDIBLE_HOSTS):
            score += 50
        if hit.get("published_at"):
            score += 15
        ranked.append((score, hit))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [hit for _, hit in ranked]


def finance_search_queries(
    symbol: str, company_name: str = "", *, intent: str = "outlook"
) -> List[str]:
    """Deterministic finance queries that favor primary and recent evidence."""
    label = f"{company_name} {symbol}".strip()
    year = datetime.now(timezone.utc).year
    if intent == "filings":
        return [f"{symbol} 10-Q 8-K site:sec.gov {year}"]
    if intent == "news":
        return [f"{label} latest company news {year}", f"{symbol} catalyst Reuters {year}"]
    return [
        f"{label} earnings guidance investor relations {year}",
        f"{symbol} analyst consensus price target outlook {year}",
        f"{symbol} 10-Q 8-K site:sec.gov {year}",
    ]


def fetch_trusted_excerpt(url: str, *, max_chars: int = 4000) -> str:
    """Fetch a bounded text excerpt from a trusted public research source."""
    host = _host(url)
    if not (
        _host_matches(host, _PRIMARY_HOSTS)
        or _host_matches(host, _CREDIBLE_HOSTS)
    ):
        return ""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AnalystLedger/0.1 local-public-research",
            "Accept": "text/html,application/xhtml+xml,text/plain",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310
            content_type = str(resp.headers.get("Content-Type") or "")
            if not any(kind in content_type for kind in ("text/", "html", "xml")):
                return ""
            raw = resp.read(250_000).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    raw = re.sub(r"(?is)<(?:script|style|noscript).*?>.*?</(?:script|style|noscript)>", " ", raw)
    return _strip_html(raw)[:max_chars]


def enrich_trusted_hits(
    hits: List[Dict[str, Any]], *, max_pages: int = 2
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    fetched = 0
    for hit in hits:
        copy = dict(hit)
        if fetched < max_pages:
            excerpt = fetch_trusted_excerpt(str(hit.get("url") or ""))
            if excerpt:
                copy["excerpt"] = excerpt
                fetched += 1
        enriched.append(copy)
    return enriched


def _get_json(url: str, *, sec: bool = False) -> Dict[str, Any]:
    headers = dict(_HTTP_HEADERS)
    if sec:
        headers["User-Agent"] = "AnalystLedger local-public-research analyst@example.com"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def extract_tickers(text: str, *, limit: int = 2) -> List[str]:
    """Extract explicit uppercase ticker-like tokens from a research request."""
    out: List[str] = []
    for match in _TICKER_RE.finditer(text or ""):
        symbol = match.group(1).upper()
        if symbol in _TICKER_STOPWORDS or symbol in out:
            continue
        out.append(symbol)
        if len(out) >= max(1, limit):
            break
    return out


def fetch_yahoo_chart_snapshot(symbol: str) -> Dict[str, Any]:
    """Fetch current market fields from Yahoo's unauthenticated chart endpoint."""
    ticker = symbol.upper().strip()
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?range=5d&interval=1d"
    )
    payload = _get_json(url)
    results = ((payload.get("chart") or {}).get("result") or [])
    if not results:
        raise RuntimeError("Yahoo chart returned no result")
    meta = results[0].get("meta") or {}
    market_time = meta.get("regularMarketTime")
    as_of = None
    if market_time:
        as_of = datetime.fromtimestamp(int(market_time), tz=timezone.utc).isoformat()
    return {
        "symbol": ticker,
        "name": meta.get("longName") or meta.get("shortName") or ticker,
        "price": meta.get("regularMarketPrice"),
        "previous_close": meta.get("chartPreviousClose"),
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
        "currency": meta.get("currency"),
        "as_of": as_of,
        "source_url": url,
    }


def _sec_cik_for_symbol(symbol: str) -> str:
    payload = _get_json(
        "https://www.sec.gov/files/company_tickers.json", sec=True
    )
    for row in payload.values():
        if isinstance(row, dict) and str(row.get("ticker") or "").upper() == symbol:
            return str(row.get("cik_str") or "").zfill(10)
    raise RuntimeError(f"SEC CIK not found for {symbol}")


def _latest_quarterly_fact(
    facts: Dict[str, Any], tags: List[str]
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    us_gaap = facts.get("us-gaap") or {}
    for tag in tags:
        units = ((us_gaap.get(tag) or {}).get("units") or {})
        for records in units.values():
            for record in records or []:
                if (
                    record.get("form") == "10-Q"
                    and record.get("frame")
                    and re.search(r"Q[1-4]$", str(record.get("frame")))
                ):
                    candidates.append({**record, "tag": tag})
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda item: (
            str(item.get("end") or ""),
            str(item.get("filed") or ""),
        ),
    )


def fetch_sec_financial_snapshot(symbol: str) -> Dict[str, Any]:
    """Fetch latest discrete quarterly revenue and diluted EPS from SEC XBRL."""
    ticker = symbol.upper().strip()
    cik = _sec_cik_for_symbol(ticker)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    payload = _get_json(url, sec=True)
    facts = payload.get("facts") or {}
    revenue = _latest_quarterly_fact(
        facts,
        [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ],
    )
    eps = _latest_quarterly_fact(
        facts, ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]
    )
    return {
        "symbol": ticker,
        "entity": payload.get("entityName") or ticker,
        "revenue": revenue.get("val"),
        "revenue_period_start": revenue.get("start"),
        "revenue_period_end": revenue.get("end"),
        "revenue_filed": revenue.get("filed"),
        "revenue_fiscal_period": revenue.get("fp"),
        "revenue_accession": revenue.get("accn"),
        "diluted_eps": eps.get("val"),
        "eps_period_end": eps.get("end"),
        "eps_filed": eps.get("filed"),
        "eps_accession": eps.get("accn"),
        "source_url": url,
    }


def format_financial_context(text: str) -> str:
    """Return deterministic public market/filing data for explicit tickers."""
    sections: List[str] = []
    for symbol in extract_tickers(text):
        try:
            quote = fetch_yahoo_chart_snapshot(symbol)
            sections.append(
                "Yahoo Finance chart data for {symbol} ({name}):\n"
                "- regular-market price: {price} {currency}\n"
                "- previous close: {previous_close}\n"
                "- day range: {day_low} to {day_high}; volume: {volume}\n"
                "- 52-week range: {fifty_two_week_low} to {fifty_two_week_high}\n"
                "- market timestamp (UTC): {as_of}\n"
                "- source: {source_url}".format(**quote)
            )
        except Exception as exc:  # noqa: BLE001
            sections.append(f"Yahoo data for {symbol} unavailable: {exc}")
        try:
            sec = fetch_sec_financial_snapshot(symbol)
            sections.append(
                "SEC company facts for {symbol} ({entity}):\n"
                "- latest discrete 10-Q revenue: {revenue} USD "
                "(period {revenue_period_start} to {revenue_period_end}, "
                "company fiscal period {revenue_fiscal_period}, "
                "filed {revenue_filed}, accession {revenue_accession})\n"
                "- diluted EPS for that quarter: {diluted_eps} "
                "(period end {eps_period_end}, filed {eps_filed}, "
                "accession {eps_accession})\n"
                "- source: {source_url}".format(**sec)
            )
        except Exception as exc:  # noqa: BLE001
            sections.append(f"SEC filing data for {symbol} unavailable: {exc}")
    return "\n\n".join(sections) or "(no explicit ticker found for structured finance data)"


def build_financial_brief(text: str) -> str:
    """Render a factual ticker brief without model-generated embellishment."""
    symbols = extract_tickers(text, limit=1)
    if not symbols:
        return ""
    symbol = symbols[0]
    verified: List[str] = []
    unavailable: List[str] = []
    quote: Dict[str, Any] = {}
    sec: Dict[str, Any] = {}
    try:
        quote = fetch_yahoo_chart_snapshot(symbol)
        verified.extend(
            [
                (
                    f"- {symbol} regular-market price: {quote.get('price')} "
                    f"{quote.get('currency')}; market timestamp: {quote.get('as_of')}."
                ),
                (
                    f"- {symbol} 52-week range: {quote.get('fifty_two_week_low')} "
                    f"to {quote.get('fifty_two_week_high')}; previous close: "
                    f"{quote.get('previous_close')}."
                ),
                f"- Yahoo source: {quote.get('source_url')}",
            ]
        )
    except Exception as exc:  # noqa: BLE001
        unavailable.append(f"- Yahoo market snapshot unavailable: {exc}")
    try:
        sec = fetch_sec_financial_snapshot(symbol)
        revenue = sec.get("revenue")
        revenue_display = (
            f"${float(revenue) / 1_000_000_000:.3f} billion"
            if isinstance(revenue, (int, float))
            else str(revenue)
        )
        verified.extend(
            [
                (
                    f"- Latest discrete SEC 10-Q revenue: {revenue_display}; "
                    f"period {sec.get('revenue_period_start')} to "
                    f"{sec.get('revenue_period_end')}; company fiscal period "
                    f"{sec.get('revenue_fiscal_period')}; filed "
                    f"{sec.get('revenue_filed')}; accession "
                    f"{sec.get('revenue_accession')}."
                ),
                (
                    f"- Diluted EPS for that discrete quarter: "
                    f"${sec.get('diluted_eps')} per share."
                ),
                f"- SEC source: {sec.get('source_url')}",
            ]
        )
    except Exception as exc:  # noqa: BLE001
        unavailable.append(f"- SEC quarterly facts unavailable: {exc}")

    analysis = [
        "- The price and 52-week range locate the shares within recent trading "
        "bounds; they do not establish fair value.",
        "- One quarter of revenue and EPS establishes that period's reported "
        "results only. Growth, margins, and valuation require comparison periods "
        "and additional filing or market data.",
    ]
    if not unavailable:
        unavailable.append(
            "- None of the requested snapshot fields were unavailable. Trend and "
            "valuation conclusions remain outside this evidence set."
        )
    return (
        "Verified evidence\n"
        + "\n".join(verified)
        + "\n\nAnalysis\n"
        + "\n".join(analysis)
        + "\n\nUnavailable / next checks\n"
        + "\n".join(unavailable)
    )
