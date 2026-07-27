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

# Generic @token finder — used with a dynamic lookup so studio-composed agents
# (and room-roster agents) can be called without rebuilding MENTION_RE.
_AT_TOKEN_RE = re.compile(r"(?<!\w)@([A-Za-z0-9][\w-]{0,80})\b")


def _tokens_for_personality(personality: FriendPersonality) -> Tuple[str, ...]:
    tokens = list(_all_mention_tokens(personality))
    compact = "@" + re.sub(r"[^A-Za-z0-9_-]+", "", personality.name or "")
    if compact != "@" and compact.casefold() not in {t.casefold() for t in tokens}:
        tokens.append(compact)
    # Users often type the roster id shown in the team chips (e.g. @agent_research_associate).
    aid = str(personality.id or "").strip()
    if aid:
        id_token = aid if aid.startswith("@") else f"@{aid}"
        if id_token.casefold() not in {t.casefold() for t in tokens}:
            tokens.append(id_token)
    return tuple(tokens)


def build_mention_lookup(
    extra_agent_ids: Optional[Sequence[str]] = None,
) -> dict:
    """Builtin mentions plus optional custom / room-roster agent ids."""
    out = dict(_MENTION_LOOKUP)
    for raw in extra_agent_ids or ():
        key = str(raw or "").strip()
        if not key:
            continue
        personality = PERSONALITIES_BY_ID.get(key) or personality_from_agent_id(key)
        if personality is None:
            continue
        for token in _tokens_for_personality(personality):
            out[token.casefold()] = personality
    return out


def match_personality(
    text: str,
    *,
    extra_agent_ids: Optional[Sequence[str]] = None,
) -> Optional[FriendPersonality]:
    found = mentioned_personalities(text, extra_agent_ids=extra_agent_ids)
    return found[0] if found else None


def mentioned_personalities(
    text: str,
    *,
    extra_agent_ids: Optional[Sequence[str]] = None,
) -> List[FriendPersonality]:
    """Return personalities @-mentioned in text (builtins + optional extras)."""
    lookup = build_mention_lookup(extra_agent_ids)
    found: List[FriendPersonality] = []
    seen = set()
    raw = text or ""
    # Scan with both the static builtin regex and generic @tokens so custom
    # agents work without being baked into MENTION_RE at import time.
    spans: List[Tuple[int, int, FriendPersonality]] = []
    for match in MENTION_RE.finditer(raw):
        personality = lookup.get(match.group(0).casefold()) or _MENTION_LOOKUP.get(
            match.group(0).casefold()
        )
        if personality:
            spans.append((match.start(), match.end(), personality))
    for match in _AT_TOKEN_RE.finditer(raw):
        token = match.group(0).casefold()
        personality = lookup.get(token)
        if personality is None:
            continue
        # Builtins stay on MENTION_RE (spaced-child guards like @Qwen Contrarian).
        # Generic @tokens only unlock studio / roster extras.
        if token in _MENTION_LOOKUP:
            continue
        spans.append((match.start(), match.end(), personality))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    last_end = -1
    for start, end, personality in spans:
        if start < last_end:
            continue
        if personality.id in seen:
            continue
        found.append(personality)
        seen.add(personality.id)
        last_end = end
    return found


def at_mention_tokens(text: str) -> List[str]:
    """Return raw @tokens in text (including @workflow), longest-first unique."""
    seen = set()
    out: List[str] = []
    for match in _AT_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def strip_personality_mentions(
    text: str,
    *,
    extra_agent_ids: Optional[Sequence[str]] = None,
) -> str:
    raw = text or ""
    lookup = build_mention_lookup(extra_agent_ids)

    def _repl(match: re.Match) -> str:
        return "" if match.group(0).casefold() in lookup else match.group(0)

    # Always strip static builtins; also strip any extras in the dynamic lookup.
    cleaned = MENTION_RE.sub("", raw)
    if extra_agent_ids:
        cleaned = _AT_TOKEN_RE.sub(_repl, cleaned)
    return cleaned.strip()


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
