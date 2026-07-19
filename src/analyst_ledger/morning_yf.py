"""Deterministic morning Yahoo Finance scan runner."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import Ledger
from .paths import artifacts_dir, ritual_specs_dir
from .schema import Event, Sensitivity, Surface, utc_now_iso


DEFAULT_WATCHLIST = ["NVDA", "AAPL", "SPY"]


def run_morning_yf_scan(
    ledger: Optional[Ledger] = None,
    watchlist: Optional[List[str]] = None,
    ritual_id: str = "morning_yahoo_scan",
    stub: bool = False,
    require_approved: bool = False,
    write_obsidian: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Fetch quote-like fields for a watchlist and draft a morning scan note.

    Uses Yahoo's public quote API by default; ``stub=True`` for offline/tests.
    Does not place trades. Creates a ledger session + note + ritual_run event.
    """
    ledger = ledger or Ledger()
    spec_path = ritual_specs_dir() / f"{ritual_id}.json"
    spec: Dict[str, Any] = {}
    if spec_path.exists():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if require_approved and not spec.get("approved"):
            raise RuntimeError(
                f"Spec '{ritual_id}' is not approved. Run: analyst rituals approve {ritual_id}"
            )
        watchlist = watchlist or list(spec.get("watchlist") or [])
    elif require_approved:
        raise RuntimeError(
            f"No approved spec for '{ritual_id}'. "
            f"Run: analyst rituals suggest {ritual_id} && analyst rituals approve {ritual_id}"
        )

    symbols = [s.upper() for s in (watchlist or DEFAULT_WATCHLIST) if s]
    if not symbols:
        symbols = list(DEFAULT_WATCHLIST)

    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for sym in symbols:
        try:
            if stub or os.environ.get("ANALYST_YF_STUB", "").strip() in {
                "1",
                "true",
                "yes",
            }:
                rows.append(_stub_quote(sym))
            else:
                rows.append(fetch_yahoo_quote(sym))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")
            rows.append({"symbol": sym, "error": str(exc)})

    note = render_morning_note(rows)
    session = ledger.start_session(
        title=f"Morning YF scan {datetime.now().strftime('%Y-%m-%d')}",
        surface=Surface.RITUAL.value,
        sensitivity=Sensitivity.INTERNAL.value,
        desk_tag="routine",
    )
    ledger.add_note(note, session_id=session.session_id, surface=Surface.RITUAL.value)

    out_dir = artifacts_dir() / session.session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    note_path = out_dir / "morning_yf_scan.md"
    note_path.write_text(note, encoding="utf-8")
    ledger.attach_artifact(note_path, session_id=session.session_id)

    if write_obsidian:
        write_obsidian = Path(write_obsidian).expanduser()
        write_obsidian.parent.mkdir(parents=True, exist_ok=True)
        write_obsidian.write_text(note, encoding="utf-8")
        ledger.append_event(
            Event(
                type="artifact_attach",
                surface=Surface.NOTES.value,
                session_id=session.session_id,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "path": str(write_obsidian.resolve()),
                    "kind": "obsidian_write",
                    "sha256": "",
                    "mime": "text/markdown",
                },
            )
        )

    ledger.append_event(
        Event(
            type="ritual_run",
            surface=Surface.RITUAL.value,
            session_id=session.session_id,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={
                "ritual_id": ritual_id,
                "runner": "morning_yf_scan",
                "symbols": symbols,
                "stub": stub,
                "errors": errors,
                "rows": rows,
            },
        )
    )
    ledger.end_session(session_id=session.session_id, tags=["neutral"])

    return {
        "status": "ok",
        "session_id": session.session_id,
        "ritual_id": ritual_id,
        "note_path": str(note_path),
        "obsidian_path": str(write_obsidian) if write_obsidian else None,
        "rows": rows,
        "errors": errors,
        "note": note,
    }


def fetch_yahoo_quote(symbol: str) -> Dict[str, Any]:
    """Pull a lightweight quote payload from Yahoo's public query API."""
    symbol = symbol.upper().strip()
    params = urllib.parse.urlencode(
        {
            "symbols": symbol,
            "fields": "regularMarketPrice,regularMarketChangePercent,regularMarketVolume,"
            "marketCap,shortName,regularMarketPreviousClose,trailingPE,earningsTimestamp",
        }
    )
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "analyst-ledger/0.1 (local research automation; personal use)",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            from .web_search import fetch_yahoo_chart_snapshot

            chart = fetch_yahoo_chart_snapshot(symbol)
            return {
                "symbol": symbol,
                "name": chart.get("name") or symbol,
                "price": chart.get("price"),
                "pct_change": None,
                "volume": chart.get("volume"),
                "market_cap": None,
                "prev_close": chart.get("previous_close"),
                "trailing_pe": None,
                "next_earnings": None,
                "headlines": fetch_yahoo_headlines(symbol, limit=3),
                "source": "yahoo_chart_fallback",
            }
        raise RuntimeError(f"Yahoo HTTP {exc.code}") from exc

    results = (
        (payload.get("quoteResponse") or {}).get("result")
        or []
    )
    if not results:
        raise RuntimeError("no quote result")
    q = results[0]
    earnings_ts = q.get("earningsTimestamp")
    next_earnings = None
    if earnings_ts:
        try:
            next_earnings = datetime.utcfromtimestamp(int(earnings_ts)).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            next_earnings = str(earnings_ts)

    headlines = fetch_yahoo_headlines(symbol, limit=3)

    return {
        "symbol": symbol,
        "name": q.get("shortName") or symbol,
        "price": q.get("regularMarketPrice"),
        "pct_change": q.get("regularMarketChangePercent"),
        "volume": q.get("regularMarketVolume"),
        "market_cap": q.get("marketCap"),
        "prev_close": q.get("regularMarketPreviousClose"),
        "trailing_pe": q.get("trailingPE"),
        "next_earnings": next_earnings,
        "headlines": headlines,
        "source": "yahoo_query1",
    }


def fetch_yahoo_headlines(symbol: str, limit: int = 3) -> List[str]:
    """Best-effort news titles; returns [] on failure (non-fatal)."""
    params = urllib.parse.urlencode(
        {"q": symbol, "lang": "en-US", "region": "US", "count": str(limit)}
    )
    # chart news endpoint varies; use search news if available
    url = (
        "https://query1.finance.yahoo.com/v1/finance/search?"
        + params
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "analyst-ledger/0.1", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    news = payload.get("news") or []
    titles = []
    for item in news[:limit]:
        t = item.get("title")
        if t:
            titles.append(str(t))
    return titles


def _stub_quote(symbol: str) -> Dict[str, Any]:
    # Deterministic-ish stub from symbol hash
    seed = sum(ord(c) for c in symbol)
    return {
        "symbol": symbol,
        "name": f"{symbol} Inc",
        "price": round(100 + (seed % 50) + (seed % 10) / 10, 2),
        "pct_change": round(((seed % 21) - 10) / 10, 2),
        "volume": 1_000_000 + seed * 1000,
        "market_cap": 1_000_000_000 + seed * 1_000_000,
        "prev_close": 100.0,
        "trailing_pe": 20.0,
        "next_earnings": "2026-08-15",
        "headlines": [
            f"{symbol} stub headline A",
            f"{symbol} stub headline B",
        ],
        "source": "stub",
    }


def render_morning_note(rows: List[Dict[str, Any]]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"## Morning scan {today}",
        "",
        "_Generated by `morning_yf_scan`. Verify figures; not investment advice._",
        "",
    ]
    for r in rows:
        sym = r.get("symbol", "?")
        if r.get("error"):
            lines.append(f"- **{sym}**: error — {r['error']}")
            continue
        pct = r.get("pct_change")
        pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else str(pct)
        price = r.get("price")
        earn = r.get("next_earnings") or "n/a"
        lines.append(
            f"- **{sym}** ({r.get('name', '')}): {price} ({pct_s}) | "
            f"vol {r.get('volume')} | earnings {earn}"
        )
        for h in r.get("headlines") or []:
            lines.append(f"  - {h}")
    lines.append("")
    lines.append("## Checks")
    lines.append("- [ ] Any filing / guidance change vs yesterday?")
    lines.append("- [ ] Relative move vs SPY / sector?")
    lines.append("- [ ] Promote any name to a deeper session?")
    lines.append("")
    return "\n".join(lines)
