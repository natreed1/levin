"""Structured, evidence-gated stock outlook research."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .web_search import (
    _get_json,
    _sec_cik_for_symbol,
    extract_tickers,
    fetch_sec_financial_snapshot,
    fetch_yahoo_chart_snapshot,
)

COMPANY_ALIASES = {
    "apple": "AAPL",
    "microsoft": "MSFT",
    "nvidia": "NVDA",
    "amazon": "AMZN",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "meta": "META",
    "tesla": "TSLA",
    "amd": "AMD",
}
OUTLOOK_RE = re.compile(
    r"\b(?:outlook|buy|sell|forecast|guidance|consensus|price\s+target|"
    r"recommendation|upside|downside|bull|bear|valuation)\b",
    re.IGNORECASE,
)
NEWS_RE = re.compile(r"\b(?:news|headline|development|catalyst)\b", re.IGNORECASE)
FILINGS_RE = re.compile(r"\b(?:filing|10-[qk]|8-k|sec)\b", re.IGNORECASE)
SNAPSHOT_RE = re.compile(
    r"\b(?:price|quote|revenue|eps|quarter|52[- ]week|snapshot)\b",
    re.IGNORECASE,
)


def classify_finance_intent(text: str) -> str:
    value = text or ""
    if OUTLOOK_RE.search(value):
        return "outlook"
    if FILINGS_RE.search(value):
        return "filings"
    if NEWS_RE.search(value):
        return "news"
    if SNAPSHOT_RE.search(value):
        return "snapshot"
    return "general"


def _alias_symbol(text: str) -> Optional[str]:
    value = (text or "").casefold()
    for name, symbol in COMPANY_ALIASES.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", value):
            return symbol
    return None


def _sec_name_symbol(text: str) -> Optional[str]:
    """Best-effort company-name resolution against the SEC ticker directory."""
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").casefold())
        if word
        not in {
            "stock",
            "shares",
            "company",
            "research",
            "outlook",
            "forecast",
            "buy",
            "sell",
            "the",
            "a",
            "an",
        }
    ]
    if not words:
        return None
    try:
        payload = _get_json(
            "https://www.sec.gov/files/company_tickers.json", sec=True
        )
    except Exception:  # noqa: BLE001
        return None
    candidates: List[tuple[int, str]] = []
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").casefold()
        ticker = str(row.get("ticker") or "").upper()
        score = sum(1 for word in words if len(word) > 2 and word in title)
        if score and ticker:
            candidates.append((score, ticker))
    return max(candidates)[1] if candidates else None


def resolve_symbol(
    text: str, context_text: str = "", *, allow_network: bool = True
) -> Optional[str]:
    """Resolve a ticker from the request first, then recent chat context."""
    tickers = extract_tickers(text)
    if tickers:
        return tickers[0]
    alias = _alias_symbol(text)
    if alias:
        return alias
    for candidate in reversed((context_text or "").splitlines()):
        alias = _alias_symbol(candidate)
        if alias:
            return alias
        tickers = extract_tickers(candidate)
        if tickers:
            return tickers[0]
    if not allow_network:
        return None
    return _sec_name_symbol(text) or _sec_name_symbol(context_text)


def _yahoo_history(symbol: str, *, range_: str = "1y") -> Dict[str, Any]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol.upper())
        + "?"
        + urllib.parse.urlencode({"range": range_, "interval": "1d"})
    )
    payload = _get_json(url)
    results = ((payload.get("chart") or {}).get("result") or [])
    if not results:
        raise RuntimeError(f"Yahoo history unavailable for {symbol}")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("adjclose") or [{}])[0])
    closes = quote.get("adjclose") or []
    points = [
        (int(ts), float(close))
        for ts, close in zip(timestamps, closes)
        if ts is not None and close is not None
    ]
    if len(points) < 2:
        raise RuntimeError(f"insufficient Yahoo history for {symbol}")
    first_ts, first = points[0]
    last_ts, last = points[-1]
    return {
        "symbol": symbol.upper(),
        "first": first,
        "last": last,
        "return_pct": ((last / first) - 1.0) * 100.0,
        "start": datetime.fromtimestamp(first_ts, tz=timezone.utc).date().isoformat(),
        "end": datetime.fromtimestamp(last_ts, tz=timezone.utc).date().isoformat(),
        "source_url": url,
    }


def _quarterly_records(
    facts: Dict[str, Any], tags: List[str]
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    us_gaap = facts.get("us-gaap") or {}
    for tag in tags:
        for unit, values in ((us_gaap.get(tag) or {}).get("units") or {}).items():
            for value in values or []:
                if (
                    value.get("form") == "10-Q"
                    and value.get("frame")
                    and re.search(r"Q[1-4]$", str(value.get("frame")))
                ):
                    records.append({**value, "tag": tag, "unit": unit})
    latest_by_period: Dict[str, Dict[str, Any]] = {}
    for record in records:
        end = str(record.get("end") or "")
        current = latest_by_period.get(end)
        if current is None or str(record.get("filed") or "") > str(
            current.get("filed") or ""
        ):
            latest_by_period[end] = record
    return sorted(
        latest_by_period.values(),
        key=lambda item: (str(item.get("end") or ""), str(item.get("filed") or "")),
        reverse=True,
    )


def _growth_pair(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not records:
        return {}
    latest = records[0]
    frame = str(latest.get("frame") or "")
    match = re.match(r"CY(\d{4})(Q[1-4])$", frame)
    prior = None
    if match:
        target = f"CY{int(match.group(1)) - 1}{match.group(2)}"
        prior = next(
            (record for record in records[1:] if record.get("frame") == target),
            None,
        )
    if prior is None and len(records) > 1:
        prior = records[1]
    growth = None
    if prior and isinstance(latest.get("val"), (int, float)) and prior.get("val"):
        growth = ((float(latest["val"]) / float(prior["val"])) - 1.0) * 100.0
    return {"latest": latest, "prior": prior, "growth_pct": growth}


def fetch_sec_trends(symbol: str) -> Dict[str, Any]:
    cik = _sec_cik_for_symbol(symbol)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    payload = _get_json(url, sec=True)
    facts = payload.get("facts") or {}
    revenue = _growth_pair(
        _quarterly_records(
            facts,
            [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "SalesRevenueNet",
            ],
        )
    )
    eps = _growth_pair(
        _quarterly_records(
            facts, ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]
        )
    )
    return {
        "symbol": symbol.upper(),
        "entity": payload.get("entityName") or symbol.upper(),
        "revenue": revenue,
        "eps": eps,
        "source_url": url,
    }


def fetch_recent_filings(symbol: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    cik = _sec_cik_for_symbol(symbol)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    payload = _get_json(url, sec=True)
    recent = ((payload.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    filings: List[Dict[str, Any]] = []
    for index, form in enumerate(forms):
        if form not in {"10-Q", "10-K", "8-K"}:
            continue
        accession = str((recent.get("accessionNumber") or [""])[index])
        document = str((recent.get("primaryDocument") or [""])[index])
        accession_path = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession_path}/{document}"
        )
        filings.append(
            {
                "form": form,
                "filed": (recent.get("filingDate") or [""])[index],
                "accession": accession,
                "url": filing_url,
            }
        )
        if len(filings) >= limit:
            break
    return filings


def fetch_finnhub_outlook(symbol: str) -> Dict[str, Any]:
    token = os.environ.get("ANALYST_FINNHUB_API_KEY", "").strip()
    if not token:
        return {"available": False, "reason": "ANALYST_FINNHUB_API_KEY not set"}

    def call(path: str) -> Any:
        url = "https://finnhub.io/api/v1/" + path + "&" + urllib.parse.urlencode(
            {"token": token}
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AnalystLedger/0.1", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    encoded = urllib.parse.urlencode({"symbol": symbol.upper()})
    try:
        targets = call("stock/price-target?" + encoded)
        recommendations = call("stock/recommendation?" + encoded)
        return {
            "available": True,
            "price_target": targets if isinstance(targets, dict) else {},
            "recommendations": (
                recommendations[:4] if isinstance(recommendations, list) else []
            ),
            "source": "Finnhub",
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)[:160]}


def build_outlook_evidence(
    symbol: str, *, include_provider: bool = True
) -> Dict[str, Any]:
    errors: List[str] = []
    evidence: Dict[str, Any] = {
        "symbol": symbol.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
    }
    for key, fn in (
        ("market", lambda: fetch_yahoo_chart_snapshot(symbol)),
        ("sec_latest", lambda: fetch_sec_financial_snapshot(symbol)),
        ("sec_trends", lambda: fetch_sec_trends(symbol)),
        ("filings", lambda: fetch_recent_filings(symbol)),
        ("performance", lambda: _yahoo_history(symbol)),
        ("benchmark", lambda: _yahoo_history("SPY")),
    ):
        try:
            evidence[key] = fn()
        except Exception as exc:  # noqa: BLE001
            evidence[key] = None
            errors.append(f"{key}: {exc}")
    performance = evidence.get("performance") or {}
    benchmark = evidence.get("benchmark") or {}
    if performance.get("return_pct") is not None and benchmark.get("return_pct") is not None:
        evidence["relative_return_pct"] = (
            float(performance["return_pct"]) - float(benchmark["return_pct"])
        )
    else:
        evidence["relative_return_pct"] = None
    evidence["provider"] = (
        fetch_finnhub_outlook(symbol)
        if include_provider
        else {"available": False, "reason": "provider disabled"}
    )
    trends = evidence.get("sec_trends") or {}
    has_comparable_facts = bool(
        ((trends.get("revenue") or {}).get("latest"))
        or ((trends.get("eps") or {}).get("latest"))
    )
    evidence["ready"] = bool(
        evidence.get("market")
        and has_comparable_facts
        and (evidence.get("filings") or evidence.get("trusted_sources"))
        and evidence.get("relative_return_pct") is not None
    )
    return evidence


def format_outlook_evidence(evidence: Dict[str, Any]) -> str:
    """Serialize only verified fields for model synthesis and audit."""
    return json.dumps(evidence, indent=2, sort_keys=True, default=str)


def render_outlook_brief(
    evidence: Dict[str, Any], *, contrarian: bool = False
) -> str:
    """Deterministic fallback outlook built only from the evidence bundle."""
    def pct(value: Any) -> str:
        return f"{float(value):.1f}%" if isinstance(value, (int, float)) else "unavailable"

    def amount(value: Any, unit: Any) -> str:
        if isinstance(value, (int, float)) and str(unit) == "USD" and abs(value) >= 1e9:
            return f"${float(value) / 1e9:.1f}B"
        return f"{value} {unit}".strip()

    symbol = str(evidence.get("symbol") or "?")
    market = evidence.get("market") or {}
    trends = evidence.get("sec_trends") or {}
    revenue = trends.get("revenue") or {}
    eps = trends.get("eps") or {}
    revenue_latest = revenue.get("latest") or {}
    revenue_prior = revenue.get("prior") or {}
    eps_latest = eps.get("latest") or {}
    eps_prior = eps.get("prior") or {}
    market_url = market.get("source_url") or "(source unavailable)"
    sec_url = trends.get("source_url") or "(source unavailable)"
    performance = evidence.get("performance") or {}
    benchmark = evidence.get("benchmark") or {}
    provider = evidence.get("provider") or {}
    filings = evidence.get("filings") or []

    verified = [
        (
            f"- {symbol}: {market.get('price')} {market.get('currency')} at "
            f"{market.get('as_of')}; 52-week {market.get('fifty_two_week_low')}–"
            f"{market.get('fifty_two_week_high')}. {market_url}"
        ),
        (
            f"- Quarterly revenue {amount(revenue_latest.get('val'), revenue_latest.get('unit'))} "
            f"vs {amount(revenue_prior.get('val'), revenue_prior.get('unit'))} "
            f"({pct(revenue.get('growth_pct'))}); diluted "
            f"EPS {eps_latest.get('val')} vs {eps_prior.get('val')} "
            f"({pct(eps.get('growth_pct'))}). {sec_url}"
        ),
        (
            f"- One-year return {pct(performance.get('return_pct'))} vs SPY "
            f"{pct(benchmark.get('return_pct'))}; relative "
            f"{pct(evidence.get('relative_return_pct'))}. "
            f"{performance.get('source_url')}"
        ),
    ]

    growth_values = [
        value
        for value in (revenue.get("growth_pct"), eps.get("growth_pct"))
        if isinstance(value, (int, float))
    ]
    relative = evidence.get("relative_return_pct")
    positive_growth = bool(growth_values) and all(value > 0 for value in growth_values)
    positive_relative = isinstance(relative, (int, float)) and relative > 0
    if contrarian:
        bull = (
            "- Caution weakens if revenue/EPS growth persists across additional "
            "quarters and relative performance remains positive."
        )
        bear = (
            "- The trap is extrapolating one quarter and recent momentum into fair "
            "value or a durable outlook."
        )
    else:
        bull = (
            "- A constructive scenario is supported conditionally by "
            + ("positive year-over-year revenue/EPS comparisons" if positive_growth else "improving future revenue/EPS comparisons")
            + (" and positive relative performance versus SPY." if positive_relative else "; relative performance does not currently add support.")
        )
        bear = (
            "- Caution remains credible: there is no cash-flow valuation, guidance, "
            "or proof that one-quarter growth persists."
        )

    filing_lines = [
        f"- {item.get('form')} filed {item.get('filed')}: {item.get('url')}"
        for item in filings[:1]
    ] or ["- No recent 10-Q, 10-K, or 8-K metadata was retrieved."]
    provider_line = (
        f"- Finnhub consensus evidence is available: {provider.get('source')}."
        if provider.get("available")
        else f"- Forward consensus is unavailable: {provider.get('reason')}."
    )
    readiness = (
        "The minimum public evidence gate passed."
        if evidence.get("ready")
        else "The minimum evidence gate did not pass; all scenarios are provisional."
    )
    return (
        "Verified facts\n"
        + "\n".join(verified)
        + f"\n- {readiness}\n\n"
        + "Bull scenario\n"
        + bull
        + "\n\nBear scenario\n"
        + bear
        + "\n\nCatalysts\n"
        + "\n".join(filing_lines)
        + "\n\nRisks\n"
        + provider_line
        + "\n- Price ranges and relative returns do not establish fair value.\n\n"
        + "What would change the view\n"
        + "- Add more comparable quarters, cash-flow and margin trends, guidance, "
        + "and a valuation using compatible trailing or forward measures."
    )
