"""Classify a chat message into a ``kind:`` label plus entity/topic.

The stream classifier: instead of carving a continuous chat into sessions, it
labels each message by *what kind of thing it is* and *what it is about*. Two
layers, both privacy-safe:

- **deterministic** — keyword/pattern rules. Fast, no network, high precision.
  Only fires when a single category clearly matches.
- **local Qwen** — for messages the rules can't confidently place. Calls the
  local OpenAI-compatible endpoint (``127.0.0.1:11434``) directly via
  ``synthesize._call_openai_compatible_messages``. Offline-graceful: if Qwen is
  unreachable, the deterministic result stands. Disable with
  ``ANALYST_CLASSIFY_QWEN=off``.

It only *classifies* — it never acts. Output labels:
``kind:<research|build|observation|idea|question>`` plus ``entity:<slug>`` and
``topic:<slug>`` when detectable.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .label_suggest import suggest_labels
from .labels import KINDS, normalize_labels, slugify
from .web_search import extract_tickers

CLASSIFY_QWEN_ENV = "ANALYST_CLASSIFY_QWEN"

# --- deterministic cues (high precision) ---
_RESEARCH_RE = re.compile(
    r"(?<!\w)(?:look\s+into|dig\s+into|research|investigate|read\s+up\s+on|"
    r"find\s+out\s+about|outlook|earnings|filing|filings|10-[qk]|8-k|"
    r"price\s+target|valuation)\b",
    re.I,
)
_BUILD_RE = re.compile(
    r"(?<!\w)(?:build|rebuild|implement|refactor|deploy|ship|wire\s+up|hook\s+up|"
    r"classifier|tagger|dashboard|the\s+sync|companion|gateway|ledger|pipeline|"
    r"schema|migration|endpoint|\bapi\b|\brepo\b|frontend|backend|\bui\b|bug|"
    r"feature|commit|merge|pull\s+request|codebase|\bmodule\b)\b",
    re.I,
)
_OBSERVATION_RE = re.compile(
    r"(?<!\w)(?:seeing|noticed|just\s+saw|broke\s+out|breaking\s+out|spiking|"
    r"spiked|rallying|selling\s+off|sold\s+off|trending|up\s+\d|down\s+\d|"
    r"looks\s+like|seems\s+like)\b",
    re.I,
)
_IDEA_RE = re.compile(
    r"(?<!\w)(?:what\s+if|we\s+could|maybe\s+we\s+should|thinking\s+about|"
    r"brainstorm|would\s+be\s+cool|idea:)\b",
    re.I,
)
_QUESTION_RE = re.compile(
    r"^\s*(?:what|why|how|when|where|which|who|should|could|can|would|is|are|"
    r"do|does|did|will)\b",
    re.I,
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def _qwen_enabled() -> bool:
    return os.environ.get(CLASSIFY_QWEN_ENV, "on").strip().lower() not in {
        "off",
        "0",
        "false",
        "no",
    }


def _deterministic_kind(text: str) -> Optional[str]:
    """Return a kind only when a single non-question category clearly matches."""
    t = text or ""
    hits: List[str] = []
    if _RESEARCH_RE.search(t):
        hits.append("research")
    if _BUILD_RE.search(t):
        hits.append("build")
    if _OBSERVATION_RE.search(t):
        hits.append("observation")
    if _IDEA_RE.search(t):
        hits.append("idea")
    if len(hits) == 1:
        return hits[0]
    if not hits and (t.strip().endswith("?") or _QUESTION_RE.search(t)):
        return "question"
    return None  # ambiguous or unclear -> defer to Qwen


def _extract_entity(text: str) -> str:
    """Best-effort subject: a ticker, a known company alias, or an ask's subject."""
    tickers = extract_tickers(text or "", limit=1)
    if tickers:
        return slugify(tickers[0])
    try:
        from .finance_research import resolve_symbol

        alias = resolve_symbol(text or "", allow_network=False)
        if alias:
            return slugify(alias)
    except Exception:  # noqa: BLE001
        pass
    try:
        from .actionable import detect_actionable

        decision = detect_actionable(text or "")
        if decision.matched and decision.entity:
            return decision.entity
    except Exception:  # noqa: BLE001
        pass
    return ""


def _qwen_kind(
    text: str, examples: Optional[List[Dict[str, Any]]] = None
) -> Optional[str]:
    """Ask the local Qwen model to classify; return a kind or None (graceful).

    ``examples`` are human-confirmed (text -> kind) pairs injected as few-shot
    turns so Qwen adopts the user's own taxonomy over time.
    """
    try:
        from .synthesize import _call_openai_compatible_messages
    except Exception:  # noqa: BLE001
        return None
    system = (
        "Classify the chat message into exactly ONE category and reply with only "
        "that single word, lowercase: research, build, observation, idea, "
        "question, or none.\n"
        "research = a stock/company/topic to look into.\n"
        "build = something to build, fix, or change in our software project.\n"
        "observation = something noticed in markets or the product.\n"
        "idea = a loose suggestion or possibility.\n"
        "question = an open question to resolve.\n"
        "none = small talk / nothing worth tracking."
    )
    messages: List[Dict[str, str]] = []
    for ex in (examples or [])[:15]:
        ex_text = str(ex.get("text") or "").strip()
        ex_kind = str(ex.get("kind") or "").strip()
        if ex_text and ex_kind:
            messages.append({"role": "user", "content": ex_text})
            messages.append({"role": "assistant", "content": ex_kind})
    messages.append({"role": "user", "content": text or ""})
    try:
        reply = _call_openai_compatible_messages(
            messages,
            max_tokens=200,
            system=system,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — Qwen offline / any error -> graceful skip
        return None
    cleaned = _THINK_RE.sub(" ", reply or "").lower()
    found: Optional[str] = None
    for match in re.finditer(
        r"\b(research|build|observation|idea|question|none)\b", cleaned
    ):
        found = match.group(1)  # keep the last one (after any reasoning)
    return found if found in KINDS else None


def classify_message(
    text: str,
    *,
    allow_qwen: bool = True,
    examples: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Classify one message into kind/entity/topic + normalized labels.

    ``examples`` (human-confirmed text->kind pairs) are forwarded to Qwen as
    few-shot examples so it learns the user's taxonomy over time.
    """
    kind = _deterministic_kind(text)
    source = "deterministic" if kind else "none"
    if kind is None and allow_qwen and _qwen_enabled():
        qk = _qwen_kind(text, examples=examples)
        if qk:
            kind, source = qk, "qwen"

    entity = _extract_entity(text)
    topics = suggest_labels(text)

    raw: List[str] = []
    if kind:
        raw.append(f"kind:{kind}")
    if entity:
        raw.append(f"entity:{entity}")
    raw.extend(topics)
    labels = normalize_labels(raw) if raw else []
    return {
        "kind": kind,
        "entity": entity or None,
        "topics": topics,
        "labels": labels,
        "source": source,
    }


def classify_pending(
    ledger: Any, *, limit: int = 20, session_id: Optional[str] = None, scan: int = 500
) -> Dict[str, Any]:
    """Background/batch pass: classify captured *user* chat messages that don't
    yet carry a ``kind:`` label (using Qwen for the fuzzy ones). Records a
    ``label`` event tied to each message via ``target_event_id``. Idempotent:
    messages that already have a kind are skipped, so it is safe to re-run.
    """
    events = ledger.list_events(session_id=session_id, limit=scan)
    have_kind: set = set()
    for ev in events:
        if ev.get("type") != "label":
            continue
        payload = ev.get("payload") or {}
        target = payload.get("target_event_id")
        if target and any(
            str(lbl).startswith("kind:") for lbl in (payload.get("labels") or [])
        ):
            have_kind.add(target)

    try:
        examples = ledger.confirmed_kind_examples(limit=15)
    except Exception:  # noqa: BLE001
        examples = None

    classified = 0
    for ev in events:
        if classified >= limit:
            break
        if ev.get("type") != "chat_message":
            continue
        payload = ev.get("payload") or {}
        if payload.get("role") != "user":
            continue
        if ev.get("event_id") in have_kind:
            continue
        body = str(payload.get("content") or "")
        if not body.strip():
            continue
        result = classify_message(body, allow_qwen=True, examples=examples)
        if not (result.get("kind") and result.get("labels")):
            continue
        try:
            ledger.record_ask_labels(
                ev.get("session_id"),
                result["labels"],
                source="classify_sweep",
                meta={
                    "classification": {
                        "kind": result["kind"],
                        "entity": result["entity"],
                        "source": result["source"],
                    },
                    "target_event_id": ev.get("event_id"),
                },
            )
            classified += 1
        except Exception:  # noqa: BLE001
            pass
    return {"classified": classified}
