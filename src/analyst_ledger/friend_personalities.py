"""Named specialist personalities for People / specialist rooms.

Display names and @mentions are role/prompt identities — not model brands.
Which model runs (Claude, GPT, local open-source) is chosen in Settings or
per-room via the model toggle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FriendPersonality:
    id: str
    name: str
    mention: str
    cookie_key: str
    prompt: str
    role: str = "analyst"  # analyst | bull | bear | synthesizer
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    # Historical message authors still treated as this agent.
    legacy_names: Tuple[str, ...] = field(default_factory=tuple)


_PERSONALITY_DEFS = (
    FriendPersonality(
        id="qwen",
        name="Analyst",
        mention="@Analyst",
        aliases=("@Qwen",),
        legacy_names=("Qwen",),
        cookie_key="qwen",
        role="analyst",
        prompt=(
            "Your role is an evidence-led balanced analyst. Answer directly, separate "
            "verified facts from inference, present the strongest credible bull and "
            "bear considerations, and name the evidence needed to resolve uncertainty. "
            "Correct stale premises explicitly. Never invent a fact, date, number, "
            "source, product, or company initiative."
        ),
    ),
    FriendPersonality(
        id="qwen-bull",
        name="Bullish Agent",
        mention="@Bullish",
        aliases=("@Qwen-Bull", "@Bull"),
        legacy_names=("Qwen Bull",),
        cookie_key="qwen-bull",
        role="bull",
        prompt=(
            "Your role is the constructive bull case specialist. Steelman the upside: "
            "name the specific thesis, the mechanism that creates value, and the "
            "evidence that would confirm it. Acknowledge the strongest bear risk in "
            "one sentence, then return to what would make the bull case right. Never "
            "invent a fact, date, number, source, product, or company initiative."
        ),
    ),
    FriendPersonality(
        id="qwen-contrarian",
        name="Contrarian Agent",
        mention="@Contrarian",
        aliases=("@Qwen-Contrarian",),
        legacy_names=("Qwen Contrarian",),
        cookie_key="qwen-contrarian",
        role="bear",
        prompt=(
            "Your role is the evidence-led contrarian. Identify the specific claim "
            "you are challenging, distinguish verified facts from inference, expose "
            "the strongest overlooked downside or incentive, and give a concrete "
            "falsification test that names evidence capable of disproving your caution. "
            "If the caution is a logical limit of the available evidence rather than "
            "an empirical claim, say it cannot be falsified from that same evidence "
            "and name the additional evidence that would resolve the uncertainty. "
            "Never compare quantities with different units or unrelated periods. Do "
            "not disagree merely for style. Concede claims that survive scrutiny, "
            "state uncertainty, and never invent a fact, date, number, source, "
            "product, or company initiative."
        ),
    ),
    FriendPersonality(
        id="qwen-synthesizer",
        name="Synthesizer Agent",
        mention="@Synthesizer",
        aliases=("@Qwen-Synthesizer",),
        legacy_names=("Qwen Synthesizer",),
        cookie_key="qwen-synthesizer",
        role="synthesizer",
        prompt=(
            "Your role is the synthesizer. After hearing multiple specialist views, "
            "extract shared facts, name the real disagreement, and propose 2–4 "
            "concrete research ideas or next checks that would move the debate. "
            "Prefer falsifiable questions over opinions. Never invent a fact, date, "
            "number, source, product, or company initiative."
        ),
    ),
)

# Stable public order (tests / UI). Mention matching uses longest-first separately.
PERSONALITIES = _PERSONALITY_DEFS
PERSONALITIES_BY_ID = {personality.id: personality for personality in PERSONALITIES}
DEFAULT_PERSONALITY = PERSONALITIES_BY_ID["qwen"]


def _all_mention_tokens(personality: FriendPersonality) -> Tuple[str, ...]:
    return (personality.mention,) + tuple(personality.aliases)


def _mention_lookup() -> dict:
    """Map casefolded mention/alias → personality (longest keys win in matching)."""
    out = {}
    for personality in PERSONALITIES:
        for token in _all_mention_tokens(personality):
            out[token.casefold()] = personality
    return out


_MENTION_LOOKUP = _mention_lookup()
_MENTION_TOKENS = tuple(
    sorted(_MENTION_LOOKUP.keys(), key=len, reverse=True)
)


def _mention_pattern(token: str) -> str:
    """Escape a mention token; guard spaced child forms for dashed prefixes."""
    child_suffixes = [
        other[len(token) + 1 :]
        for other in _MENTION_TOKENS
        if other.casefold().startswith((token + "-").casefold())
        and other.casefold() != token.casefold()
    ]
    canonical = next(t for t in _MENTION_TOKENS if t.casefold() == token.casefold())
    for p in PERSONALITIES:
        for t in _all_mention_tokens(p):
            if t.casefold() == token.casefold():
                canonical = t
                break
    spaced_child_guard = (
        r"(?!\s+(?:" + "|".join(re.escape(s) for s in child_suffixes) + r")\b)"
        if child_suffixes
        else ""
    )
    return re.escape(canonical) + spaced_child_guard


MENTION_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(_mention_pattern(t) for t in _MENTION_TOKENS)
    + r")(?![\w-])",
    re.IGNORECASE,
)


def match_personality(text: str) -> Optional[FriendPersonality]:
    """Return the first named personality mentioned in ``text``."""
    match = MENTION_RE.search(text or "")
    if not match:
        return None
    return _MENTION_LOOKUP.get(match.group(0).casefold())


def mentioned_personalities(text: str) -> List[FriendPersonality]:
    """Return each personality mentioned in text, in message order."""
    found: List[FriendPersonality] = []
    seen = set()
    for match in MENTION_RE.finditer(text or ""):
        personality = _MENTION_LOOKUP.get(match.group(0).casefold())
        if personality and personality.id not in seen:
            found.append(personality)
            seen.add(personality.id)
    return found


def strip_personality_mentions(text: str) -> str:
    return MENTION_RE.sub("", text or "").strip()


def resolve_specialists(ids: Optional[Sequence[str]] = None) -> List[FriendPersonality]:
    """Resolve specialist ids; default to bull + contrarian + synthesizer."""
    if not ids:
        ids = ["qwen-bull", "qwen-contrarian", "qwen-synthesizer"]
    out: List[FriendPersonality] = []
    for raw in ids:
        key = str(raw or "").strip()
        personality = PERSONALITIES_BY_ID.get(key)
        if personality and personality not in out:
            out.append(personality)
    return out


def author_names_for(personality: FriendPersonality) -> set:
    return {personality.name, *personality.legacy_names}


def all_agent_author_names() -> set:
    names: set = set()
    for personality in PERSONALITIES:
        names |= author_names_for(personality)
    return names


def specialists_public() -> List[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "mention": p.mention,
            "aliases": list(p.aliases),
            "legacy_names": list(p.legacy_names),
            "role": p.role,
        }
        for p in PERSONALITIES
    ]
