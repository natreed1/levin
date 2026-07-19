"""Claude reviewer pipeline: gather ledger evidence → review → draft proposals.

The reviewer only *proposes*: draft specs are written with approved=false and
the human approves in the dashboard. Falls back to a deterministic local stub
when no API key is configured so the whole flow works offline.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import Ledger
from .paths import data_dir, ritual_specs_dir
from .redact import redact_text
from .rituals import _validate_ritual_id, last_runs, list_automations
from .runners import RUNNERS
from .schema import (
    Event,
    Sensitivity,
    Surface,
    parse_sensitivity,
    sensitivity_allows_egress,
    utc_now_iso,
)

_MEMO_NAME_RE = re.compile(r"^[\w.-]+\.md$")

_DEFAULT_STEPS: Dict[str, List[Dict[str, Any]]] = {
    "morning_yf_scan": [
        {"fetch_quote": ["price", "pct_change", "volume", "market_cap"]},
        {"fetch_headlines": {"limit": 3}},
        {"draft_note": "morning_scan_template"},
    ],
    "generic_watchlist_scan": [
        {"fetch_quote": ["price", "pct_change", "volume"]},
        {"draft_note": "watchlist_template"},
    ],
    "sec_filings_check": [
        {"fetch_filings": {"days": 3}},
        {"draft_note": "filings_template"},
    ],
    "note_digest": [
        {"collect_notes": {"days": 7}},
        {"draft_note": "digest_template"},
    ],
}


def reviews_dir() -> Path:
    path = data_dir() / "reviews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _egress_ok(ev: Dict[str, Any]) -> bool:
    try:
        level = parse_sensitivity(ev.get("sensitivity"))
    except ValueError:
        return False
    return sensitivity_allows_egress(level, Sensitivity.INTERNAL)


def gather_review_context(
    ledger: Ledger, days: int = 14, max_sessions: int = 15
) -> Dict[str, Any]:
    """Redacted, internal-and-below evidence pack for the reviewer."""
    from .session_insights import summarize_session_events

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    sessions_out: List[Dict[str, Any]] = []
    ritual_session_count = 0
    for sess in ledger.list_sessions(limit=150):
        if str(sess.get("started_at") or "") < cutoff:
            continue
        if sess.get("surface") == Surface.RITUAL.value:
            ritual_session_count += 1
            continue
        if len(sessions_out) >= max_sessions:
            continue
        events = [
            e
            for e in ledger.list_events(session_id=sess["session_id"], limit=300)
            if _egress_ok(e)
        ]
        insight = summarize_session_events(events)
        sessions_out.append(
            {
                "session_id": sess["session_id"],
                "title": redact_text(str(sess.get("title") or ""))[:120],
                "started_at": sess.get("started_at"),
                "tags": sess.get("tags") or [],
                "surface": sess.get("surface"),
                "summary": insight.get("summary_line"),
                "symbols": (insight.get("symbols") or [])[:8],
                "sections": (insight.get("sections") or [])[:6],
                "notes": [redact_text(n)[:200] for n in (insight.get("notes") or [])[:5]],
            }
        )

    feedback_counts: Dict[str, int] = {}
    for ev in ledger.list_events(limit=200, types=["feedback"]):
        label = str((ev.get("payload") or {}).get("label") or "")
        if label:
            feedback_counts[label] = feedback_counts.get(label, 0) + 1

    return {
        "window_days": days,
        "sessions": sessions_out,
        "ritual_session_count": ritual_session_count,
        "automations": [
            {
                "ritual_id": a.get("ritual_id"),
                "confidence": a.get("confidence"),
                "has_spec": a.get("has_spec"),
                "approved": a.get("approved"),
                "runner": a.get("runner"),
                "watchlist": (a.get("watchlist") or [])[:10],
                "host_family": a.get("host_family"),
                "last_run": a.get("last_run"),
            }
            for a in list_automations(ledger)
        ],
        "feedback_counts": feedback_counts,
        "available_runners": sorted(RUNNERS),
    }


def build_review_prompt(context: Dict[str, Any]) -> str:
    return (
        "You are reviewing a buy-side analyst's local workflow ledger to improve their\n"
        "automations. Do not invent market data. Do not recommend trades. Work only\n"
        "from the evidence below (already redacted; sensitive material excluded).\n\n"
        "Tasks:\n"
        "1. Judge each existing automation: keep, fix (say what), or retire, using\n"
        "   last_run outcomes and whether its pattern still appears in sessions.\n"
        "2. Find repeated manual patterns worth automating that the heuristic miner\n"
        "   missed (cross-surface intent, follow-ups never done, repeated checks).\n"
        f"3. Propose new automations using ONLY these runners: {', '.join(context['available_runners'])}.\n\n"
        "Respond with STRICT JSON only (no prose outside it):\n"
        "{\n"
        '  "memo_markdown": "the review memo a human will read",\n'
        '  "verdicts": [{"ritual_id": "...", "verdict": "keep|fix|retire", "reason": "..."}],\n'
        '  "proposals": [{"ritual_id": "lowercase_snake_case", "runner": "...",\n'
        '                 "schedule": "cron like 0 7 * * 1-5", "watchlist": ["TICK"],\n'
        '                 "rationale": "why, citing the evidence"}]\n'
        "}\n\n"
        "Evidence:\n" + json.dumps(context, ensure_ascii=False, indent=1)[:14000]
    )


def _stub_review(context: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic offline reviewer so the UI flow works without an API key."""
    verdicts: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []
    for a in context.get("automations") or []:
        rid = a.get("ritual_id")
        if not rid:
            continue
        run = a.get("last_run")
        if a.get("has_spec") and run and run.get("error_count"):
            verdicts.append(
                {"ritual_id": rid, "verdict": "fix",
                 "reason": f"last run had {run['error_count']} error(s)"}
            )
        elif a.get("approved") and not run:
            verdicts.append(
                {"ritual_id": rid, "verdict": "fix",
                 "reason": "approved but never run — schedule it or retire it"}
            )
        elif a.get("has_spec"):
            verdicts.append({"ritual_id": rid, "verdict": "keep", "reason": "runs cleanly"})
        if not a.get("has_spec"):
            family = a.get("host_family")
            runner = {
                "yahoo": "morning_yf_scan",
                "sec": "sec_filings_check",
                "notes": "note_digest",
            }.get(str(family), "generic_watchlist_scan")
            proposals.append(
                {
                    "ritual_id": rid,
                    "runner": runner,
                    "schedule": "0 7 * * 1-5",
                    "watchlist": a.get("watchlist") or [],
                    "rationale": f"mined candidate (confidence {a.get('confidence')}) has no spec yet",
                }
            )
    note_count = sum(len(s.get("notes") or []) for s in context.get("sessions") or [])
    has_digest = any(
        (a.get("runner") == "note_digest") for a in context.get("automations") or []
    )
    if note_count >= 3 and not has_digest:
        proposals.append(
            {
                "ritual_id": "weekly_note_digest",
                "runner": "note_digest",
                "schedule": "0 16 * * 5",
                "watchlist": [],
                "rationale": f"{note_count} hand-written notes in window and no digest automation",
            }
        )

    lines = [
        "# Ledger review (local stub)",
        "",
        "_Generated without a model call — set ANTHROPIC_API_KEY for a Claude-written review._",
        "",
        f"Window: last {context.get('window_days')} day(s) · "
        f"{len(context.get('sessions') or [])} research session(s) · "
        f"{context.get('ritual_session_count')} automation run session(s)",
        "",
        "## Existing automations",
    ]
    lines += [f"- **{v['ritual_id']}**: {v['verdict']} — {v['reason']}" for v in verdicts] or ["- (none yet)"]
    lines += ["", "## Proposals"]
    lines += [f"- **{p['ritual_id']}** ({p['runner']}): {p['rationale']}" for p in proposals] or ["- (none)"]
    return {"memo_markdown": "\n".join(lines), "verdicts": verdicts, "proposals": proposals}


def _parse_model_json(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Unparseable → keep the prose as the memo, no proposals
    return {"memo_markdown": text, "verdicts": [], "proposals": []}


def _write_proposals(proposals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Persist drafts (approved=false). Never touch approved or human-made specs."""
    written: List[Dict[str, Any]] = []
    for p in proposals:
        try:
            rid = _validate_ritual_id(str(p.get("ritual_id") or ""))
        except ValueError:
            continue
        runner = str(p.get("runner") or "")
        if runner not in RUNNERS:
            continue
        path = ritual_specs_dir() / f"{rid}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
            if existing.get("approved") or existing.get("proposed_by") != "claude_review":
                continue
        watchlist = [
            str(s).strip().upper() for s in (p.get("watchlist") or []) if str(s).strip()
        ][:15]
        spec = {
            "name": rid,
            "version": 1,
            "approved": False,
            "runner": runner,
            "schedule": str(p.get("schedule") or "0 7 * * 1-5"),
            "watchlist": watchlist,
            "steps": _DEFAULT_STEPS.get(runner, [{"draft_note": "template"}]),
            "outputs": {"ledger_session": True},
            "proposed_by": "claude_review",
            "rationale": redact_text(str(p.get("rationale") or ""))[:500],
            "created_at": utc_now_iso(),
        }
        path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        written.append({"ritual_id": rid, "runner": runner, "spec_path": str(path)})
    return written


def run_review(
    ledger: Optional[Ledger] = None,
    days: int = 14,
    destination: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    ledger = ledger or Ledger()
    if destination is None:
        destination = (
            "anthropic" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "local_stub"
        )

    context = gather_review_context(ledger, days=days)
    prompt = build_review_prompt(context)

    if dry_run:
        ledger.record_egress(
            destination=destination,
            prompt=prompt,
            max_sensitivity=Sensitivity.INTERNAL.value,
            status="dry_run",
            detail={"kind": "review"},
        )
        return {"status": "dry_run", "prompt_preview": prompt[:1500], "destination": destination}

    if destination == "local_stub":
        review = _stub_review(context)
    else:
        from .synthesize import _call_anthropic, _call_bedrock

        raw = _call_bedrock(prompt) if destination == "bedrock" else _call_anthropic(
            prompt, max_tokens=4096
        )
        review = _parse_model_json(raw)

    ledger.record_egress(
        destination=destination,
        prompt=prompt,
        max_sensitivity=Sensitivity.INTERNAL.value,
        status="ok",
        detail={"kind": "review", "days": days},
    )

    written = _write_proposals(review.get("proposals") or [])

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    memo_path = reviews_dir() / f"{stamp}_review.md"
    footer = ["", "---", ""]
    if written:
        footer.append("**Draft specs written (unapproved — approve in the dashboard):**")
        footer += [f"- `{w['ritual_id']}` → {w['spec_path']}" for w in written]
    else:
        footer.append("_No new draft specs written._")
    memo_path.write_text(
        str(review.get("memo_markdown") or "(empty memo)") + "\n".join(footer) + "\n",
        encoding="utf-8",
    )

    ledger.append_event(
        Event(
            type="review_run",
            surface=Surface.SYNTHESIS.value,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={
                "destination": destination,
                "days": days,
                "memo_path": str(memo_path),
                "proposal_count": len(written),
                "verdict_count": len(review.get("verdicts") or []),
            },
        )
    )
    return {
        "status": "ok",
        "destination": destination,
        "memo_path": str(memo_path),
        "memo": str(review.get("memo_markdown") or ""),
        "verdicts": review.get("verdicts") or [],
        "proposals_written": written,
    }


def list_reviews() -> List[Dict[str, str]]:
    out = []
    for path in sorted(reviews_dir().glob("*_review.md"), reverse=True):
        out.append({"name": path.name, "path": str(path)})
    return out


def read_review(name: str) -> Optional[str]:
    if not _MEMO_NAME_RE.match(name or ""):
        return None
    path = reviews_dir() / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
