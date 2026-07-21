"""Ritual runner registry + deterministic runners beyond the morning YF scan.

Every runner follows the same contract as ``run_morning_yf_scan``:
it accepts (ledger, watchlist, ritual_id, stub, require_approved, ...) kwargs,
writes its results back into the ledger as a session + note + ritual_run event,
and returns a dict with at least {"status", "session_id", "note"}.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ledger import Ledger
from .morning_yf import run_morning_yf_scan
from .paths import artifacts_dir, data_dir, ritual_specs_dir
from .redact import redact_text
from .schema import (
    Event,
    Sensitivity,
    Surface,
    parse_sensitivity,
    sensitivity_allows_egress,
)


def _spec_for(ritual_id: str, require_approved: bool) -> Dict[str, Any]:
    """Load the ritual spec if present; enforce approval when requested."""
    spec_path = ritual_specs_dir() / f"{ritual_id}.json"
    if not spec_path.exists():
        if require_approved:
            raise RuntimeError(
                f"No approved spec for '{ritual_id}'. "
                f"Run: analyst rituals suggest {ritual_id} && analyst rituals approve {ritual_id}"
            )
        return {}
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if require_approved and not spec.get("approved"):
        raise RuntimeError(
            f"Spec '{ritual_id}' is not approved. Run: analyst rituals approve {ritual_id}"
        )
    return spec


def _record_run(
    ledger: Ledger,
    *,
    ritual_id: str,
    runner: str,
    title: str,
    note: str,
    payload_extra: Dict[str, Any],
    artifact_name: str,
    sensitivity: str = Sensitivity.INTERNAL.value,
) -> Dict[str, Any]:
    """Shared bookkeeping: session + note + artifact + ritual_run event."""
    session = ledger.start_session(
        title=title,
        surface=Surface.RITUAL.value,
        sensitivity=sensitivity,
        desk_tag="routine",
    )
    ledger.add_note(
        note, session_id=session.session_id, sensitivity=sensitivity, surface=Surface.RITUAL.value
    )

    out_dir = artifacts_dir() / session.session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    note_path = out_dir / artifact_name
    note_path.write_text(note, encoding="utf-8")
    ledger.attach_artifact(note_path, session_id=session.session_id, sensitivity=sensitivity)

    ledger.append_event(
        Event(
            type="ritual_run",
            surface=Surface.RITUAL.value,
            session_id=session.session_id,
            sensitivity=sensitivity,
            payload={"ritual_id": ritual_id, "runner": runner, **payload_extra},
        )
    )
    ledger.end_session(session_id=session.session_id, tags=["neutral"])
    return {
        "status": "ok",
        "session_id": session.session_id,
        "ritual_id": ritual_id,
        "note_path": str(note_path),
        "note": note,
    }


# --- SEC filings check ------------------------------------------------------

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_TICKER_CACHE_MAX_AGE = 7 * 86400  # refresh weekly


def _sec_user_agent() -> str:
    # SEC asks automated clients to identify themselves with a contact address.
    contact = os.environ.get("ANALYST_SEC_CONTACT", "").strip()
    return f"analyst-ledger/0.1 ({contact or 'personal research tool'})"


def _sec_get_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _sec_user_agent(), "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _ticker_to_cik_map() -> Dict[str, int]:
    """Ticker → CIK from SEC's public mapping, cached on disk for a week."""
    cache_dir = data_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "sec_company_tickers.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < _TICKER_CACHE_MAX_AGE:
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        raw = _sec_get_json(SEC_TICKER_MAP_URL)
        cache.write_text(json.dumps(raw), encoding="utf-8")
    out: Dict[str, int] = {}
    for row in raw.values():
        ticker = str(row.get("ticker") or "").upper()
        if ticker:
            out[ticker] = int(row["cik_str"])
    return out


def _recent_filings(cik: int, days: int, limit: int = 5) -> List[Dict[str, Any]]:
    data = _sec_get_json(SEC_SUBMISSIONS_URL.format(cik=cik))
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: List[Dict[str, Any]] = []
    for form, date, acc, doc in zip(forms, dates, accessions, docs):
        if date < cutoff:
            continue
        acc_nodash = str(acc).replace("-", "")
        out.append(
            {
                "form": form,
                "date": date,
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}",
            }
        )
        if len(out) >= limit:
            break
    return out


def _stub_filings(symbol: str) -> List[Dict[str, Any]]:
    today = datetime.now().strftime("%Y-%m-%d")
    return [
        {"form": "8-K", "date": today, "url": f"https://example.invalid/{symbol}/8-K"},
    ]


def render_filings_note(
    rows: List[Dict[str, Any]], days: int
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"## SEC filings check {today} (last {days} day(s))",
        "",
        "_Generated by `sec_filings_check` from EDGAR public data. Verify before use._",
        "",
    ]
    for r in rows:
        sym = r.get("symbol", "?")
        if r.get("error"):
            lines.append(f"- **{sym}**: error — {r['error']}")
            continue
        filings = r.get("filings") or []
        if not filings:
            lines.append(f"- **{sym}**: no new filings")
            continue
        lines.append(f"- **{sym}**: {len(filings)} new filing(s)")
        for f in filings:
            lines.append(f"  - {f['form']} ({f['date']}) — {f['url']}")
    lines += [
        "",
        "## Checks",
        "- [ ] Any 8-K worth a deeper read?",
        "- [ ] Guidance / risk-factor changes vs the last 10-Q?",
        "",
    ]
    return "\n".join(lines)


def run_sec_filings_check(
    ledger: Optional[Ledger] = None,
    watchlist: Optional[List[str]] = None,
    ritual_id: str = "sec_filings_check",
    stub: bool = False,
    require_approved: bool = False,
    days: int = 3,
    **_: Any,
) -> Dict[str, Any]:
    """List recent EDGAR filings for the watchlist and draft a check note."""
    ledger = ledger or Ledger()
    spec = _spec_for(ritual_id, require_approved)
    symbols = [s.upper() for s in (watchlist or spec.get("watchlist") or []) if s]
    if not symbols:
        symbols = ["NVDA", "AAPL", "SPY"]

    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    cik_map: Dict[str, int] = {}
    if not stub:
        try:
            cik_map = _ticker_to_cik_map()
        except Exception as exc:  # noqa: BLE001 — degrade per-symbol below
            errors.append(f"ticker map: {exc}")

    for sym in symbols:
        try:
            if stub:
                rows.append({"symbol": sym, "filings": _stub_filings(sym)})
                continue
            cik = cik_map.get(sym)
            if cik is None:
                rows.append({"symbol": sym, "error": "no CIK (ETF or unknown ticker)"})
                continue
            rows.append({"symbol": sym, "filings": _recent_filings(cik, days=days)})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")
            rows.append({"symbol": sym, "error": str(exc)})

    note = render_filings_note(rows, days=days)
    result = _record_run(
        ledger,
        ritual_id=ritual_id,
        runner="sec_filings_check",
        title=f"SEC filings check {datetime.now().strftime('%Y-%m-%d')}",
        note=note,
        payload_extra={"symbols": symbols, "stub": stub, "errors": errors, "rows": rows},
        artifact_name="sec_filings_check.md",
    )
    result["rows"] = rows
    result["errors"] = errors
    return result


# --- Note digest -------------------------------------------------------------


def run_note_digest(
    ledger: Optional[Ledger] = None,
    watchlist: Optional[List[str]] = None,
    ritual_id: str = "note_digest",
    stub: bool = False,
    require_approved: bool = False,
    days: int = 7,
    destination: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    """
    Digest the last N days of hand-written notes into one review note.

    Notes at `internal` sensitivity or below are included; with
    destination="qwen" (local model — content never leaves the machine)
    `confidential` notes are included too. `restricted` never leaves its
    session. Text is redacted, and notes written by rituals themselves are
    skipped so the digest never digests itself.
    Pass destination="anthropic"/"bedrock"/"qwen" to append a model summary.
    """
    ledger = ledger or Ledger()
    _spec_for(ritual_id, require_approved)

    include_ceiling = (
        Sensitivity.CONFIDENTIAL if destination == "qwen" else Sensitivity.INTERNAL
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    events = ledger.list_events(limit=1000, types=["note"])
    picked: List[Dict[str, Any]] = []
    skipped_sensitive = 0
    has_confidential = False
    for ev in events:
        if str(ev.get("ts") or "") < cutoff:
            continue
        if ev.get("surface") == Surface.RITUAL.value:
            continue
        level = parse_sensitivity(ev.get("sensitivity"))
        if not sensitivity_allows_egress(level, include_ceiling):
            skipped_sensitive += 1
            continue
        text = str((ev.get("payload") or {}).get("text") or "").strip()
        if text:
            picked.append({"ts": ev.get("ts"), "text": redact_text(text)[:300]})
            if level == Sensitivity.CONFIDENTIAL:
                has_confidential = True

    picked.reverse()  # chronological

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"## Note digest {today} (last {days} day(s))",
        "",
        f"_{len(picked)} note(s) included; {skipped_sensitive} skipped above the "
        f"'{include_ceiling.value}' ceiling._",
        "",
    ]
    if picked:
        lines += [f"- [{str(n['ts'])[:10]}] {n['text']}" for n in picked]
    else:
        lines.append("- (no notes in window)")

    digest_sensitivity = (
        Sensitivity.CONFIDENTIAL if has_confidential else Sensitivity.INTERNAL
    )
    narrative_error: Optional[str] = None
    if destination in {"anthropic", "bedrock", "qwen"} and picked:
        from .synthesize import (
            _call_anthropic,
            _call_bedrock,
            _call_openai_compatible_messages,
            assert_destination_allowed,
        )

        assert_destination_allowed(destination, digest_sensitivity)
        prompt = (
            "Summarize this analyst's own research notes from the past week into "
            "3-6 bullets of themes and open questions. Do not invent market facts. "
            "Do not recommend trades.\n\n" + "\n".join(f"- {n['text']}" for n in picked)
        )
        try:
            if destination == "qwen":
                narrative = _call_openai_compatible_messages(
                    [{"role": "user", "content": prompt}]
                ).strip()
            elif destination == "bedrock":
                narrative = _call_bedrock(prompt)
            else:
                narrative = _call_anthropic(prompt)
            ledger.record_egress(
                destination=destination,
                prompt=prompt,
                max_sensitivity=digest_sensitivity.value,
                status="ok",
                detail={"ritual_id": ritual_id, "kind": "note_digest"},
            )
            lines += ["", "## Themes (model summary)", "", narrative]
        except Exception as exc:  # noqa: BLE001 — digest still useful without it
            narrative_error = str(exc)
            ledger.record_egress(
                destination=destination,
                prompt=prompt,
                max_sensitivity=digest_sensitivity.value,
                status="error",
                detail={"ritual_id": ritual_id, "error": narrative_error},
            )
            if destination == "qwen":
                lines += [
                    "",
                    "_(Local model offline — digest above is complete; "
                    "start Ollama to add a themes summary.)_",
                ]

    lines.append("")
    note = "\n".join(lines)
    result = _record_run(
        ledger,
        ritual_id=ritual_id,
        runner="note_digest",
        title=f"Note digest {today}",
        note=note,
        payload_extra={
            "stub": stub,
            "errors": [narrative_error] if narrative_error else [],
            "note_count": len(picked),
            "skipped_sensitive": skipped_sensitive,
            "destination": destination,
        },
        artifact_name="note_digest.md",
        sensitivity=digest_sensitivity.value,
    )
    result["note_count"] = len(picked)
    return result


# --- Registry ----------------------------------------------------------------

RUNNERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "morning_yf_scan": run_morning_yf_scan,
    "generic_watchlist_scan": run_morning_yf_scan,
    "sec_filings_check": run_sec_filings_check,
    "note_digest": run_note_digest,
}


def resolve_runner(
    ritual_id: str, explicit: Optional[str] = None
) -> Tuple[str, Callable[..., Dict[str, Any]]]:
    """
    Pick a runner: explicit --runner flag > spec's runner field > name heuristic.
    """
    if explicit:
        if explicit not in RUNNERS:
            raise RuntimeError(
                f"Unknown runner '{explicit}'. Available: {', '.join(sorted(RUNNERS))}"
            )
        return explicit, RUNNERS[explicit]

    spec_path = ritual_specs_dir() / f"{ritual_id}.json"
    if spec_path.exists():
        try:
            spec_runner = json.loads(spec_path.read_text(encoding="utf-8")).get("runner")
        except (json.JSONDecodeError, OSError):
            spec_runner = None
        if spec_runner in RUNNERS:
            return str(spec_runner), RUNNERS[str(spec_runner)]

    rid = ritual_id.lower()
    if "yahoo" in rid or rid == "morning_yf_scan":
        return "morning_yf_scan", run_morning_yf_scan
    if "sec" in rid or "filing" in rid:
        return "sec_filings_check", run_sec_filings_check
    if "digest" in rid or "note" in rid:
        return "note_digest", run_note_digest
    raise RuntimeError(
        f"No runner for ritual '{ritual_id}'. "
        f"Available runners: {', '.join(sorted(RUNNERS))} (use --runner to force one)."
    )
