"""Named Qwen personalities available in the Friend messenger room."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class FriendPersonality:
    id: str
    name: str
    mention: str
    cookie_key: str
    prompt: str


PERSONALITIES = (
    FriendPersonality(
        id="qwen-contrarian",
        name="Qwen Contrarian",
        mention="@Qwen-Contrarian",
        cookie_key="qwen-contrarian",
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
        id="qwen",
        name="Qwen",
        mention="@Qwen",
        cookie_key="qwen",
        prompt=(
            "Your role is an evidence-led balanced analyst. Answer directly, separate "
            "verified facts from inference, present the strongest credible bull and "
            "bear considerations, and name the evidence needed to resolve uncertainty. "
            "Correct stale premises explicitly. Never invent a fact, date, number, "
            "source, product, or company initiative."
        ),
    ),
)

PERSONALITIES_BY_ID = {personality.id: personality for personality in PERSONALITIES}
DEFAULT_PERSONALITY = PERSONALITIES_BY_ID["qwen"]

# Longest mentions must come first so "@Qwen-Contrarian" never resolves as "@Qwen".
def _mention_pattern(personality: FriendPersonality) -> str:
    """Build a mention pattern, rejecting spaced forms of dashed child names."""
    child_suffixes = [
        other.mention[len(personality.mention) + 1 :]
        for other in PERSONALITIES
        if other.mention.casefold().startswith(
            (personality.mention + "-").casefold()
        )
    ]
    spaced_child_guard = (
        r"(?!\s+(?:" + "|".join(re.escape(s) for s in child_suffixes) + r")\b)"
        if child_suffixes
        else ""
    )
    return re.escape(personality.mention) + spaced_child_guard


MENTION_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(_mention_pattern(p) for p in PERSONALITIES)
    + r")(?![\w-])",
    re.IGNORECASE,
)


def match_personality(text: str) -> Optional[FriendPersonality]:
    """Return the first named personality mentioned in ``text``."""
    match = MENTION_RE.search(text or "")
    if not match:
        return None
    value = match.group(0).casefold()
    for personality in PERSONALITIES:
        if value == personality.mention.casefold():
            return personality
    return None


def mentioned_personalities(text: str) -> List[FriendPersonality]:
    """Return each personality mentioned in text, in message order."""
    found: List[FriendPersonality] = []
    seen = set()
    for match in MENTION_RE.finditer(text or ""):
        value = match.group(0).casefold()
        personality = next(
            (p for p in PERSONALITIES if p.mention.casefold() == value), None
        )
        if personality and personality.id not in seen:
            seen.add(personality.id)
            found.append(personality)
    return found


def strip_personality_mentions(text: str) -> str:
    return MENTION_RE.sub(" ", text or "")
