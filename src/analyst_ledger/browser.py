"""Browser URL capture helpers (allowlist + deep-research any-site)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# Default research surfaces (auto-capture when site toggle is on).
ALLOWED_HOST_SUFFIXES = (
    "finance.yahoo.com",
    "yahoo.com",
    "tradingview.com",
    "www.tradingview.com",
    "sec.gov",
    "www.sec.gov",
    "edgar.sec.gov",
    "seekingalpha.com",
    "www.seekingalpha.com",
    "bloomberg.com",
    "www.bloomberg.com",
    "ft.com",
    "www.ft.com",
    "reuters.com",
    "www.reuters.com",
    "cnbc.com",
    "www.cnbc.com",
    # Deep-research pubs (toggleable in extension; also allowlisted for auto)
    "theverge.com",
    "www.theverge.com",
    "news.cn",
    "english.news.cn",
    "xinhuanet.com",
    "www.xinhuanet.com",
    "news.google.com",
    # Public OSINT / social research (manual or deep-research still prefer allow_any)
    "x.com",
    "twitter.com",
    "www.twitter.com",
)

# Never capture these (privacy / noise), even in deep research.
DENIED_HOST_SUFFIXES = (
    "accounts.google.com",
    "mail.google.com",
    "gmail.com",
    "outlook.live.com",
    "outlook.office.com",
    "login.microsoftonline.com",
    "appleid.apple.com",
    "icloud.com",
    "chase.com",
    "bankofamerica.com",
    "wellsfargo.com",
    "paypal.com",
    "venmo.com",
    "stripe.com",
    "consent.yahoo.com",
    "guce.yahoo.com",
    "login.yahoo.com",
    "api.login.yahoo.com",
    "consent.google.com",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
)

DENIED_SCHEMES = ("chrome:", "chrome-extension:", "about:", "edge:", "brave:", "devtools:")

_YF_QUOTE_RE = re.compile(
    r"/quote/(?:[A-Z]{1,4}:)?([A-Z][A-Z0-9.\-]{0,15})(?:/|$|\?)",
    re.I,
)
_TV_SYMBOL_RE = re.compile(r"/chart/(?:[^/]+/)?([A-Z0-9.\-_]+)", re.I)


def _suffix_match(host: str, suffixes: tuple) -> bool:
    host = (host or "").lower().strip().rstrip(".")
    if not host:
        return False
    for suffix in suffixes:
        s = suffix.lower()
        if host == s or host.endswith("." + s):
            return True
    return False


def host_denied(host: str) -> bool:
    return _suffix_match(host, DENIED_HOST_SUFFIXES)


def host_allowed(host: str) -> bool:
    """True if host is on the default research allowlist (and not denied)."""
    if host_denied(host):
        return False
    return _suffix_match(host, ALLOWED_HOST_SUFFIXES)


def normalize_url_key(url: str) -> str:
    """Canonical key for dedupe: scheme://host/path without trailing slash or noise query."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    scheme = (parsed.scheme or "https").lower()
    return f"{scheme}://{host}{path}"


def parse_url(url: str, *, allow_any: bool = False) -> Dict[str, Any]:
    """
    Normalize a URL into a url_focus payload.

    By default only allowlisted research hosts are accepted.
    With allow_any=True (deep research / manual), any http(s) host is
    accepted except the denylist.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("empty url")
    lower = raw.lower()
    for scheme in DENIED_SCHEMES:
        if lower.startswith(scheme):
            raise ValueError(f"scheme not allowed: {scheme.rstrip(':')}")

    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    scheme = (parsed.scheme or "https").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"scheme not allowed: {scheme}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("missing host")

    # Unwrap Google redirector → destination publication
    if host in {"www.google.com", "google.com"} and parsed.path.startswith("/url"):
        qs = parse_qs(parsed.query)
        dest = (qs.get("q") or qs.get("url") or [None])[0]
        if dest:
            return parse_url(dest, allow_any=allow_any)

    if host_denied(host):
        raise ValueError(f"host denied: {host}")
    if not allow_any and not host_allowed(host):
        raise ValueError(f"host not allowlisted: {host}")

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    title = ""
    symbol = _extract_symbol(host, path, parsed.query)
    section = _guess_section(host, path)
    canon = f"{scheme}://{host}{path}"

    return {
        "url": canon,
        "host": host,
        "path": path,
        "symbol": symbol,
        "section": section,
        "title": title,
        "query_keys": sorted(parse_qs(parsed.query).keys())[:20],
        "capture_mode": "any" if allow_any and not host_allowed(host) else "allowlist",
    }


def _extract_symbol(host: str, path: str, query: str) -> Optional[str]:
    if "yahoo" in host:
        m = _YF_QUOTE_RE.search(path)
        if m:
            return m.group(1).upper()
        qs = parse_qs(query)
        if "p" in qs and qs["p"]:
            return qs["p"][0].upper()
        if "s" in qs and qs["s"]:
            return qs["s"][0].upper()
    if "tradingview" in host:
        m = _TV_SYMBOL_RE.search(path)
        if m:
            return m.group(1).upper()
        qs = parse_qs(query)
        if "symbol" in qs and qs["symbol"]:
            raw = qs["symbol"][0]
            return raw.split(":")[-1].upper()
    return None


_YF_SECTION_ALIASES = {
    "key-statistics": "statistics",
    "historical-data": "history",
    "financials": "financials",
    "analysis": "analysis",
    "holders": "holders",
    "profile": "profile",
    "news": "news",
    "chart": "chart",
    "community": "community",
    "options": "options",
    "sustainability": "sustainability",
}


def _guess_section(host: str, path: str) -> Optional[str]:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "home"
    if "yahoo" in host:
        if len(parts) >= 3 and parts[0] == "quote":
            raw = parts[2].lower()
            return _YF_SECTION_ALIASES.get(raw, raw)
        if parts[0] == "quote":
            return "quote"
        return parts[0].lower()
    if "sec.gov" in host:
        if "edgar" in path.lower():
            return "edgar"
        return parts[0].lower()
    return parts[0].lower()


def merge_quote_scrape(parsed: Dict[str, Any], scrape: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach optional Yahoo DOM scrape fields onto a url_focus payload."""
    if not scrape or not isinstance(scrape, dict):
        return parsed
    out = dict(parsed)
    quote: Dict[str, Any] = {}
    if scrape.get("price") is not None:
        try:
            quote["price"] = float(scrape["price"])
        except (TypeError, ValueError):
            pass
    if scrape.get("change") is not None:
        try:
            quote["change"] = float(scrape["change"])
        except (TypeError, ValueError):
            pass
    if scrape.get("change_pct") is not None:
        try:
            quote["change_pct"] = float(scrape["change_pct"])
        except (TypeError, ValueError):
            pass
    for key in ("currency", "market_state", "earnings", "as_of"):
        val = scrape.get(key)
        if val:
            quote[key] = str(val)[:120]
    if quote:
        out["quote"] = quote
    return out


def summarize_url_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate url_focus events for ritual mining."""
    hosts: Dict[str, int] = {}
    symbols: Dict[str, int] = {}
    sections: Dict[str, int] = {}
    paths: List[str] = []
    for ev in events:
        if ev.get("type") != "url_focus":
            continue
        p = ev.get("payload") or {}
        host = p.get("host") or ""
        if host:
            hosts[host] = hosts.get(host, 0) + 1
        sym = p.get("symbol")
        if sym:
            symbols[str(sym).upper()] = symbols.get(str(sym).upper(), 0) + 1
        sec = p.get("section")
        if sec:
            sections[str(sec)] = sections.get(str(sec), 0) + 1
        if p.get("path"):
            paths.append(str(p["path"]))
    return {
        "hosts": dict(sorted(hosts.items(), key=lambda x: -x[1])),
        "symbols": dict(sorted(symbols.items(), key=lambda x: -x[1])),
        "sections": dict(sorted(sections.items(), key=lambda x: -x[1])),
        "sample_paths": paths[:30],
    }
