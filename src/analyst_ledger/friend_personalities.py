"""Named specialist personalities for People / specialist rooms.

SoR is ``analyst_ledger.registry.Agent``. This module adapts registry agents
into ``FriendPersonality`` for mention matching and room orchestration.
Which model runs is chosen in Settings or per-room via the model toggle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .registry import get_agent, list_agents, list_room_palette_public


@dataclass(frozen=True)
class FriendPersonality:
    id: str
    name: str
    mention: str
    cookie_key: str
    prompt: str
    role: str = "analyst"  # analyst | bull | bear | synthesizer
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    legacy_names: Tuple[str, ...] = field(default_factory=tuple)


def _personality_from_agent(agent_id: str) -> Optional[FriendPersonality]:
    agent = get_agent(agent_id)
    if agent is None or not agent.prompt:
        return None
    return FriendPersonality(
        id=agent.id,
        name=agent.name,
        mention=agent.mention,
        cookie_key=agent.cookie_key or agent.id,
        prompt=agent.prompt,
        role=agent.role,
        aliases=tuple(agent.aliases),
        legacy_names=tuple(agent.legacy_names),
    )


def _build_personalities() -> Tuple[FriendPersonality, ...]:
    out: List[FriendPersonality] = []
    for agent in list_agents():
        if not agent.room_palette or not agent.prompt:
            continue
        p = _personality_from_agent(agent.id)
        if p is not None:
            out.append(p)
    return tuple(out)


# Stable public order (tests / UI). Mention matching uses longest-first separately.
PERSONALITIES = _build_personalities()
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
    """Room palette — same registry SoR as the Agents tab."""
    return list_room_palette_public()
