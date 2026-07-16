"""Redaction and allowlisting before any external model call."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Pattern

from .schema import Sensitivity, sensitivity_allows_egress

# Patterns that look like account / counterparty / email identifiers
_DEFAULT_PATTERNS: List[Pattern[str]] = [
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b(?:acct|account|client|counterparty)[\s#:.-]*\d{4,}\b", re.I),
    re.compile(r"\b(?:SSN|EIN)[\s#:.-]*\d[\d-]+\b", re.I),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-like
]


def redact_text(text: str, extra_patterns: Optional[List[Pattern[str]]] = None) -> str:
    out = text
    for pat in _DEFAULT_PATTERNS + (extra_patterns or []):
        out = pat.sub("[REDACTED]", out)
    return out


def filter_events_for_egress(
    events: List[Dict[str, Any]],
    max_sensitivity: Sensitivity = Sensitivity.INTERNAL,
) -> List[Dict[str, Any]]:
    """Drop restricted / over-max events; redact text fields in the rest."""
    allowed: List[Dict[str, Any]] = []
    for ev in events:
        level = Sensitivity(ev.get("sensitivity", Sensitivity.INTERNAL.value))
        if not sensitivity_allows_egress(level, max_sensitivity):
            continue
        payload = dict(ev.get("payload") or {})
        if "text" in payload and isinstance(payload["text"], str):
            payload["text"] = redact_text(payload["text"])
        if "notes" in payload and isinstance(payload["notes"], str):
            payload["notes"] = redact_text(payload["notes"])
        allowed.append(
            {
                "ts": ev.get("ts"),
                "type": ev.get("type"),
                "surface": ev.get("surface"),
                "sensitivity": level.value,
                "payload": payload,
            }
        )
    return allowed


def build_synthesis_prompt(context: Dict[str, Any], instruction: str) -> str:
    """Assemble an allowlisted prompt from session context."""
    session = context.get("session") or {}
    lines = [
        "You are assisting a buy-side research analyst. Draft from THEIR workflow data only.",
        "Do not invent market facts. Do not recommend trades. Structure a research memo / next-checks list.",
        "",
        f"Instruction: {instruction.strip()}",
        "",
        f"Session title: {session.get('title', '')}",
        f"Surface: {session.get('surface', '')}",
        f"Desk tag: {session.get('desk_tag') or '(none)'}",
        f"Tags: {', '.join(session.get('tags') or []) or '(none)'}",
        "",
        "Events (redacted, chronological):",
    ]
    for ev in context.get("events") or []:
        payload = ev.get("payload") or {}
        summary = _payload_summary(ev.get("type", ""), payload)
        lines.append(f"- [{ev.get('ts')}] {ev.get('type')} ({ev.get('surface')}): {summary}")
    arts = context.get("artifacts") or []
    if arts:
        lines.append("")
        lines.append("Attached artifacts (metadata only; content not inlined):")
        for a in arts:
            lines.append(
                f"- {a.get('artifact_id')} mime={a.get('mime')} sha256={a.get('sha256', '')[:12]}…"
            )
    lines.append("")
    lines.append(
        "Output markdown with: Summary, What was examined, Open questions, Suggested next checks."
    )
    return "\n".join(lines)


def _payload_summary(event_type: str, payload: Dict[str, Any]) -> str:
    if event_type == "note":
        return redact_text(str(payload.get("text", ""))[:500])
    if event_type == "symbol_focus":
        return f"symbol={payload.get('symbol')} interval={payload.get('interval')}"
    if event_type == "interval_change":
        return f"interval={payload.get('interval')}"
    if event_type == "drawing_meta":
        return f"drawings={payload.get('count', payload.get('drawings', '?'))}"
    if event_type == "inbox_file":
        return f"file={payload.get('name')} path={payload.get('path')}"
    if event_type == "agent_stop":
        return "cursor agent stop"
    if event_type == "shell":
        cmd = str(payload.get("command", ""))[:200]
        return f"cmd={redact_text(cmd)}"
    if event_type == "url_focus":
        return (
            f"host={payload.get('host')} path={payload.get('path')} "
            f"symbol={payload.get('symbol')} section={payload.get('section')}"
        )
    if event_type == "external_note":
        return (
            f"source={payload.get('source')} title={payload.get('title')} "
            f"chars={payload.get('chars')}"
        )
    if event_type == "file_edit":
        return f"file={payload.get('path', payload.get('file', ''))}"
    if event_type == "tag":
        return f"tag={payload.get('tag')}"
    # Generic compact dump
    try:
        import json

        return redact_text(json.dumps(payload, ensure_ascii=False)[:400])
    except (TypeError, ValueError):
        return redact_text(str(payload)[:400])
