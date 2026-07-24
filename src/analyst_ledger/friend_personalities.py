"""Named specialist personalities for People / specialist rooms.

SoR is ``analyst_ledger.registry.Agent``. Builtin personalities are adapted at
import time; custom studio agents resolve dynamically via ``get_agent``.
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
    role: str = "analyst"
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    legacy_names: Tuple[str, ...] = field(default_factory=tuple)


def personality_from_agent_id(agent_id: str) -> Optional[FriendPersonality]:
    agent = get_agent(agent_id)
    if agent is None or not (agent.prompt or "").strip():
        return None
    return FriendPersonality(
        id=agent.id,
        name=agent.name,
        mention=agent.mention if agent.mention.startswith("@") else f"@{agent.name.replace(' ', '')}",
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
        # Builtins only at import — user agents resolve dynamically.
        if agent.id.startswith("agent_") or agent.id.startswith("lens_"):
            continue
        p = personality_from_agent_id(agent.id)
        if p is not None:
            out.append(p)
    return tuple(out)


PERSONALITIES = _build_personalities()
PERSONALITIES_BY_ID = {personality.id: personality for personality in PERSONALITIES}
DEFAULT_PERSONALITY = PERSONALITIES_BY_ID["qwen"]


def _all_mention_tokens(personality: FriendPersonality) -> Tuple[str, ...]:
    return (personality.mention,) + tuple(personality.aliases)


def _mention_lookup() -> dict:
    out = {}
    for personality in PERSONALITIES:
        for token in _all_mention_tokens(personality):
            out[token.casefold()] = personality
    return out


_MENTION_LOOKUP = _mention_lookup()
_MENTION_TOKENS = tuple(sorted(_MENTION_LOOKUP.keys(), key=len, reverse=True))


def _mention_pattern(token: str) -> str:
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
    match = MENTION_RE.search(text or "")
    if not match:
        return None
    return _MENTION_LOOKUP.get(match.group(0).casefold())


def mentioned_personalities(text: str) -> List[FriendPersonality]:
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
    """Resolve specialist ids from registry (builtins + custom studio agents)."""
    if not ids:
        ids = ["qwen-bull", "qwen-contrarian", "qwen-synthesizer"]
    out: List[FriendPersonality] = []
    seen = set()
    for raw in ids:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        personality = PERSONALITIES_BY_ID.get(key) or personality_from_agent_id(key)
        if personality:
            out.append(personality)
            seen.add(personality.id)
    return out


def author_names_for(personality: FriendPersonality) -> set:
    return {personality.name, *personality.legacy_names}


def all_agent_author_names() -> set:
    names: set = set()
    for personality in PERSONALITIES:
        names |= author_names_for(personality)
    for agent in list_agents():
        if agent.room_palette:
            names.add(agent.name)
            names |= set(agent.legacy_names)
    return names


def specialists_public() -> List[dict]:
    return list_room_palette_public()
