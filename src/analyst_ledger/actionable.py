"""Single-shot actionable-request detector for the chat brain.

Recognises an *explicit* research request in a chat message — e.g. Nat saying
"we should look into Acme AI" — and turns it into a proposed actionable ask:
``intent:research`` + ``entity:<slug>`` + ``state:open``.

Pure function: NO model calls, NO network, NO ledger writes. The chat layer
decides what to do with a match (run the research as an unapproved draft).

Design choices (signed off 2026-07-20):
- EXPLICIT asks only — high precision. A small, deliberate trigger set; widen
  later once trusted.
- ONE message is enough (unlike ``chat_mining`` which needs repetition).
- Kill switch: ``ANALYST_CHAT_ACTIONABLE=off`` disables detection entirely.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .labels import normalize_labels, slugify

ACTIONABLE_ENV = "ANALYST_CHAT_ACTIONABLE"

# High-precision request triggers. "check out"/"look up" deliberately excluded
# (too noisy). Verb captured in group 1 for the provenance reason.
_TRIGGER_RE = re.compile(
    r"\b(look(?:ing)?\s+into|dig(?:ging)?\s+into|research(?:ing)?|"
    r"investigate|read\s+up\s+on|find\s+out\s+about)\b",
    re.IGNORECASE,
)

# End the entity capture at a clause boundary (conjunction, subordinator, or
# a trailing purpose/time clause).
_STOP_SPLIT_RE = re.compile(
    r"[.,;:!?\n]|\b(?:and|but|so|because|then|when|whenever|while|once|if|"
    r"after|before|until|unless)\b|\bfor (?:us|me)\b|\bas soon\b|\bto see\b",
    re.IGNORECASE,
)

# Leading words trimmed off the captured subject.
_LEADING_FILLER = {
    "this", "that", "the", "a", "an", "some", "more", "about", "into", "on",
    "of", "startup", "company", "ai", "new", "called", "named", "stock",
    "it", "them", "those", "these", "him", "her", "us",
}
# Trailing words trimmed off the captured subject.
_TRAILING_FILLER = {
    "stock", "company", "startup", "outlook", "news", "situation",
    "thing", "stuff", "space",
}
# If "research" is used as a noun, the preceding word is usually one of these.
_RESEARCH_NOUN_PRECEDERS = {
    "some", "my", "the", "his", "her", "did", "more", "this", "that",
    "of", "our", "their", "a", "no",
}


def actionable_enabled() -> bool:
    raw = os.environ.get(ACTIONABLE_ENV, "on").strip().lower()
    return raw not in {"off", "0", "false", "no"}


@dataclass
class ActionableDecision:
    matched: bool
    intent: str = "research"
    entity_raw: str = ""
    entity: str = ""
    labels: List[str] = field(default_factory=list)
    reason: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "matched": self.matched,
            "intent": self.intent,
            "entity": self.entity,
            "entity_raw": self.entity_raw,
            "labels": list(self.labels),
            "reason": self.reason,
        }


def _clean_entity(tail: str) -> str:
    chunk = _STOP_SPLIT_RE.split(tail, maxsplit=1)[0]
    called = re.search(r"\b(?:called|named)\b\s+(.*)", chunk, re.IGNORECASE)
    if called:
        chunk = called.group(1)
    tokens = chunk.strip().split()
    while tokens and tokens[0].lower().strip(",.'\"") in _LEADING_FILLER:
        tokens.pop(0)
    while tokens and tokens[-1].lower().strip(",.'\"") in _TRAILING_FILLER:
        tokens.pop()
    return " ".join(tokens).strip(" ,.'\"")


def detect_actionable(text: str) -> ActionableDecision:
    """Detect an explicit research ask and extract its subject entity."""
    if not actionable_enabled():
        return ActionableDecision(matched=False)
    match = _TRIGGER_RE.search(text or "")
    if not match:
        return ActionableDecision(matched=False)

    verb = match.group(1).lower()
    if verb.startswith("research"):
        before = (text or "")[: match.start()].strip().split()
        if before and before[-1].lower().strip(",.") in _RESEARCH_NOUN_PRECEDERS:
            return ActionableDecision(matched=False)  # "did some research", noun usage

    entity_raw = _clean_entity((text or "")[match.end():])
    entity = slugify(entity_raw)
    if not entity or len(entity.replace("-", "")) < 2:
        return ActionableDecision(matched=False)  # no concrete subject to research

    labels = normalize_labels(
        [f"entity:{entity}", "intent:research", "state:open"]
    )
    return ActionableDecision(
        matched=True,
        intent="research",
        entity_raw=entity_raw,
        entity=entity,
        labels=labels,
        reason=f"explicit research request ('{verb} ...')",
    )
