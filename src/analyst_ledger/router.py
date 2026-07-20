"""Deterministic chat router: keyword/term-frequency dispatch to approved runners.

Decision functions here make NO model calls, NO network requests, and NO ledger
writes — pure scoring over the incoming message and on-disk specs. The only
side-effecting function is ``execute_routed_run``, which is the background-job
body that runs the matched runner and posts the reply into the chat thread.

Safety invariants:
- Only approved + enabled specs whose runner exists in ``runners.RUNNERS`` are
  routable; approval is re-checked at execution time (``require_approved=True``).
- Ambiguous or low-scoring messages return ``matched=False`` so the caller
  falls through to the existing model paths unchanged.
- Kill switch: ``ANALYST_CHAT_ROUTER=off`` disables routing entirely.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

ROUTER_ENV = "ANALYST_CHAT_ROUTER"
THRESHOLD_ENV = "ANALYST_CHAT_ROUTER_THRESHOLD"
MARGIN_ENV = "ANALYST_CHAT_ROUTER_MARGIN"
DEFAULT_THRESHOLD = 5.0
DEFAULT_MARGIN = 2.0

# Intent tags per runner (keys must match runners.RUNNERS)
RUNNER_INTENTS: Dict[str, Set[str]] = {
    "sec_filings_check": {"filings"},
    "morning_yf_scan": {"snapshot", "outlook"},
    "generic_watchlist_scan": {"snapshot", "outlook"},
    "note_digest": set(),
}
RUNNER_KEYWORDS: Dict[str, Set[str]] = {
    "sec_filings_check": {"filing", "filings", "sec", "edgar", "8-k", "10-q", "10-k"},
    "morning_yf_scan": {"scan", "watchlist", "quote", "quotes", "price", "prices", "morning", "yahoo"},
    "generic_watchlist_scan": {"scan", "watchlist", "quote", "quotes", "price", "prices"},
    "note_digest": {"note", "notes", "digest", "summary", "summarize", "recap"},
}
# Runners that accept a watchlist override (note_digest ignores watchlists)
_WATCHLIST_RUNNERS = frozenset(
    {"sec_filings_check", "morning_yf_scan", "generic_watchlist_scan"}
)

STOPWORDS = frozenset(
    {
        "the", "a", "an", "any", "and", "or", "of", "for", "to", "in", "on", "is",
        "are", "what", "whats", "give", "me", "my", "can", "you", "do", "we",
        "have", "new", "recent", "please", "run", "check", "show", "get",
        "about", "with", "there", "how", "did", "does", "was", "were", "it",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

# Score weights / caps (see plan scoring table)
_W_TICKER_IN_WATCHLIST = 3.0
_CAP_TICKER = 6.0
_W_ALIAS_IN_WATCHLIST = 3.0
_CAP_ALIAS = 6.0
_W_INTENT = 2.0
_W_RID_TOKEN = 2.0
_CAP_RID = 4.0
_W_KEYWORD = 2.0
_CAP_KEYWORD = 4.0
_W_GENERAL = 1.0
_CAP_GENERAL = 3.0
_W_TICKER_OFF_WATCHLIST = 2.0
_PENALTY_INTENT_MISMATCH = -2.0


def router_enabled() -> bool:
    raw = os.environ.get(ROUTER_ENV, "on").strip().lower()
    return raw not in {"off", "0", "false", "no"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _tokens(text: str) -> List[str]:
    return [
        t
        for t in _TOKEN_RE.findall((text or "").casefold())
        if t not in STOPWORDS and len(t) >= 3
    ]


@dataclass
class RouteEntry:
    ritual_id: str
    runner: str
    watchlist: List[str]
    ritual_tokens: Set[str]
    runner_tokens: Set[str]
    extra_tokens: Set[str]
    intents: Set[str]


@dataclass
class RouteDecision:
    matched: bool
    ritual_id: Optional[str] = None
    runner: Optional[str] = None
    score: float = 0.0
    runner_up_score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    tickers: List[str] = field(default_factory=list)
    watchlist_override: Optional[List[str]] = None
    intent: str = "general"

    def public(self) -> Dict[str, Any]:
        return {
            "matched": self.matched,
            "ritual_id": self.ritual_id,
            "runner": self.runner,
            "score": round(self.score, 2),
            "runner_up_score": round(self.runner_up_score, 2),
            "reasons": list(self.reasons),
            "tickers": list(self.tickers),
            "watchlist_override": (
                list(self.watchlist_override) if self.watchlist_override else None
            ),
            "intent": self.intent,
        }


def build_route_index() -> List[RouteEntry]:
    """Routable entries from approved + enabled specs with a known runner."""
    from .rituals import list_specs, load_candidates
    from .runners import RUNNERS

    hints_by_rid: Dict[str, List[str]] = {}
    for cand in load_candidates():
        rid = str(cand.get("ritual_id") or "")
        if rid:
            hints_by_rid[rid] = [str(h) for h in (cand.get("note_hints") or [])]

    entries: List[RouteEntry] = []
    for row in list_specs():
        spec = row.get("spec") or {}
        if not row.get("approved"):
            continue
        if not spec.get("enabled", True):
            continue
        runner = str(row.get("runner") or "")
        if runner not in RUNNERS:
            continue
        rid = str(row["ritual_id"])

        ritual_tokens = set(_tokens(rid.replace("_", " ").replace("-", " ")))
        runner_tokens = set(RUNNER_KEYWORDS.get(runner, set()))
        runner_tokens |= set(_tokens(runner.replace("_", " ")))

        extra: Set[str] = set()
        for step in spec.get("steps") or []:
            if isinstance(step, dict):
                for action in step:
                    extra |= set(_tokens(str(action).replace("_", " ")))
        for hint in hints_by_rid.get(rid, []) + [
            str(h) for h in (spec.get("note_hints") or [])
        ]:
            extra |= set(_tokens(hint))
        extra -= ritual_tokens | runner_tokens

        entries.append(
            RouteEntry(
                ritual_id=rid,
                runner=runner,
                watchlist=[str(s).upper() for s in (spec.get("watchlist") or [])],
                ritual_tokens=ritual_tokens,
                runner_tokens=runner_tokens,
                extra_tokens=extra,
                intents=set(RUNNER_INTENTS.get(runner, set())),
            )
        )
    return entries


def score_message(
    text: str,
    entry: RouteEntry,
    *,
    tickers: List[str],
    intent: str,
    alias_symbols: List[str],
) -> Tuple[float, List[str]]:
    """Deterministic additive score for one entry. Pure function."""
    score = 0.0
    reasons: List[str] = []
    counts = Counter(_tokens(text))

    in_watch = [t for t in tickers if t in entry.watchlist]
    if in_watch:
        gained = min(_W_TICKER_IN_WATCHLIST * len(in_watch), _CAP_TICKER)
        score += gained
        reasons.append(f"ticker {', '.join(in_watch)} in watchlist (+{gained:g})")

    alias_hits = [
        s for s in alias_symbols if s in entry.watchlist and s not in in_watch
    ]
    if alias_hits:
        gained = min(_W_ALIAS_IN_WATCHLIST * len(alias_hits), _CAP_ALIAS)
        score += gained
        reasons.append(f"company alias {', '.join(alias_hits)} (+{gained:g})")

    if intent != "general" and intent in entry.intents:
        score += _W_INTENT
        reasons.append(f"intent {intent} (+{_W_INTENT:g})")
    elif intent != "general" and entry.intents and intent not in entry.intents:
        score += _PENALTY_INTENT_MISMATCH
        reasons.append(f"intent {intent} mismatch ({_PENALTY_INTENT_MISMATCH:g})")

    rid_hits = sorted(set(counts) & entry.ritual_tokens)
    if rid_hits:
        gained = min(_W_RID_TOKEN * len(rid_hits), _CAP_RID)
        score += gained
        reasons.append(f"name terms {', '.join(rid_hits)} (+{gained:g})")

    kw_hits = sorted(set(counts) & entry.runner_tokens)
    if kw_hits:
        gained = min(_W_KEYWORD * len(kw_hits), _CAP_KEYWORD)
        score += gained
        reasons.append(f"keywords {', '.join(kw_hits)} (+{gained:g})")

    gen_hits = sorted(set(counts) & entry.extra_tokens)
    if gen_hits:
        gained = min(
            sum(_W_GENERAL * min(counts[t], 2) for t in gen_hits), _CAP_GENERAL
        )
        score += gained
        reasons.append(f"terms {', '.join(gen_hits)} (+{gained:g})")

    off_watch = [t for t in tickers + alias_symbols if t not in entry.watchlist]
    if off_watch and entry.runner in _WATCHLIST_RUNNERS and (kw_hits or rid_hits):
        score += _W_TICKER_OFF_WATCHLIST
        reasons.append(
            f"ticker {', '.join(sorted(set(off_watch)))} (override) (+{_W_TICKER_OFF_WATCHLIST:g})"
        )

    return score, reasons


def route_message(
    text: str,
    *,
    index: Optional[List[RouteEntry]] = None,
    threshold: Optional[float] = None,
    margin: Optional[float] = None,
    restrict_to: Optional[str] = None,
) -> RouteDecision:
    """Score the message against routable automations; match only when confident."""
    from .finance_research import classify_finance_intent, resolve_symbol
    from .web_search import extract_tickers

    threshold = threshold if threshold is not None else _env_float(THRESHOLD_ENV, DEFAULT_THRESHOLD)
    margin = margin if margin is not None else _env_float(MARGIN_ENV, DEFAULT_MARGIN)

    entries = index if index is not None else build_route_index()
    if restrict_to:
        entries = [e for e in entries if e.ritual_id == restrict_to]
    if not entries:
        return RouteDecision(matched=False)

    tickers = extract_tickers(text, limit=2)
    alias = resolve_symbol(text, allow_network=False)
    alias_symbols = [alias] if alias and alias not in tickers else []
    intent = classify_finance_intent(text)

    scored: List[Tuple[float, List[str], RouteEntry]] = []
    for entry in entries:
        score, reasons = score_message(
            text, entry, tickers=tickers, intent=intent, alias_symbols=alias_symbols
        )
        scored.append((score, reasons, entry))
    scored.sort(key=lambda item: (-item[0], item[2].ritual_id))

    top_score, top_reasons, top = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    matched = top_score >= threshold and (top_score - runner_up) >= margin

    all_symbols = list(dict.fromkeys(tickers + alias_symbols))
    override: Optional[List[str]] = None
    if (
        matched
        and top.runner in _WATCHLIST_RUNNERS
        and all_symbols
        and any(s not in top.watchlist for s in all_symbols)
    ):
        override = all_symbols

    return RouteDecision(
        matched=matched,
        ritual_id=top.ritual_id if matched else None,
        runner=top.runner if matched else None,
        score=top_score,
        runner_up_score=runner_up,
        reasons=top_reasons if matched else [],
        tickers=all_symbols,
        watchlist_override=override,
        intent=intent,
    )


def execute_routed_run(
    ledger: Any, thread_id: str, decision: RouteDecision, *, stub: bool = False
) -> Dict[str, Any]:
    """Background-job body: run the matched runner, post the reply to the thread."""
    from .runners import resolve_runner

    try:
        runner_name, runner_fn = resolve_runner(
            decision.ritual_id, explicit=decision.runner
        )
        result = runner_fn(
            ledger=ledger,
            watchlist=decision.watchlist_override,
            ritual_id=decision.ritual_id,
            stub=stub,
            require_approved=True,
        )
        header = (
            f"Routed deterministically -> {decision.ritual_id} "
            f"(score {decision.score:.1f}; {'; '.join(decision.reasons)})"
        )
        reply = (
            f"{header}\n\n{result.get('note') or '(no note produced)'}\n\n"
            f"Full run: session {result.get('session_id')}"
        )
        ledger.append_chat_message(
            thread_id,
            role="assistant",
            content=reply,
            kind="routed_run",
            metadata={
                "router": decision.public(),
                "ritual_id": decision.ritual_id,
                "runner": runner_name,
                "run_session_id": result.get("session_id"),
                "stub": stub,
            },
        )
        return {
            "status": "ok",
            "thread_id": thread_id,
            "routed": True,
            "ritual_id": decision.ritual_id,
            "runner": runner_name,
            "session_id": result.get("session_id"),
            "note_path": result.get("note_path"),
        }
    except Exception as exc:
        try:
            ledger.append_chat_message(
                thread_id,
                role="system",
                content=f"Deterministic run of {decision.ritual_id} failed: {exc}",
                kind="error",
                metadata={"router": decision.public()},
            )
        except Exception:  # noqa: BLE001 — never mask the original failure
            pass
        raise
