"""Three-layer product catalog: Capabilities → Agents → Automations.

Capabilities are reusable verbs (built-in or mined from ledger data).
Agents are how those verbs get used in rooms — lenses are prompt injections;
operators may invoke capabilities. Automations are loops of capabilities
(born from chats via /automate; today surfaced from approved+enabled rituals).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Built-in capability atoms agents / automations can call.
BUILTIN_CAPABILITIES: List[Dict[str, Any]] = [
    {
        "id": "web_research",
        "name": "Web research",
        "kind": "builtin",
        "summary": "Background public-web research pass for a question or ticker.",
        "invoke": "mention + research keywords (e.g. @Analyst research NVDA)",
        "schedulable": False,
        "needs_model": True,
    },
    {
        "id": "morning_yf_scan",
        "name": "Morning quote scan",
        "kind": "builtin",
        "summary": "Deterministic Yahoo-style watchlist quote / calendar / headlines scan.",
        "invoke": "runner morning_yf_scan · @workflow · router",
        "schedulable": True,
        "needs_model": False,
        "runner": "morning_yf_scan",
    },
    {
        "id": "generic_watchlist_scan",
        "name": "Watchlist scan",
        "kind": "builtin",
        "summary": "Generic watchlist scan runner for recurring symbol checks.",
        "invoke": "runner generic_watchlist_scan · @workflow · router",
        "schedulable": True,
        "needs_model": False,
        "runner": "generic_watchlist_scan",
    },
    {
        "id": "sec_filings_check",
        "name": "SEC filings check",
        "kind": "builtin",
        "summary": "Pull recent SEC EDGAR filings for watchlist symbols.",
        "invoke": "runner sec_filings_check · @workflow · router",
        "schedulable": True,
        "needs_model": False,
        "runner": "sec_filings_check",
    },
    {
        "id": "note_digest",
        "name": "Note digest",
        "kind": "builtin",
        "summary": "Digest recent ledger notes into a short brief.",
        "invoke": "runner note_digest · @workflow · router",
        "schedulable": True,
        "needs_model": False,
        "runner": "note_digest",
    },
    {
        "id": "fetch_quote",
        "name": "Fetch quote",
        "kind": "builtin",
        "summary": "Governed workflow step: allowlisted quote fields.",
        "invoke": "WorkflowEngine step",
        "schedulable": False,
        "needs_model": False,
        "action": "fetch_quote",
    },
    {
        "id": "fetch_calendar",
        "name": "Fetch calendar",
        "kind": "builtin",
        "summary": "Governed workflow step: earnings / calendar fields.",
        "invoke": "WorkflowEngine step",
        "schedulable": False,
        "needs_model": False,
        "action": "fetch_calendar",
    },
    {
        "id": "fetch_headlines",
        "name": "Fetch headlines",
        "kind": "builtin",
        "summary": "Governed workflow step: recent headlines.",
        "invoke": "WorkflowEngine step",
        "schedulable": False,
        "needs_model": False,
        "action": "fetch_headlines",
    },
    {
        "id": "find_files",
        "name": "Find files",
        "kind": "builtin",
        "summary": "Search configured research folders (never the whole drive).",
        "invoke": "chat file-finder · WorkflowEngine step",
        "schedulable": False,
        "needs_model": False,
        "action": "find_files",
    },
    {
        "id": "public_web_search",
        "name": "Public web search",
        "kind": "builtin",
        "summary": "Allowlisted public web search step inside a governed workflow.",
        "invoke": "WorkflowEngine step",
        "schedulable": False,
        "needs_model": True,
        "action": "public_web_search",
    },
    {
        "id": "classify_message",
        "name": "Classify message",
        "kind": "builtin",
        "summary": "Tag chat/tracking messages with kind / entity / topic.",
        "invoke": "capture + classify sweep",
        "schedulable": True,
        "needs_model": False,
    },
]

# Agents: lenses reshape prompts; operators may call capabilities.
AGENT_DEFS: List[Dict[str, Any]] = [
    {
        "id": "qwen",
        "name": "Analyst",
        "mention": "@Analyst",
        "kind": "operator",
        "role": "analyst",
        "summary": "Evidence-led balanced analyst. Can chat or kick off web research.",
        "how": "Prompt lens plus capability calls when you ask to research / look up.",
        "capabilities": ["web_research", "classify_message"],
        "model": "room or Settings active profile",
    },
    {
        "id": "qwen-bull",
        "name": "Bullish Agent",
        "mention": "@Bullish",
        "kind": "lens",
        "role": "bull",
        "summary": "Steelmans the upside case. Does not own runners or schedules.",
        "how": "Prompt injection only — same model as the room, different system prompt.",
        "capabilities": [],
        "model": "room or Settings active profile",
    },
    {
        "id": "qwen-contrarian",
        "name": "Contrarian Agent",
        "mention": "@Contrarian",
        "kind": "lens",
        "role": "bear",
        "summary": "Evidence-led downside / falsification lens. Not a capability.",
        "how": "Prompt injection only — same model as the room, different system prompt.",
        "capabilities": [],
        "model": "room or Settings active profile",
    },
    {
        "id": "qwen-synthesizer",
        "name": "Synthesizer Agent",
        "mention": "@Synthesizer",
        "kind": "lens",
        "role": "synthesizer",
        "summary": "Closes multi-agent debate with shared facts and next checks.",
        "how": "Prompt injection in specialist present / debate / idea runs.",
        "capabilities": [],
        "model": "room or Settings active profile",
    },
    {
        "id": "master",
        "name": "Master",
        "mention": "Agents → Master thread",
        "kind": "operator",
        "role": "router",
        "summary": "Routes asks to approved capability loops before calling a model.",
        "how": "Deterministic router → runners / workflows; model only on gaps.",
        "capabilities": [
            "morning_yf_scan",
            "generic_watchlist_scan",
            "sec_filings_check",
            "note_digest",
            "web_research",
            "find_files",
        ],
        "model": "Settings active profile (for novel asks)",
    },
]


def _user_capability_from_ritual(row: Dict[str, Any]) -> Dict[str, Any]:
    rid = str(row.get("ritual_id") or row.get("name") or "").strip()
    approved = bool(row.get("approved"))
    enabled = bool(row.get("enabled", True)) if approved else False
    status = "approved" if approved else "draft"
    if approved and not row.get("enabled", True):
        status = "disabled"
    return {
        "id": rid,
        "name": rid.replace("_", " "),
        "kind": "user",
        "summary": (
            f"Mined / drafted capability"
            f"{' · runner ' + str(row.get('runner')) if row.get('runner') else ''}."
        ),
        "invoke": f"@workflow {rid}" if approved else "approve first",
        "schedulable": bool(row.get("runner") or row.get("schedule")),
        "needs_model": bool(row.get("model")),
        "status": status,
        "approved": approved,
        "enabled": enabled,
        "runner": row.get("runner"),
        "model": row.get("model"),
        "schedule": row.get("schedule"),
        "watchlist": row.get("watchlist") or [],
        "last_run": row.get("last_run"),
        "ritual_id": rid,
        "has_spec": bool(row.get("has_spec")),
        "has_candidate": bool(row.get("has_candidate")),
        "confidence": row.get("confidence"),
        "evidence_count": row.get("evidence_count"),
    }


def list_capabilities(ledger: Any = None) -> List[Dict[str, Any]]:
    """Built-in verbs + per-user ritual specs as capabilities."""
    out: List[Dict[str, Any]] = [dict(c) for c in BUILTIN_CAPABILITIES]
    if ledger is None:
        return out
    from analyst_ledger.rituals import list_automations, load_spec

    for row in list_automations(ledger):
        cap = _user_capability_from_ritual(row)
        try:
            spec = load_spec(cap["id"])
        except Exception:
            spec = None
        if isinstance(spec, dict):
            if spec.get("schedule") and not cap.get("schedule"):
                cap["schedule"] = spec.get("schedule")
            cap["proposed_by"] = spec.get("proposed_by")
            steps = []
            for step in spec.get("steps") or []:
                if isinstance(step, dict) and step:
                    steps.append(str(next(iter(step))))
            if steps:
                cap["steps"] = steps
            if not cap.get("summary") or str(cap.get("summary") or "").startswith("Mined"):
                desc = (spec.get("description") or "").strip()
                if desc:
                    cap["summary"] = desc[:240]
        out.append(cap)
    return out


def list_agents_catalog() -> List[Dict[str, Any]]:
    """Agents tab rows: lenses vs operators and which capabilities they use."""
    by_id = {c["id"]: c for c in BUILTIN_CAPABILITIES}
    rows: List[Dict[str, Any]] = []
    for agent in AGENT_DEFS:
        caps = []
        for cid in agent.get("capabilities") or []:
            meta = by_id.get(cid) or {"id": cid, "name": cid}
            caps.append({"id": meta["id"], "name": meta.get("name") or cid})
        rows.append(
            {
                **agent,
                "capability_details": caps,
                "uses_capabilities": bool(caps),
            }
        )
    return rows


def list_automation_loops(ledger: Any = None) -> List[Dict[str, Any]]:
    """Capability loops ready to run (approved + enabled user rituals).

    Room-native /automate compositions will land here later; until then
    approved rituals are the automation instances.
    """
    if ledger is None:
        return []
    from analyst_ledger.rituals import list_automations, load_spec

    loops: List[Dict[str, Any]] = []
    for row in list_automations(ledger):
        if not row.get("approved"):
            continue
        if not row.get("enabled", True):
            continue
        rid = str(row.get("ritual_id") or "")
        steps: List[str] = []
        schedule = row.get("schedule")
        try:
            spec = load_spec(rid)
        except Exception:
            spec = None
        if isinstance(spec, dict):
            schedule = schedule or spec.get("schedule")
            for step in spec.get("steps") or []:
                if isinstance(step, dict) and step:
                    steps.append(str(next(iter(step))))
                elif isinstance(step, str):
                    steps.append(step)
        loops.append(
            {
                "id": rid,
                "name": rid.replace("_", " "),
                "ritual_id": rid,
                "kind": "ritual_loop",
                "source": "approved_capability",
                "capabilities": steps
                or ([row.get("runner")] if row.get("runner") else []),
                "runner": row.get("runner"),
                "schedule": schedule,
                "model": row.get("model") or (spec or {}).get("model"),
                "watchlist": row.get("watchlist") or [],
                "last_run": row.get("last_run"),
                "room_id": (spec or {}).get("room_id") if isinstance(spec, dict) else None,
            }
        )
    return loops
