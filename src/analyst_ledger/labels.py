"""Controlled + open vocabulary for the ``labels`` tagging axis.

A *label* is an ``axis:value`` string. Labels are a NEW tagging axis that sits
alongside — and never overloads — the existing axes:

- ``sensitivity`` (egress: public<internal<confidential<restricted)
- ``surface`` (source/provenance)
- outcome ``tags`` (idea/followup/reject/neutral — session disposition)
- ``desk_tag`` (subsystem routing: chat:* / arena:* / routine)

Labels live on sessions (``topic``, ``project``) and on chat messages
(``intent``, ``entity``, ``state``). Design signed off 2026-07-20.

Two kinds of axis:

* **Controlled** — reject unknown values, exactly like ``SESSION_TAGS``:
  ``topic``, ``intent``, ``state``. The value must already be in the vocabulary.
* **Open** — only normalise the value to a slug (the set is unbounded):
  ``entity`` (startups / people / products) and ``project``. A project's title
  and status live in the personal project registry; tagging does not
  hard-require prior registration (kept soft so overnight capture never errors).

To grow the shared theme vocabulary, add a slug to ``TOPICS`` below — that list
is the single source of truth both partners edit and commit.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

# All recognised label axes.
AXES = ("topic", "entity", "project", "intent", "state", "kind")

# --- Controlled vocabularies (edit TOPICS to grow the shared theme list) ---
TOPICS = frozenset(
    {
        "ai-startups",
        "ai-models",
        "ai-capex",
        "semiconductors",
        "cloud",
        "cybersecurity",
        "rate-cuts",
        "inflation",
        "macro",
        "earnings",
        "energy",
        "crypto",
        "biotech",
        "regulation",
        "m-and-a",
        "ipos",
        "consumer",
    }
)
INTENTS = frozenset({"research", "monitor", "summarize", "compare", "watch"})
STATES = frozenset({"open", "done", "blocked"})
# What KIND of thing a chat message is — the stream classifier's primary axis.
KINDS = frozenset({"research", "build", "observation", "idea", "question"})

_CONTROLLED: Dict[str, frozenset] = {
    "topic": TOPICS,
    "intent": INTENTS,
    "state": STATES,
    "kind": KINDS,
}
_OPEN = frozenset({"entity", "project"})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class LabelError(ValueError):
    """Raised when a label is malformed or uses an unknown controlled value."""


def slugify(value: str) -> str:
    """Lowercase, collapse anything non-alphanumeric to single hyphens."""
    return _SLUG_RE.sub("-", str(value).strip().lower()).strip("-")


def normalize_label(raw: str) -> str:
    """Return the canonical ``axis:value`` form, or raise :class:`LabelError`.

    Controlled axes (topic/intent/state) reject values outside their vocabulary.
    Open axes (entity/project) accept any non-empty slug.
    """
    if not raw or ":" not in str(raw):
        raise LabelError(f"label must be 'axis:value', got {raw!r}")
    axis, _, value = str(raw).partition(":")
    axis = axis.strip().lower()
    if axis not in AXES:
        raise LabelError(
            f"unknown label axis {axis!r}; expected one of: {', '.join(AXES)}"
        )
    value = slugify(value)
    if not value:
        raise LabelError(f"label {raw!r} has an empty value")
    allowed = _CONTROLLED.get(axis)
    if allowed is not None and value not in allowed:
        raise LabelError(
            f"unknown {axis} '{value}'. Add it to labels.py, or use one of: "
            f"{', '.join(sorted(allowed))}"
        )
    return f"{axis}:{value}"


def normalize_labels(labels: Iterable[str]) -> List[str]:
    """Normalise, de-duplicate and sort a collection of labels."""
    out: List[str] = []
    for raw in labels or []:
        norm = normalize_label(raw)
        if norm not in out:
            out.append(norm)
    return sorted(out)


def labels_by_axis(labels: Iterable[str]) -> Dict[str, List[str]]:
    """Group ``axis:value`` labels into ``{axis: [values]}`` (no validation)."""
    grouped: Dict[str, List[str]] = {}
    for raw in labels or []:
        axis, _, value = str(raw).partition(":")
        grouped.setdefault(axis.strip().lower(), []).append(value)
    return grouped


def is_valid_label(raw: str) -> bool:
    """True if ``raw`` normalises cleanly (never raises)."""
    try:
        normalize_label(raw)
        return True
    except LabelError:
        return False
