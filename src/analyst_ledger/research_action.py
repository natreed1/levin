"""Draft-then-approve research action for detected actionable chat asks.

Runs a deterministic public-web research pass for an entity and posts the result
into the chat thread as an UNAPPROVED draft (``approved=False``), carrying the
labels ``intent:research`` / ``entity:<slug>`` / ``state:done``.

NOTE (2026-07-21): this action is NOT wired into live chat. The chat layer only
*tags* actionable asks (``state:open``) as a data framework for later model
training; actually running the research is an opt-in future capability, parked
here for when the agent is allowed to act.

No model call is made — sources are public web results (Bing RSS, no API key),
so this never egresses thread content to an external model. Two safety gates:

- If the thread's sensitivity exceeds the ``internal`` egress ceiling, the live
  web search is skipped and only the ask is recorded (graceful offline note).
- ``stub=True`` runs fully offline (used by tests and smoke runs).

A model-synthesis upgrade can be layered on later behind the existing egress gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .actionable import ActionableDecision
from .labels import normalize_labels
from .schema import Sensitivity, parse_sensitivity, sensitivity_allows_egress


def _year() -> int:
    return datetime.now(timezone.utc).year


def _gather_sources(
    decision: ActionableDecision, *, symbol: Optional[str]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    from .finance_research import finance_search_queries
    from .web_search import bing_search, rank_search_hits

    if symbol:
        queries = finance_search_queries(symbol, intent="news")[:2]
    else:
        subject = decision.entity_raw or decision.entity.replace("-", " ")
        queries = [f"{subject} company overview", f"{subject} funding news {_year()}"]

    hits: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries[:2]:
        for hit in bing_search(query, limit=5):
            url = str(hit.get("url") or "")
            if url and url not in seen:
                seen.add(url)
                hits.append(hit)
    return rank_search_hits(hits)[:5], queries


def _render_draft(
    decision: ActionableDecision,
    symbol: Optional[str],
    hits: List[Dict[str, Any]],
    *,
    web_used: bool,
) -> str:
    from .web_search import format_hits_for_prompt

    subject = decision.entity_raw or decision.entity
    lines = [
        f"Research draft (unapproved) - {subject}",
        f"Triggered by: {decision.reason}",
    ]
    if symbol:
        lines.append(
            f"Ticker detected: {symbol} - run the outlook automation for full financials."
        )
    lines.append("")
    if not web_used:
        lines.append(
            "Public web research was not run (offline/stub, or the thread's "
            "sensitivity exceeds the egress ceiling). The ask is on record; "
            "approve to keep it as a research to-do."
        )
    elif hits:
        lines.append("Top public sources:")
        lines.append(format_hits_for_prompt(hits))
    else:
        lines.append(
            "No public results were retrieved (the search may be offline). The "
            "ask is on record; approve to keep it as a research to-do."
        )
    lines += [
        "",
        "Suggested next steps:",
        "- Confirm this is the right entity, then skim the sources above.",
        "- If it warrants ongoing tracking, add an automation on the /review page.",
        "",
        "[DRAFT - approve to keep or reject. No model was used; sources are public web results.]",
    ]
    return "\n".join(lines)


def execute_research_draft(
    ledger: Any, thread_id: str, decision: ActionableDecision, *, stub: bool = False
) -> Dict[str, Any]:
    """Background-job body: research the entity, post an unapproved draft reply."""
    from .finance_research import resolve_symbol

    session = ledger.get_session(thread_id)
    sens = parse_sensitivity(
        session.sensitivity if session else Sensitivity.INTERNAL.value
    )
    web_used = (not stub) and sensitivity_allows_egress(sens, Sensitivity.INTERNAL)

    try:
        symbol = resolve_symbol(
            decision.entity_raw or decision.entity, allow_network=False
        )
    except Exception:  # noqa: BLE001
        symbol = None

    hits: List[Dict[str, Any]] = []
    queries: List[str] = []
    if web_used:
        try:
            hits, queries = _gather_sources(decision, symbol=symbol)
        except Exception:  # noqa: BLE001 — research must never break the thread
            hits, queries = [], []

    draft = _render_draft(decision, symbol, hits, web_used=web_used)
    labels = normalize_labels(
        [f"entity:{decision.entity}", "intent:research", "state:done"]
    )
    ask_labels = normalize_labels(
        [f"entity:{decision.entity}", "intent:research", "state:open"]
    )
    ledger.append_chat_message(
        thread_id,
        role="assistant",
        content=draft,
        kind="research_draft",
        metadata={
            "approved": False,
            "labels": labels,
            "ask_labels": ask_labels,
            "entity": decision.entity,
            "symbol": symbol,
            "queries": queries,
            "web_used": web_used,
            "sources": [
                {"title": h.get("title"), "url": h.get("url")} for h in hits
            ],
            "actionable": decision.public(),
        },
    )
    return {
        "status": "ok",
        "thread_id": thread_id,
        "entity": decision.entity,
        "symbol": symbol,
        "labels": labels,
        "source_count": len(hits),
        "web_used": web_used,
    }
