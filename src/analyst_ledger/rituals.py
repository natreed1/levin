"""Mine recurring research rituals and suggest / persist workflow specs."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .browser import summarize_url_events
from .ledger import Ledger
from .paths import (
    claude_skills_dir,
    data_dir,
    ritual_builds_dir,
    ritual_specs_dir,
    rituals_dir,
)
from .redact import redact_text
from .schema import (
    Event,
    Sensitivity,
    Surface,
    parse_sensitivity,
    sensitivity_allows_egress,
    utc_now_iso,
)


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Support both Z and +00:00
        cleaned = ts.replace("Z", "+00:00")
        if "." in cleaned:
            # trim overlong fractional seconds
            head, rest = cleaned.split(".", 1)
            frac = re.match(r"(\d+)", rest)
            tz = rest[len(frac.group(1)) :] if frac else ""
            cleaned = f"{head}.{frac.group(1)[:6]}{tz}" if frac else cleaned
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _local_bucket(dt: datetime) -> Tuple[int, int]:
    """Return (weekday 0=Mon, local hour) using system local tz."""
    local = dt.astimezone()
    return local.weekday(), local.hour


def mine_rituals(
    ledger: Optional[Ledger] = None,
    days: int = 21,
    min_sessions: int = 3,
    morning_hours: Tuple[int, int] = (5, 11),
) -> List[Dict[str, Any]]:
    """
    Cluster sessions by local weekday+hour and research hosts/symbols.

    Returns candidate ritual dicts sorted by confidence.
    """
    ledger = ledger or Ledger()
    sessions = ledger.list_sessions(limit=500)
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400

    # bucket_key -> list of session evidence
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for sess in sessions:
        started = _parse_ts(sess.get("started_at") or "")
        if not started:
            continue
        if started.timestamp() < cutoff:
            continue
        weekday, hour = _local_bucket(started)
        # Morning band collapses to one ritual slot; other hours stay hour-specific
        if morning_hours[0] <= hour < morning_hours[1]:
            slot = "morning"
            hour_label = f"{morning_hours[0]:02d}-{morning_hours[1]:02d}"
        else:
            slot = f"h{hour:02d}"
            hour_label = f"{hour:02d}:00"

        events = ledger.list_events(session_id=sess["session_id"], limit=500)
        events_chrono = list(reversed(events))
        url_summary = summarize_url_events(events_chrono)
        symbols_from_tv = _symbols_from_events(events_chrono)
        notes = [
            (e.get("payload") or {}).get("text", "")
            for e in events_chrono
            if e.get("type") == "note"
        ]
        notes = [n for n in notes if n]

        hosts = url_summary.get("hosts") or {}
        # Also count tradingview surface events as a host signal
        if any(e.get("surface") == "tradingview" for e in events_chrono):
            hosts = dict(hosts)
            hosts["tradingview.com"] = hosts.get("tradingview.com", 0) + sum(
                1 for e in events_chrono if e.get("surface") == "tradingview"
            )

        primary_host = next(iter(hosts), None) if hosts else None
        if not primary_host and not symbols_from_tv and not notes:
            continue

        all_symbols = dict(symbols_from_tv)
        for s, c in (url_summary.get("symbols") or {}).items():
            all_symbols[s] = all_symbols.get(s, 0) + c

        # Bucket by slot + dominant host family
        host_family = _host_family(primary_host) if primary_host else "notes"
        key = f"{slot}|{host_family}"
        buckets[key].append(
            {
                "session_id": sess["session_id"],
                "title": sess.get("title"),
                "started_at": sess.get("started_at"),
                "weekday": weekday,
                "hour": hour,
                "hour_label": hour_label,
                "hosts": hosts,
                "symbols": all_symbols,
                "sections": url_summary.get("sections") or {},
                "notes": notes[:10],
                "sample_paths": url_summary.get("sample_paths") or [],
                "tags": list(sess.get("tags") or []),
            }
        )

    candidates: List[Dict[str, Any]] = []
    for key, evidence in buckets.items():
        if len(evidence) < min_sessions:
            continue
        slot, host_family = key.split("|", 1)
        merged_hosts: Dict[str, int] = defaultdict(int)
        merged_symbols: Dict[str, int] = defaultdict(int)
        merged_sections: Dict[str, int] = defaultdict(int)
        symbol_sessions: Dict[str, int] = defaultdict(int)
        note_hints: List[str] = []
        weekdays = set()
        outcome_counts: Dict[str, int] = defaultdict(int)
        for ev in evidence:
            weekdays.add(ev["weekday"])
            for h, c in (ev.get("hosts") or {}).items():
                merged_hosts[h] += c
            for s, c in (ev.get("symbols") or {}).items():
                merged_symbols[s] += c
                symbol_sessions[s] += 1
            for sec, c in (ev.get("sections") or {}).items():
                merged_sections[sec] += c
            note_hints.extend(ev.get("notes") or [])
            for t in ev.get("tags") or []:
                outcome_counts[str(t)] += 1

        # weekday-only rituals score higher if mostly Mon-Fri
        weekday_only = weekdays <= {0, 1, 2, 3, 4}
        n = len(evidence)
        conf = 0.35 + 0.08 * min(n, 8)
        reasons = [f"{n} sessions in bucket"]
        if weekday_only:
            conf += 0.12
            reasons.append("weekday-only")
        if host_family == "yahoo":
            conf += 0.1
            reasons.append("yahoo host")
        if slot == "morning" and n >= 3:
            conf += 0.08
            reasons.append("≥3 morning sessions")

        # Symbols that recur across sessions → real watchlist signal
        recur = [
            s
            for s, sc in symbol_sessions.items()
            if sc >= max(2, (n + 1) // 2)
        ]
        if recur:
            conf += min(0.12, 0.04 * len(recur))
            reasons.append(f"recurring symbols: {', '.join(recur[:5])}")

        research_sections = {
            s for s in merged_sections if s not in {"home", "quote", "video"}
        }
        if research_sections:
            conf += min(0.1, 0.03 * len(research_sections))
            reasons.append("depth: " + ", ".join(sorted(research_sections)[:5]))

        useful_outcomes = outcome_counts.get("idea", 0) + outcome_counts.get(
            "followup", 0
        )
        if useful_outcomes:
            conf += min(0.1, 0.04 * useful_outcomes)
            reasons.append(f"{useful_outcomes} idea/followup outcome(s)")

        conf = round(min(0.95, conf), 3)

        ritual_id = f"{slot}_{host_family}_scan"
        # Prefer symbols seen in multiple sessions, then by hit count
        top_symbols = [
            s
            for s, _ in sorted(
                merged_symbols.items(),
                key=lambda x: (-symbol_sessions.get(x[0], 0), -x[1], x[0]),
            )[:15]
        ]
        top_sections = [
            s
            for s, _ in sorted(merged_sections.items(), key=lambda x: -x[1])
            if s != "home"
        ][:8]
        candidate = {
            "ritual_id": ritual_id,
            "confidence": conf,
            "confidence_reasons": reasons,
            "slot": slot,
            "host_family": host_family,
            "when": {
                "slot": slot,
                "hour_label": evidence[0].get("hour_label"),
                "weekdays": sorted(weekdays),
                "weekday_only": weekday_only,
            },
            "sequence": _build_sequence(host_family, top_symbols, top_sections),
            "watchlist": top_symbols,
            "recurring_symbols": recur,
            "sections": top_sections,
            "hosts": dict(sorted(merged_hosts.items(), key=lambda x: -x[1])),
            "note_hints": _hint_keywords(note_hints),
            "outcomes": dict(outcome_counts),
            "evidence_sessions": [e["session_id"] for e in evidence],
            "evidence_count": len(evidence),
            "mined_at": utc_now_iso(),
        }
        candidates.append(candidate)

        ledger.append_event(
            Event(
                type="ritual_candidate",
                surface=Surface.RITUAL.value,
                session_id=None,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "ritual_id": ritual_id,
                    "confidence": candidate["confidence"],
                    "evidence_count": len(evidence),
                },
            )
        )

    candidates.sort(key=lambda c: (-c["confidence"], -c["evidence_count"]))
    out_path = rituals_dir() / "candidates.json"
    out_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    return candidates


def _symbols_from_events(events: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for e in events:
        if e.get("type") not in {"symbol_focus", "interval_change", "drawing_meta"}:
            continue
        sym = (e.get("payload") or {}).get("symbol")
        if sym:
            out[str(sym).upper()] += 1
    return dict(out)


def _host_family(host: Optional[str]) -> str:
    if not host:
        return "notes"
    h = host.lower()
    if "yahoo" in h:
        return "yahoo"
    if "tradingview" in h:
        return "tradingview"
    if "sec.gov" in h:
        return "sec"
    if "bloomberg" in h:
        return "bloomberg"
    if "seekingalpha" in h:
        return "seekingalpha"
    return h.split(".")[-2] if "." in h else h


def _build_sequence(
    host_family: str, symbols: List[str], sections: List[str]
) -> List[Dict[str, Any]]:
    seq: List[Dict[str, Any]] = []
    if host_family == "yahoo":
        seq.append(
            {
                "type": "url_focus",
                "host": "finance.yahoo.com",
                "path_pattern": "/quote/*",
            }
        )
        # Prefer research depth tabs when observed; else bare quote
        depth = [s for s in sections if s not in {"home", "video"}]
        if depth:
            seq.append(
                {
                    "type": "sections",
                    "values": depth,
                    "path_patterns": [
                        ("/quote/*" if sec == "quote" else f"/quote/*/{sec}")
                        for sec in depth
                    ],
                }
            )
        else:
            seq.append({"type": "sections", "values": ["quote"]})
    elif host_family == "tradingview":
        seq.append({"type": "surface", "value": "tradingview"})
    else:
        seq.append({"type": "host_family", "value": host_family})
    if symbols:
        seq.append({"type": "symbol_set", "symbols": symbols})
    # Notes remain optional — hint only when mining saw them
    seq.append({"type": "note", "optional": True, "template_hints": ["% change", "earnings", "news"]})
    return seq


def _hint_keywords(notes: List[str]) -> List[str]:
    bag = " ".join(notes).lower()
    hints = []
    for kw in (
        "%",
        "percent",
        "change",
        "earnings",
        "eps",
        "news",
        "filing",
        "volume",
        "rsi",
        "peer",
        "guidance",
    ):
        if kw in bag:
            hints.append(kw)
    return hints[:12]


def load_candidates() -> List[Dict[str, Any]]:
    path = rituals_dir() / "candidates.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_candidates(candidates: List[Dict[str, Any]]) -> Path:
    path = rituals_dir() / "candidates.json"
    path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    return path


def get_candidate(ritual_id: str) -> Dict[str, Any]:
    for c in load_candidates():
        if c.get("ritual_id") == ritual_id:
            return c
    raise RuntimeError(f"Ritual '{ritual_id}' not found. Run: analyst rituals mine")


def save_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    rid = _validate_ritual_id(str(candidate.get("ritual_id") or ""))
    candidates = load_candidates()
    found = False
    for i, c in enumerate(candidates):
        if c.get("ritual_id") == rid:
            candidates[i] = candidate
            found = True
            break
    if not found:
        candidates.append(candidate)
    save_candidates(candidates)
    return candidate


def active_evidence_sessions(candidate: Optional[Dict[str, Any]]) -> List[str]:
    if not candidate:
        return []
    excluded = set(candidate.get("excluded_sessions") or [])
    return [s for s in (candidate.get("evidence_sessions") or []) if s not in excluded]


def update_automation(
    ritual_id: str,
    *,
    watchlist: Optional[List[str]] = None,
    excluded_sessions: Optional[List[str]] = None,
    excluded_event_ids: Optional[List[str]] = None,
    approved: Optional[bool] = None,
    enabled: Optional[bool] = None,
    note_hints: Optional[List[str]] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Edit automation fields (watchlist, evidence toggles, approve/enable, model).

    Persists to candidates.json and/or the workflow spec when present.
    """
    rid = _validate_ritual_id(ritual_id)
    candidate: Optional[Dict[str, Any]] = None
    try:
        candidate = dict(get_candidate(rid))
    except RuntimeError:
        candidate = None

    if candidate is not None:
        if watchlist is not None:
            candidate["watchlist"] = [
                s.strip().upper() for s in watchlist if str(s).strip()
            ]
        if excluded_sessions is not None:
            candidate["excluded_sessions"] = [
                s for s in excluded_sessions if s in (candidate.get("evidence_sessions") or [])
            ]
            candidate["evidence_count"] = len(active_evidence_sessions(candidate))
        if excluded_event_ids is not None:
            candidate["excluded_event_ids"] = list(excluded_event_ids)
        if note_hints is not None:
            candidate["note_hints"] = note_hints
        if enabled is not None:
            candidate["enabled"] = bool(enabled)
        candidate["updated_at"] = utc_now_iso()
        save_candidate(candidate)

    spec: Optional[Dict[str, Any]] = None
    try:
        spec = load_spec(rid)
    except RuntimeError:
        spec = None

    if spec is not None:
        if watchlist is not None:
            spec["watchlist"] = [
                s.strip().upper() for s in watchlist if str(s).strip()
            ]
        if approved is not None:
            spec["approved"] = bool(approved)
            if approved:
                spec["approved_at"] = utc_now_iso()
        if enabled is not None:
            spec["enabled"] = bool(enabled)
        if model is not None:
            from .models import normalize_agent_model

            mid = normalize_agent_model(model)
            if model and not mid:
                raise RuntimeError(
                    f"Unknown agent model {model!r}. Choose 'claude' or 'qwen3-8b'."
                )
            spec["model"] = mid
        path = ritual_specs_dir() / f"{rid}.json"
        path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    if candidate is None and spec is None:
        raise RuntimeError(f"Ritual '{rid}' not found")

    return {
        "status": "ok",
        "ritual_id": rid,
        "candidate": candidate,
        "spec": spec,
        "active_evidence_sessions": active_evidence_sessions(candidate),
    }

def suggest_ritual(
    ritual_id: str,
    ledger: Optional[Ledger] = None,
    destination: str = "local_stub",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Review a mined ritual and produce a workflow spec (JSON).

    Uses local stub by default; anthropic/bedrock optional for richer prose.
    """
    ledger = ledger or Ledger()
    # Filter evidence notes by excluded sessions/events when suggesting
    candidate = get_candidate(ritual_id)
    excluded_sessions = set(candidate.get("excluded_sessions") or [])
    excluded_events = set(candidate.get("excluded_event_ids") or [])
    evidence_notes: List[str] = []
    for sid in active_evidence_sessions(candidate)[:8]:
        if sid in excluded_sessions:
            continue
        for e in ledger.list_events(session_id=sid, limit=100, types=["note"]):
            if e.get("event_id") in excluded_events:
                continue
            text = (e.get("payload") or {}).get("text")
            if text:
                evidence_notes.append(text)

    prompt = _suggest_prompt(candidate, evidence_notes)
    if dry_run:
        return {
            "status": "dry_run",
            "ritual_id": ritual_id,
            "prompt_preview": prompt[:1500],
        }

    if destination == "local_stub":
        spec = _stub_spec(candidate)
        narrative = _stub_narrative(candidate, evidence_notes)
    else:
        # Reuse synthesis destinations for narrative; spec still from stub structure
        from .synthesize import _call_anthropic, _call_bedrock

        narrative_prompt = prompt + "\n\nWrite the review narrative in markdown only."
        if destination == "bedrock":
            narrative = _call_bedrock(narrative_prompt)
        else:
            narrative = _call_anthropic(narrative_prompt)
        spec = _stub_spec(candidate)
        spec["review_narrative"] = narrative

    ledger.record_egress(
        destination=destination if destination != "local_stub" else "local_stub",
        prompt=prompt,
        max_sensitivity=Sensitivity.INTERNAL.value,
        status="ok",
        session_id=None,
        detail={"ritual_id": ritual_id, "kind": "ritual_suggest"},
    )

    spec_path = ritual_specs_dir() / f"{ritual_id}.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    narrative_path = ritual_specs_dir() / f"{ritual_id}_review.md"
    narrative_path.write_text(
        narrative if destination != "local_stub" else _stub_narrative(candidate, evidence_notes),
        encoding="utf-8",
    )

    ledger.append_event(
        Event(
            type="ritual_suggest",
            surface=Surface.RITUAL.value,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={
                "ritual_id": ritual_id,
                "spec_path": str(spec_path),
                "destination": destination,
            },
        )
    )
    return {
        "status": "ok",
        "ritual_id": ritual_id,
        "spec_path": str(spec_path),
        "narrative_path": str(narrative_path),
        "spec": spec,
    }


def _recent_planner_context(ledger: Ledger, limit: int = 20) -> List[Dict[str, Any]]:
    """Build an allowlisted recent-session summary for automation planning."""
    out: List[Dict[str, Any]] = []
    for session in ledger.list_sessions(limit=max(limit * 3, 40)):
        if len(out) >= limit:
            break
        if session.get("surface") in {Surface.RITUAL.value, "chat"}:
            continue
        level = parse_sensitivity(session.get("sensitivity"))
        if not sensitivity_allows_egress(level, Sensitivity.INTERNAL):
            continue
        context = ledger.session_context_for_synthesis(
            str(session["session_id"]), max_sensitivity=Sensitivity.INTERNAL
        )
        events: List[Dict[str, Any]] = []
        for ev in context.get("events") or []:
            payload = ev.get("payload") or {}
            kind = ev.get("type")
            safe: Dict[str, Any] = {}
            if kind == "note":
                safe["text"] = redact_text(str(payload.get("text") or ""))[:500]
            elif kind == "url_focus":
                for key in ("host", "path", "symbol", "section"):
                    if payload.get(key) is not None:
                        safe[key] = str(payload[key])[:200]
            elif kind in {"symbol_focus", "interval_change", "tag"}:
                for key in ("symbol", "interval", "tag"):
                    if payload.get(key) is not None:
                        safe[key] = str(payload[key])[:80]
            else:
                continue
            events.append({"type": kind, "ts": ev.get("ts"), "payload": safe})
        out.append(
            {
                "session_id": session["session_id"],
                "title": redact_text(str(session.get("title") or ""))[:200],
                "started_at": session.get("started_at"),
                "tags": list(session.get("tags") or []),
                "events": events[-40:],
            }
        )
    return out


def create_automations_with_claude(
    ledger: Optional[Ledger] = None,
    *,
    gateway: Any = None,
    recent_limit: int = 20,
) -> Dict[str, Any]:
    """Ask Claude for governed workflow drafts based on recent ledger activity."""
    from .orchestration import ClaudeGateway, validate_workflow_spec

    ledger = ledger or Ledger()
    context = _recent_planner_context(ledger, limit=recent_limit)
    if not context:
        raise RuntimeError("No eligible recent sessions found. Capture research first.")
    gateway = gateway or ClaudeGateway(ledger)
    prompt = (
        "Review the redacted analyst workflow history below and propose up to three useful "
        "repeatable automations. Return JSON only as {\"automations\":[...]}. Each automation "
        "must include name, description, runner, schedule, watchlist, steps, and budget. "
        "Allowed runners: morning_yf_scan, generic_watchlist_scan, sec_filings_check, "
        "note_digest. Each step must contain exactly one allowed action: fetch_quote, "
        "fetch_calendar, fetch_headlines, sec_filings, recent_notes. Do not invent tools, "
        "shell commands, code, trades, or market facts. Names must use letters, digits, "
        "underscore, or dash.\n\nRecent history:\n"
        + json.dumps(context, ensure_ascii=False)
    )
    payload = gateway.complete_json(
        [{"role": "user", "content": prompt}],
        kind="automation_create",
        max_tokens=4096,
        system="You design conservative, auditable buy-side research workflows.",
    )
    proposals = payload.get("automations") if isinstance(payload, dict) else None
    if not isinstance(proposals, list) or not proposals:
        raise RuntimeError("Claude did not return any automation proposals.")
    written: List[Dict[str, Any]] = []
    source_ids = [str(s["session_id"]) for s in context]
    for raw in proposals[:3]:
        spec = validate_workflow_spec(raw)
        rid = _validate_ritual_id(spec["name"])
        path = ritual_specs_dir() / f"{rid}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("approved"):
                continue
        spec.update(
            {
                "approved": False,
                "proposed_by": "claude_api",
                "created_at": utc_now_iso(),
                "source_candidate": {
                    "confidence": None,
                    "evidence_count": len(source_ids),
                    "session_ids": source_ids,
                },
            }
        )
        path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        review_path = ritual_specs_dir() / f"{rid}_review.md"
        review_path.write_text(
            f"# {rid}\n\n{spec.get('description') or 'Claude-generated workflow draft.'}\n\n"
            "**Status:** Unapproved — review the steps and approve in the dashboard before running.\n",
            encoding="utf-8",
        )
        ledger.append_event(
            Event(
                type="ritual_suggest",
                surface=Surface.RITUAL.value,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "ritual_id": rid,
                    "spec_path": str(path),
                    "destination": "anthropic",
                    "proposed_by": "claude_api",
                },
            )
        )
        written.append(spec)
    if not written:
        raise RuntimeError("Claude proposals matched existing approved automations; nothing changed.")
    return {"status": "ok", "count": len(written), "automations": written}


def _suggest_prompt(candidate: Dict[str, Any], notes: List[str]) -> str:
    note_block = "\n".join(f"- {n}" for n in notes[:20]) or "- (no notes)"
    return (
        "You are reviewing a mined research ritual from a buy-side analyst's local ledger.\n"
        "Propose a deterministic workflow an agent could run. Do not invent market data.\n"
        "Do not recommend trades. Focus on what they look at, read, and write down.\n\n"
        f"Ritual id: {candidate.get('ritual_id')}\n"
        f"Confidence: {candidate.get('confidence')}\n"
        f"When: {json.dumps(candidate.get('when'))}\n"
        f"Hosts: {json.dumps(candidate.get('hosts'))}\n"
        f"Watchlist: {candidate.get('watchlist')}\n"
        f"Sections: {candidate.get('sections')}\n"
        f"Sequence: {json.dumps(candidate.get('sequence'))}\n"
        f"Note hints: {candidate.get('note_hints')}\n\n"
        f"Sample notes from evidence sessions:\n{note_block}\n"
    )


def _stub_narrative(candidate: Dict[str, Any], notes: List[str]) -> str:
    wl = ", ".join(candidate.get("watchlist") or []) or "(empty)"
    secs = ", ".join(candidate.get("sections") or []) or "quote"
    hints = ", ".join(candidate.get("note_hints") or []) or "change, news"
    return (
        f"# Ritual review: {candidate.get('ritual_id')}\n\n"
        f"**Confidence:** {candidate.get('confidence')} "
        f"({candidate.get('evidence_count')} sessions)\n\n"
        f"## What they look at\n"
        f"- Host family: `{candidate.get('host_family')}`\n"
        f"- Watchlist (frequent): {wl}\n"
        f"- Page sections: {secs}\n\n"
        f"## What they read into notes\n"
        f"- Recurring themes: {hints}\n"
        + ("\n".join(f"- {n}" for n in notes[:8]) or "- (no notes captured yet)")
        + "\n\n## Suggested agent\n"
        f"- Schedule: weekday morning ({(candidate.get('when') or {}).get('hour_label')})\n"
        f"- Fetch structured quote + headlines for watchlist\n"
        f"- Draft a morning scan note into the ledger (+ optional Obsidian path)\n"
        f"- Human reviews via `analyst feedback`\n"
    )


def _stub_spec(candidate: Dict[str, Any]) -> Dict[str, Any]:
    ritual_id = candidate["ritual_id"]
    host_family = candidate.get("host_family")
    if host_family == "yahoo":
        runner = "morning_yf_scan"
    elif host_family == "sec":
        runner = "sec_filings_check"
    elif host_family == "notes":
        runner = "note_digest"
    else:
        runner = "generic_watchlist_scan"
    return {
        "name": ritual_id,
        "version": 1,
        "approved": False,
        "runner": runner,
        "schedule": "0 7 * * 1-5",
        "schedule_comment": "OpenClaw cron or system crontab; adjust timezone locally",
        "watchlist": candidate.get("watchlist") or [],
        "steps": [
            {"fetch_quote": ["price", "pct_change", "volume", "market_cap"]},
            {"fetch_calendar": ["next_earnings"]},
            {"fetch_headlines": {"limit": 3, "source": "yahoo"}},
            {"draft_note": "morning_scan_template"},
        ],
        "outputs": {
            "ledger_session": True,
            "obsidian_path_template": "Routines/Morning Scan {{date}}.md",
        },
        "source_candidate": {
            "confidence": candidate.get("confidence"),
            "evidence_count": candidate.get("evidence_count"),
            "hosts": candidate.get("hosts"),
        },
        "created_at": utc_now_iso(),
    }


def load_spec(ritual_id: str) -> Dict[str, Any]:
    path = ritual_specs_dir() / f"{ritual_id}.json"
    if not path.exists():
        raise RuntimeError(
            f"No spec for '{ritual_id}'. Run: analyst rituals suggest {ritual_id}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def approve_spec(ritual_id: str) -> Dict[str, Any]:
    spec = load_spec(ritual_id)
    spec["approved"] = True
    spec["approved_at"] = utc_now_iso()
    path = ritual_specs_dir() / f"{ritual_id}.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec


def _validate_ritual_id(ritual_id: str) -> str:
    rid = (ritual_id or "").strip()
    if not rid or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,120}$", rid):
        raise ValueError(f"invalid ritual_id: {ritual_id!r}")
    return rid


def list_specs() -> List[Dict[str, Any]]:
    """Return all persisted workflow specs (metadata + body)."""
    out: List[Dict[str, Any]] = []
    for path in sorted(ritual_specs_dir().glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ritual_id = spec.get("name") or path.stem
        review = ritual_specs_dir() / f"{ritual_id}_review.md"
        out.append(
            {
                "ritual_id": ritual_id,
                "spec_path": str(path),
                "review_path": str(review) if review.exists() else None,
                "approved": bool(spec.get("approved")),
                "runner": spec.get("runner"),
                "watchlist": spec.get("watchlist") or [],
                "spec": spec,
            }
        )
    return out


def build_dir_for(ritual_id: str) -> Path:
    return ritual_builds_dir() / _validate_ritual_id(ritual_id)


def build_status(ritual_id: str) -> Dict[str, Any]:
    rid = _validate_ritual_id(ritual_id)
    bdir = build_dir_for(rid)
    skill = bdir / "SKILL.md"
    return {
        "built": skill.exists(),
        "build_dir": str(bdir) if bdir.exists() else None,
        "files": sorted(p.name for p in bdir.iterdir()) if bdir.exists() else [],
    }


def last_runs(ledger: Ledger, limit: int = 300) -> Dict[str, Dict[str, Any]]:
    """Newest ritual_run per ritual_id: {rid: {ts, stub, error_count, session_id}}."""
    out: Dict[str, Dict[str, Any]] = {}
    events = ledger.list_events(limit=limit, types=["ritual_run"])
    for ev in events:  # newest-first; keep only the first per ritual
        payload = ev.get("payload") or {}
        rid = payload.get("ritual_id")
        if not rid or rid in out:
            continue
        out[rid] = {
            "ts": ev.get("ts"),
            "stub": bool(payload.get("stub")),
            "error_count": len(payload.get("errors") or []),
            "session_id": ev.get("session_id"),
        }
    return out


def list_automations(ledger: Optional[Ledger] = None) -> List[Dict[str, Any]]:
    """
    Merge mined candidates + specs into a dashboard-friendly list.

    Specs without a matching candidate still appear (e.g. demo fixtures).
    Pass a ledger to include each ritual's most recent run outcome.
    """
    runs = last_runs(ledger) if ledger is not None else {}
    by_id: Dict[str, Dict[str, Any]] = {}
    for c in load_candidates():
        rid = c.get("ritual_id")
        if not rid:
            continue
        by_id[rid] = {
            "ritual_id": rid,
            "confidence": c.get("confidence"),
            "evidence_count": c.get("evidence_count"),
            "host_family": c.get("host_family"),
            "slot": c.get("slot"),
            "watchlist": c.get("watchlist") or [],
            "when": c.get("when"),
            "has_candidate": True,
            "has_spec": False,
            "approved": False,
            "runner": None,
            "build": build_status(rid),
        }
    for s in list_specs():
        rid = s["ritual_id"]
        row = by_id.get(rid) or {
            "ritual_id": rid,
            "confidence": (s["spec"].get("source_candidate") or {}).get("confidence"),
            "evidence_count": (s["spec"].get("source_candidate") or {}).get(
                "evidence_count"
            ),
            "host_family": None,
            "slot": None,
            "watchlist": s.get("watchlist") or [],
            "when": None,
            "has_candidate": False,
            "build": build_status(rid),
        }
        row["has_spec"] = True
        row["approved"] = s["approved"]
        row["enabled"] = bool(s["spec"].get("enabled", True))
        row["runner"] = s.get("runner")
        row["model"] = s["spec"].get("model")
        row["watchlist"] = s.get("watchlist") or row.get("watchlist") or []
        row["build"] = build_status(rid)
        by_id[rid] = row
    for rid, row in by_id.items():
        row["last_run"] = runs.get(rid)
    items = list(by_id.values())
    items.sort(
        key=lambda r: (
            -float(r.get("confidence") or 0),
            -(r.get("evidence_count") or 0),
            r.get("ritual_id") or "",
        )
    )
    return items


def get_automation_detail(ritual_id: str, ledger: Optional[Ledger] = None) -> Dict[str, Any]:
    ledger = ledger or Ledger()
    rid = _validate_ritual_id(ritual_id)
    candidate: Optional[Dict[str, Any]] = None
    for c in load_candidates():
        if c.get("ritual_id") == rid:
            candidate = c
            break
    spec: Optional[Dict[str, Any]] = None
    review_md: Optional[str] = None
    try:
        spec = load_spec(rid)
        review_path = ritual_specs_dir() / f"{rid}_review.md"
        if review_path.exists():
            review_md = review_path.read_text(encoding="utf-8")
    except RuntimeError:
        pass
    if not candidate and not spec:
        raise RuntimeError(
            f"Ritual '{rid}' not found. Mine candidates or suggest a spec first."
        )

    excluded_sessions = set((candidate or {}).get("excluded_sessions") or [])
    excluded_events = set((candidate or {}).get("excluded_event_ids") or [])
    session_ids = list((candidate or {}).get("evidence_sessions") or [])
    evidence: List[Dict[str, Any]] = []
    for sid in session_ids:
        session = ledger.get_session(sid)
        events = ledger.list_events(session_id=sid, limit=200)
        events = list(reversed(events))  # chronological
        evidence.append(
            {
                "session_id": sid,
                "title": session.title if session else sid,
                "status": session.status if session else "unknown",
                "started_at": session.started_at if session else None,
                "included": sid not in excluded_sessions,
                "events": [
                    {
                        **ev,
                        "included": ev.get("event_id") not in excluded_events,
                    }
                    for ev in events
                ],
            }
        )

    return {
        "ritual_id": rid,
        "candidate": candidate,
        "spec": spec,
        "review_md": review_md,
        "approved": bool(spec and spec.get("approved")),
        "enabled": bool(
            (candidate or {}).get("enabled", True)
            if candidate is not None
            else (spec or {}).get("enabled", True)
        ),
        "build": build_status(rid),
        "claude_skills_dir": str(claude_skills_dir()) if claude_skills_dir() else None,
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "evidence": evidence,
        "active_evidence_sessions": active_evidence_sessions(candidate),
        "watchlist": (spec or {}).get("watchlist")
        or (candidate or {}).get("watchlist")
        or [],
    }


def _redacted_sample_context(
    candidate: Optional[Dict[str, Any]],
    spec: Dict[str, Any],
) -> Dict[str, Any]:
    """Allowlisted sample context only — no restricted notes, redacted text."""
    hints = []
    if candidate:
        for h in candidate.get("note_hints") or []:
            hints.append(redact_text(str(h)))
    return {
        "ritual_id": spec.get("name"),
        "runner": spec.get("runner"),
        "watchlist": list(spec.get("watchlist") or [])[:20],
        "schedule": spec.get("schedule"),
        "steps": spec.get("steps") or [],
        "hosts": (candidate or {}).get("hosts") or (spec.get("source_candidate") or {}).get(
            "hosts"
        )
        or {},
        "note_hints": hints,
        "allowlisted_fields": [
            "symbol",
            "price",
            "pct_change",
            "volume",
            "market_cap",
            "next_earnings",
            "headlines",
        ],
        "sensitivity_rules": {
            "max_egress": Sensitivity.INTERNAL.value,
            "never_include": [
                Sensitivity.RESTRICTED.value,
                "raw confidential note dumps",
                "account/client identifiers",
            ],
        },
    }


def _skill_md(ritual_id: str, spec: Dict[str, Any], sample: Dict[str, Any]) -> str:
    runner = spec.get("runner") or "generic_watchlist_scan"
    watch = ", ".join(spec.get("watchlist") or []) or "(from spec)"
    steps_md = "\n".join(
        f"{i}. `{json.dumps(step, ensure_ascii=False)}`"
        for i, step in enumerate(spec.get("steps") or [], 1)
    ) or "1. (see workflow.json)"
    return f"""---
name: analyst-ritual-{ritual_id}
description: Run the Analyst Ledger ritual `{ritual_id}` via local CLI. Never exfiltrate restricted data.
---

# Analyst Ritual: {ritual_id}

## Purpose

Execute the approved research ritual **{ritual_id}** using the local Analyst Ledger.
Prefer the deterministic local runner over inventing market data.

## Hard rules (non-negotiable)

1. **Never** include `restricted` or confidential raw note dumps in prompts, skills context, or chat.
2. Only use **allowlisted** market fields: `{', '.join(sample['allowlisted_fields'])}`.
3. Prefer calling the local CLI / tools below instead of scraping arbitrary sites.
4. Do **not** recommend trades or invent prices — use `analyst rituals run` output.
5. Max egress sensitivity is `{Sensitivity.INTERNAL.value}` unless a human raises it explicitly.

## Inputs

- Ritual id: `{ritual_id}`
- Runner: `{runner}`
- Default watchlist: {watch}
- Schedule hint: `{spec.get('schedule') or 'n/a'}`

## Workflow steps

{steps_md}

## How to run (local)

```bash
# Stub (offline / CI):
analyst rituals run {ritual_id} --stub --require-approved

# Live Yahoo public quotes (morning_yf_scan runner):
analyst rituals run {ritual_id} --require-approved
```

Or from this build package:

```bash
./runner.sh --stub
```

## Outputs

- Ledger session + morning scan note (ritual surface)
- Artifact under `data/artifacts/<session_id>/`
- Optional Obsidian path if `--obsidian` is passed

## Integrate

See `INTEGRATE.md` in this package. Claude Skill install uses `ANALYST_CLAUDE_SKILLS_DIR`.
"""


def _integrate_md(ritual_id: str, spec: Dict[str, Any], build_dir: Path) -> str:
    runner = spec.get("runner") or "morning_yf_scan"
    skills = claude_skills_dir()
    skills_hint = str(skills) if skills else "$ANALYST_CLAUDE_SKILLS_DIR"
    return f"""# Integrate: {ritual_id}

Build directory: `{build_dir}`

## Option A — Claude Skill

1. Set a skills directory (Claude Desktop / Claude Code skills folder):

   ```bash
   export ANALYST_CLAUDE_SKILLS_DIR="{skills_hint}"
   ```

2. Install from the dashboard **Integrate → Claude Skill**, or:

   ```bash
   analyst rituals integrate {ritual_id} --target claude-skill
   ```

   This copies `SKILL.md` (and supporting files) to:
   `{skills_hint}/analyst-ritual-{ritual_id}/`

3. Restart / reload Claude so the skill is picked up.
4. When the skill runs, it must call **local** `analyst rituals run` — do not paste restricted notes into the chat.

## Option B — Local environment (CLI + cron / OpenClaw)

1. Ensure the package is installed and `ANALYST_LEDGER_DATA` points at your ledger data dir.
2. Approve + run:

   ```bash
   analyst rituals approve {ritual_id}
   analyst rituals run {ritual_id} --require-approved --stub   # smoke
   analyst rituals run {ritual_id} --require-approved          # live
   ```

3. Or use the package launcher:

   ```bash
   {build_dir / 'runner.sh'} --stub
   ```

4. Schedule with system crontab or OpenClaw — see `docs/openclaw-cron-morning-yf.md`.
   Example cron (`{spec.get('schedule') or '0 7 * * 1-5'}`):

   ```bash
   analyst rituals run {ritual_id} --require-approved
   ```

5. Optional local integrate (writes a small launcher under builds):

   ```bash
   analyst rituals integrate {ritual_id} --target local
   ```

## Option C — Windows Task Scheduler

1. Approve the spec, then register the scheduled task (translates the spec's
   cron schedule automatically):

   ```powershell
   analyst rituals integrate {ritual_id} --target windows-task
   ```

2. Verify / remove:

   ```powershell
   schtasks /Query /TN "AnalystLedger_{ritual_id}"
   schtasks /Delete /TN "AnalystLedger_{ritual_id}" /F
   ```

3. The task runs `runner.ps1` in this build directory, which calls the local
   `analyst rituals run {ritual_id} --require-approved`.

## Runner notes

- Spec runner: `{runner}`
- Yahoo-family rituals use `morning_yf_scan`.
- Set `ANTHROPIC_API_KEY` only for suggest/build narratives that call Claude; default is `local_stub`.
"""


def _runner_sh(ritual_id: str, spec: Dict[str, Any]) -> str:
    # Pin Python + ledger dir at build time: cron/launchd inherit neither the
    # interactive shell's PATH/venv nor ANALYST_LEDGER_DATA.
    py = Path(sys.executable).resolve()
    return f"""#!/usr/bin/env bash
# Auto-generated launcher for ritual: {ritual_id} (macOS / Linux)
set -euo pipefail
export ANALYST_LEDGER_DATA="${{ANALYST_LEDGER_DATA:-{data_dir()}}}"
RITUAL_ID="{ritual_id}"
EXTRA=("$@")
if [[ ${{#EXTRA[@]}} -eq 0 ]]; then
  EXTRA=(--require-approved)
fi
exec "{py}" -m analyst_ledger.cli rituals run "$RITUAL_ID" "${{EXTRA[@]}}"
"""


def _runner_ps1(ritual_id: str, spec: Dict[str, Any]) -> str:
    """Windows launcher. Embeds the absolute Python path and ledger data dir
    from build time so the script works from Task Scheduler, where no venv is
    activated and no user environment variables are inherited."""
    py = Path(sys.executable).resolve()
    return f"""# Auto-generated Windows launcher for ritual: {ritual_id}
$ErrorActionPreference = "Stop"
if (-not $env:ANALYST_LEDGER_DATA) {{ $env:ANALYST_LEDGER_DATA = "{data_dir()}" }}
$extra = @($args)
if ($extra.Count -eq 0) {{ $extra = @("--require-approved") }}
& "{py}" -m analyst_ledger.cli rituals run "{ritual_id}" @extra
exit $LASTEXITCODE
"""


_CRON_DOW_NAMES = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


def parse_cron_schedule(schedule: Optional[str]) -> Dict[str, Any]:
    """
    Translate a simple cron string ('minute hour dom month dow') into
    Task-Scheduler-friendly fields: {"time": "HH:MM", "days": [...], "daily": bool}.

    Only the minute/hour/day-of-week fields are honored (that is all the specs
    generate). Anything unparseable falls back to weekdays at 07:00.
    """
    default = {
        "time": "07:00",
        "days": ["MON", "TUE", "WED", "THU", "FRI"],
        "daily": False,
    }
    parts = str(schedule or "").split()
    if len(parts) < 5:
        return default
    minute_s, hour_s, _dom, _month, dow = parts[:5]
    try:
        minute, hour = int(minute_s), int(hour_s)
    except ValueError:
        return default
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return default
    time = f"{hour:02d}:{minute:02d}"
    if dow in {"*", "?"}:
        return {"time": time, "days": [], "daily": True}
    days: List[str] = []
    for chunk in dow.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            try:
                a, b = (int(x) for x in chunk.split("-", 1))
            except ValueError:
                return {"time": time, "days": [], "daily": True}
            if a > b:  # wrap-around ranges are rare; treat as daily
                return {"time": time, "days": [], "daily": True}
            days.extend(_CRON_DOW_NAMES[d % 7] for d in range(a, b + 1))
        else:
            try:
                days.append(_CRON_DOW_NAMES[int(chunk) % 7])
            except ValueError:
                return {"time": time, "days": [], "daily": True}
    seen = set()
    days = [d for d in days if not (d in seen or seen.add(d))]
    return {"time": time, "days": days, "daily": not days}


def schtasks_create_args(
    task_name: str, runner_ps1: Path, schedule: Optional[str]
) -> List[str]:
    """Build the schtasks.exe argument list for a ritual's scheduled task."""
    sched = parse_cron_schedule(schedule)
    tr = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{runner_ps1}"'
    args = ["schtasks", "/Create", "/F", "/TN", task_name, "/TR", tr, "/ST", sched["time"]]
    if sched["daily"]:
        args += ["/SC", "DAILY"]
    else:
        args += ["/SC", "WEEKLY", "/D", ",".join(sched["days"])]
    return args


def build_ritual(
    ritual_id: str,
    ledger: Optional[Ledger] = None,
    require_approved: bool = False,
) -> Dict[str, Any]:
    """
    Create an implementation package under data/rituals/builds/<ritual_id>/.

    Artifacts: SKILL.md, workflow.json, runner.sh, INTEGRATE.md, sample_context.json
    """
    ledger = ledger or Ledger()
    rid = _validate_ritual_id(ritual_id)
    spec = load_spec(rid)
    if require_approved and not spec.get("approved"):
        raise RuntimeError(
            f"Spec '{rid}' is not approved. Run: analyst rituals approve {rid}"
        )

    candidate: Optional[Dict[str, Any]] = None
    try:
        candidate = get_candidate(rid)
    except RuntimeError:
        candidate = None

    bdir = build_dir_for(rid)
    bdir.mkdir(parents=True, exist_ok=True)

    sample = _redacted_sample_context(candidate, spec)
    workflow = {
        "ritual_id": rid,
        "version": spec.get("version", 1),
        "approved": bool(spec.get("approved")),
        "runner": spec.get("runner"),
        "schedule": spec.get("schedule"),
        "watchlist": spec.get("watchlist") or [],
        "steps": spec.get("steps") or [],
        "outputs": spec.get("outputs") or {},
        "built_at": utc_now_iso(),
    }

    files = {
        "SKILL.md": _skill_md(rid, spec, sample),
        "workflow.json": json.dumps(workflow, indent=2) + "\n",
        "INTEGRATE.md": _integrate_md(rid, spec, bdir),
        "sample_context.json": json.dumps(sample, indent=2) + "\n",
        "runner.sh": _runner_sh(rid, spec),
        "runner.ps1": _runner_ps1(rid, spec),
        "manifest.json": json.dumps(
            {
                "ritual_id": rid,
                "built_at": utc_now_iso(),
                "files": [
                    "SKILL.md",
                    "workflow.json",
                    "runner.sh",
                    "runner.ps1",
                    "INTEGRATE.md",
                    "sample_context.json",
                    "manifest.json",
                ],
            },
            indent=2,
        )
        + "\n",
    }
    written: List[str] = []
    for name, content in files.items():
        path = bdir / name
        path.write_text(content, encoding="utf-8")
        written.append(name)
        if name == "runner.sh":
            path.chmod(path.stat().st_mode | 0o111)

    ledger.append_event(
        Event(
            type="ritual_build",
            surface=Surface.RITUAL.value,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={"ritual_id": rid, "build_dir": str(bdir), "files": written},
        )
    )
    return {
        "status": "ok",
        "ritual_id": rid,
        "build_dir": str(bdir),
        "files": written,
    }


def integrate_ritual(
    ritual_id: str,
    target: str = "claude-skill",
    ledger: Optional[Ledger] = None,
) -> Dict[str, Any]:
    """
    Install a built package into Claude skills dir or prepare a local launcher.

    Targets:
      - claude-skill: copy SKILL.md (+ helpers) to ANALYST_CLAUDE_SKILLS_DIR
      - local: ensure runner + write local_launcher.sh under the build dir
      - windows-task: register runner.ps1 with Windows Task Scheduler
    """
    ledger = ledger or Ledger()
    rid = _validate_ritual_id(ritual_id)
    target = (target or "").strip().lower().replace("_", "-")
    if target not in {"claude-skill", "local", "windows-task"}:
        raise ValueError("target must be 'claude-skill', 'local', or 'windows-task'")

    bdir = build_dir_for(rid)

    # Fail fast on the wrong OS so we don't force a build just to return guidance.
    if target == "windows-task" and platform.system() != "Windows":
        return {
            "status": "needs_config",
            "ritual_id": rid,
            "target": target,
            "message": (
                "windows-task requires Windows. On macOS/Linux use "
                "`--target local` and point cron/OpenClaw at runner.sh "
                "(see docs/openclaw-cron-morning-yf.md)."
            ),
            "build_dir": str(bdir),
        }

    # Rebuild if never built, or built before runner.ps1 existed
    if not (bdir / "SKILL.md").exists() or not (bdir / "runner.ps1").exists():
        build_ritual(rid, ledger=ledger)

    if target == "windows-task":
        spec = load_spec(rid)
        task_name = f"AnalystLedger_{rid}"
        args = schtasks_create_args(task_name, bdir / "runner.ps1", spec.get("schedule"))
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return {
                "status": "error",
                "ritual_id": rid,
                "target": target,
                "message": (proc.stderr or proc.stdout or "schtasks failed").strip(),
            }
        sched = parse_cron_schedule(spec.get("schedule"))
        ledger.append_event(
            Event(
                type="ritual_integrate",
                surface=Surface.RITUAL.value,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "ritual_id": rid,
                    "target": target,
                    "task_name": task_name,
                    "schedule": sched,
                },
            )
        )
        return {
            "status": "ok",
            "ritual_id": rid,
            "target": target,
            "task_name": task_name,
            "schedule": sched,
            "verify_hint": f'schtasks /Query /TN "{task_name}"',
            "remove_hint": f'schtasks /Delete /TN "{task_name}" /F',
        }

    if target == "claude-skill":
        dest_root = claude_skills_dir()
        if not dest_root:
            return {
                "status": "needs_config",
                "ritual_id": rid,
                "target": target,
                "message": (
                    "Set ANALYST_CLAUDE_SKILLS_DIR to your Claude skills folder, then retry. "
                    f"Package is ready at {bdir}. See INTEGRATE.md."
                ),
                "build_dir": str(bdir),
                "skill_path": str(bdir / "SKILL.md"),
            }
        dest = dest_root / f"analyst-ritual-{rid}"
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("SKILL.md", "workflow.json", "sample_context.json", "INTEGRATE.md"):
            src = bdir / name
            if src.exists():
                shutil.copy2(src, dest / name)
        ledger.append_event(
            Event(
                type="ritual_integrate",
                surface=Surface.RITUAL.value,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "ritual_id": rid,
                    "target": target,
                    "dest": str(dest),
                },
            )
        )
        return {
            "status": "ok",
            "ritual_id": rid,
            "target": target,
            "dest": str(dest),
            "skill_path": str(dest / "SKILL.md"),
        }

    # local
    launcher = bdir / "local_launcher.sh"
    launcher.write_text(
        f"""#!/usr/bin/env bash
# Local environment launcher for {rid}
set -euo pipefail
cd "$(dirname "$0")"
exec ./runner.sh "$@"
""",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | 0o111)
    ledger.append_event(
        Event(
            type="ritual_integrate",
            surface=Surface.RITUAL.value,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={
                "ritual_id": rid,
                "target": "local",
                "launcher": str(launcher),
            },
        )
    )
    return {
        "status": "ok",
        "ritual_id": rid,
        "target": "local",
        "launcher": str(launcher),
        "build_dir": str(bdir),
        "run_hint": f"analyst rituals run {rid} --require-approved",
        "docs": "docs/openclaw-cron-morning-yf.md",
    }


def default_suggest_destination() -> str:
    """Use anthropic when ANTHROPIC_API_KEY is set; otherwise local_stub."""
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    return "local_stub"
