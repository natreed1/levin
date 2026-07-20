"""Chat mining: deterministic detection of repeated asks and automation gaps.

Reads dashboard chat threads (and, when configured, the Friend messenger room)
and clusters the user's asks. The prime signal is the "automation gap": a user
message answered by the model (kind="synthesis") because no deterministic route
matched. Repeated gaps become draft automation proposals (approved=false,
proposed_by="chat_mining") that the human approves on the /review page.

No model calls here. The only network is the optional Friend fetch, gated by
messenger_configured() and degraded gracefully on any failure. All ask text is
redacted at ingestion. Note: classify_finance_intent checks OUTLOOK before
FILINGS, so "filings guidance" reads as outlook — acceptable imprecision.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

NOISE_KINDS = frozenset({"status", "research_step", "handoff"})
HANDLED_KINDS = frozenset({"routed_run", "file_search"})
MODELED_KINDS = frozenset({"synthesis"})

JACCARD_THRESHOLD = 0.5
MIN_COUNT = 3
MIN_DAYS = 2
MIN_MODELED = 1
MAX_PROPOSALS = 3
MAX_CONTEXT_CLUSTERS = 8
_ASK_TEXT_CAP = 200
_SAMPLE_CAP = 120

_RUNNER_SHORT = {
    "sec_filings_check": "filings",
    "morning_yf_scan": "scan",
    "generic_watchlist_scan": "watch",
    "note_digest": "digest",
}

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


@dataclass
class ChatAsk:
    ts: str
    thread_id: str
    source: str  # "dashboard" | "friend"
    text: str  # redacted, capped
    tokens: FrozenSet[str]
    symbols: Tuple[str, ...]
    intent: str
    outcome: str  # routed | file_search | modeled | unanswered | friend


@dataclass
class AskCluster:
    label: str
    asks: List[ChatAsk] = field(default_factory=list)
    count: int = 0
    day_count: int = 0
    symbols: Tuple[str, ...] = ()
    intent: str = "general"
    modeled_count: int = 0
    routed_count: int = 0
    sources: List[str] = field(default_factory=list)
    sample: str = ""
    top_tokens: List[str] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "count": self.count,
            "day_count": self.day_count,
            "symbols": list(self.symbols),
            "intent": self.intent,
            "modeled_count": self.modeled_count,
            "routed_count": self.routed_count,
            "sources": list(self.sources),
            "sample": self.sample,
            "top_tokens": list(self.top_tokens),
        }


def _within_window(ts: str, cutoff: str) -> bool:
    head = (ts or "")[:19].replace(" ", "T")
    if not _TS_RE.match(head):
        return False
    return head >= cutoff[:19]


def _all_alias_symbols(text: str) -> List[str]:
    """All COMPANY_ALIASES hits (the package helper returns only the first)."""
    from .finance_research import COMPANY_ALIASES

    value = (text or "").casefold()
    out: List[str] = []
    for name, symbol in COMPANY_ALIASES.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", value) and symbol not in out:
            out.append(symbol)
    return out


def _make_ask(ev: Dict[str, Any], *, source: str, thread_id: str, outcome: str) -> ChatAsk:
    from .finance_research import classify_finance_intent
    from .redact import redact_text
    from .router import _tokens
    from .web_search import extract_tickers

    content = str((ev.get("payload") or {}).get("content") or "").strip()
    symbols = [s.upper() for s in extract_tickers(content, limit=2)]
    for alias in _all_alias_symbols(content):
        if alias.upper() not in symbols:
            symbols.append(alias.upper())
    return ChatAsk(
        ts=str(ev.get("ts") or ""),
        thread_id=thread_id,
        source=source,
        text=redact_text(content)[:_ASK_TEXT_CAP],
        tokens=frozenset(_tokens(content)),
        symbols=tuple(sorted(set(symbols))),
        intent=classify_finance_intent(content),
        outcome=outcome,
    )


def _classify_outcome(msgs: List[Dict[str, Any]], start: int) -> str:
    """Scan replies after the ask (until the next user ask). handled > modeled."""
    outcome = "unanswered"
    for j in range(start + 1, len(msgs)):
        payload = msgs[j].get("payload") or {}
        if payload.get("role") == "user" and payload.get("kind") == "message":
            break
        kind = payload.get("kind")
        if kind in HANDLED_KINDS:
            return "routed" if kind == "routed_run" else "file_search"
        if kind in MODELED_KINDS:
            outcome = "modeled"
    return outcome


def gather_chat_asks(
    ledger: Any,
    *,
    days: int = 14,
    max_messages: int = 600,
    include_friend: bool = True,
) -> Tuple[List[ChatAsk], bool]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    asks: List[ChatAsk] = []
    scanned = 0
    for thread in ledger.list_chat_threads():
        if scanned >= max_messages:
            break
        sid = str(thread.get("session_id") or "")
        if not sid or sid == "friend" or thread.get("desk_tag") == "chat:friend":
            continue  # the Friend room is mined via the bridge, not the ledger
        msgs = ledger.list_chat_messages(sid, limit=300)
        scanned += len(msgs)
        for i, ev in enumerate(msgs):
            payload = ev.get("payload") or {}
            if payload.get("role") != "user" or payload.get("kind") != "message":
                continue
            if not str(payload.get("content") or "").strip():
                continue
            if not _within_window(str(ev.get("ts") or ""), cutoff):
                continue
            asks.append(
                _make_ask(
                    ev,
                    source="dashboard",
                    thread_id=sid,
                    outcome=_classify_outcome(msgs, i),
                )
            )

    friend_included = False
    if include_friend:
        from . import messenger_bridge as mb

        if mb.messenger_configured():
            try:
                friend_msgs = mb.list_friend_messages(limit=200)
                friend_included = True
            except (mb.MessengerBridgeError, OSError):
                friend_msgs = []
            for ev in friend_msgs:
                payload = ev.get("payload") or {}
                if payload.get("role") != "user":
                    continue
                if not str(payload.get("content") or "").strip():
                    continue
                if not _within_window(str(ev.get("ts") or ""), cutoff):
                    continue
                asks.append(
                    _make_ask(ev, source="friend", thread_id="friend", outcome="friend")
                )

    asks.sort(key=lambda a: (a.ts, a.thread_id, a.text))
    return asks, friend_included


def _jaccard(a: FrozenSet[str], b: FrozenSet[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _joins(ask: ChatAsk, rep: ChatAsk) -> bool:
    if _jaccard(ask.tokens, rep.tokens) >= JACCARD_THRESHOLD:
        return True
    return bool(ask.symbols) and ask.symbols == rep.symbols and ask.intent == rep.intent


def cluster_asks(asks: List[ChatAsk]) -> List[AskCluster]:
    clusters: List[AskCluster] = []
    for ask in asks:
        placed = False
        for cluster in clusters:
            if _joins(ask, cluster.asks[0]):
                cluster.asks.append(ask)
                placed = True
                break
        if not placed:
            clusters.append(AskCluster(label="", asks=[ask]))

    for cluster in clusters:
        rep = cluster.asks[0]
        cluster.count = len(cluster.asks)
        cluster.day_count = len({a.ts[:10] for a in cluster.asks})
        cluster.modeled_count = sum(1 for a in cluster.asks if a.outcome == "modeled")
        cluster.routed_count = sum(
            1 for a in cluster.asks if a.outcome in {"routed", "file_search"}
        )
        cluster.sources = sorted({a.source for a in cluster.asks})
        cluster.symbols = rep.symbols
        cluster.intent = rep.intent
        cluster.sample = rep.text[:_SAMPLE_CAP]
        freq: Counter = Counter()
        for a in cluster.asks:
            freq.update(a.tokens - {s.casefold() for s in a.symbols})
        cluster.top_tokens = [
            t for t, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:5]
        ]
        # Label head: intent when specific, else the dominant token (the intent
        # regexes miss plurals like "filings", so tokens carry that signal)
        head = (
            cluster.intent
            if cluster.intent != "general"
            else (cluster.top_tokens[0] if cluster.top_tokens else "general")
        )
        if cluster.symbols:
            cluster.label = f"{head}:{'+'.join(cluster.symbols)}"
        else:
            cluster.label = f"{head}:{'+'.join(cluster.top_tokens[:3]) or 'misc'}"

    clusters.sort(key=lambda c: (-c.count, -c.modeled_count, c.label))
    return clusters


def chat_context(
    ledger: Any, *, days: int = 14, include_friend: bool = True
) -> Dict[str, Any]:
    """Compact chat-evidence block for the review context (and Claude prompt)."""
    asks, friend_included = gather_chat_asks(
        ledger, days=days, include_friend=include_friend
    )
    clusters = cluster_asks(asks)
    return {
        "ask_count": len(asks),
        "gap_count": sum(1 for a in asks if a.outcome == "modeled"),
        "routed_count": sum(
            1 for a in asks if a.outcome in {"routed", "file_search"}
        ),
        "unanswered_count": sum(1 for a in asks if a.outcome == "unanswered"),
        "friend_included": friend_included,
        "clusters": [c.public() for c in clusters[:MAX_CONTEXT_CLUSTERS]],
    }


def _guess_runner(cluster: Dict[str, Any]) -> str:
    from .router import RUNNER_KEYWORDS

    intent = str(cluster.get("intent") or "general")
    tokens = set(cluster.get("top_tokens") or []) | set(
        str(cluster.get("label") or "").split(":", 1)[-1].split("+")
    )
    if intent == "filings" or tokens & RUNNER_KEYWORDS["sec_filings_check"]:
        return "sec_filings_check"
    if intent in {"snapshot", "outlook", "news"} or tokens & RUNNER_KEYWORDS[
        "morning_yf_scan"
    ]:
        return "morning_yf_scan"
    if not cluster.get("symbols") and tokens & RUNNER_KEYWORDS["note_digest"]:
        return "note_digest"
    return "generic_watchlist_scan"


def _covered_by_approved(
    cluster: Dict[str, Any], runner: str, automations: List[Dict[str, Any]]
) -> bool:
    symbols = set(cluster.get("symbols") or [])
    for auto in automations:
        if not auto.get("approved") or str(auto.get("runner") or "") != runner:
            continue
        watchlist = {str(s).upper() for s in (auto.get("watchlist") or [])}
        if symbols and symbols <= watchlist:
            return True
        if not symbols:
            return True
    return False


def proposals_from_clusters(
    clusters: List[Dict[str, Any]],
    automations: List[Dict[str, Any]],
    *,
    max_proposals: int = MAX_PROPOSALS,
) -> List[Dict[str, Any]]:
    """Pure: draft-proposal dicts from compact cluster dicts. No I/O."""
    from .rituals import _validate_ritual_id

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for cluster in clusters:
        if len(out) >= max_proposals:
            break
        count = int(cluster.get("count") or 0)
        day_count = int(cluster.get("day_count") or 0)
        modeled = int(cluster.get("modeled_count") or 0)
        if count < MIN_COUNT or day_count < MIN_DAYS or modeled < MIN_MODELED:
            continue
        runner = _guess_runner(cluster)
        if _covered_by_approved(cluster, runner, automations):
            continue
        symbols = [str(s).upper() for s in (cluster.get("symbols") or [])]
        if symbols:
            suffix = "_".join(s.lower() for s in symbols)
        else:
            label = str(cluster.get("label") or "")
            first = label.split(":", 1)[-1].split("+")[0]
            suffix = re.sub(r"[^a-z0-9_-]+", "_", first.casefold()) or "asks"
        rid = f"chat_{_RUNNER_SHORT[runner]}_{suffix}"[:60].rstrip("_-")
        try:
            rid = _validate_ritual_id(rid)
        except ValueError:
            continue
        if rid in seen:
            continue
        seen.add(rid)
        routed = int(cluster.get("routed_count") or 0)
        sample = str(cluster.get("sample") or "")
        out.append(
            {
                "ritual_id": rid,
                "runner": runner,
                "schedule": "0 7 * * 1-5",
                "watchlist": symbols,
                "rationale": (
                    f"asked {count}x over {day_count} day(s) in chat "
                    f"({modeled} handled by the model, {routed} routed); "
                    f'e.g. "{sample}"'
                ),
                "source": "chat_mining",
                "source_label": str(cluster.get("label") or ""),
            }
        )
    return out
