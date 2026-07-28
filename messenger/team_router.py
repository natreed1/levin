"""Deterministic reverse-TF router for People/team rooms.

Splits simple asks (agent or approved automation) from complex/discussion work
that should go to the frontier Orchestrator. Pure scoring — no model calls.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from analyst_ledger.router import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    _env_float,
    _tokens,
    route_message,
    router_enabled,
)

COMPLEX_ENV = "ANALYST_TEAM_COMPLEX"
THRESHOLD_ENV = "ANALYST_TEAM_ROUTER_THRESHOLD"
MARGIN_ENV = "ANALYST_TEAM_ROUTER_MARGIN"

_COMPLEX_VERBS = frozenset(
    {
        "debate",
        "discuss",
        "organize",
        "organise",
        "orchestrate",
        "roadmap",
        "then",
        "both",
        "versus",
        "vs",
        "bull",
        "bear",
        "contrarian",
        "critique",
        "team",
        "round",
        "rounds",
        "follow",
        "followup",
        "questions",
    }
)
_COMPLEX_PHRASES = (
    "research then",
    "then discuss",
    "bull and bear",
    "bullish and",
    "what do bull",
    "what do bear",
    "organize the room",
    "organise the room",
    "draw an org",
    "set up a flow",
    "loop until",
)
_ORCH_MENTIONS = frozenset({"@orchestrator", "@orch", "@boss"})
_TOKEN_RE = re.compile(r"(?<!\w)@[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass
class AgentRouteHit:
    agent_id: str
    name: str
    stage: str
    score: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class TeamRouteDecision:
    """Outcome of entry routing for one room message."""

    path: str  # orchestrator | agent | automation | clarify | mention
    complex: bool = False
    score: float = 0.0
    runner_up_score: float = 0.0
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    ritual_id: Optional[str] = None
    runner: Optional[str] = None
    watchlist_override: Optional[List[str]] = None
    reasons: List[str] = field(default_factory=list)
    mentioned_ids: List[str] = field(default_factory=list)
    chip: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "complex": self.complex,
            "score": round(self.score, 2),
            "runner_up_score": round(self.runner_up_score, 2),
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "ritual_id": self.ritual_id,
            "runner": self.runner,
            "watchlist_override": (
                list(self.watchlist_override) if self.watchlist_override else None
            ),
            "reasons": list(self.reasons),
            "mentioned_ids": list(self.mentioned_ids),
            "chip": self.chip,
        }


def team_router_enabled() -> bool:
    raw = os.environ.get(COMPLEX_ENV, "on").strip().lower()
    if raw in {"off", "0", "false", "no"}:
        return False
    return router_enabled()


def _at_tokens(text: str) -> List[str]:
    return [m.group(0) for m in _TOKEN_RE.finditer(text or "")]


def _is_complex_message(
    text: str,
    *,
    mentioned_ids: Sequence[str],
    mentioned_stages: Sequence[str],
) -> bool:
    """Deterministic pre-check: complex / discussion → Orchestrator."""
    raw = text or ""
    low = raw.casefold()
    tokens = set(_tokens(raw))
    at = [t.casefold() for t in _at_tokens(raw)]

    if any(t in _ORCH_MENTIONS for t in at):
        return True
    if not at and any(p in low for p in _COMPLEX_PHRASES):
        return True
    if any(p in low for p in _COMPLEX_PHRASES):
        return True
    if len(mentioned_ids) >= 2:
        stages = {s for s in mentioned_stages if s}
        if len(stages) >= 2 or "critique" in stages and "research" in stages:
            return True
        if len(mentioned_ids) >= 2:
            return True
    # Discussion verbs + multi-step cues without a single @agent
    verb_hits = tokens & _COMPLEX_VERBS
    if len(verb_hits) >= 2 and len(mentioned_ids) != 1:
        return True
    if "then" in tokens and ("research" in tokens or "discuss" in tokens or "debate" in tokens):
        return True
    return False


def score_agent(
    text: str,
    agent: Any,
    *,
    counts: Optional[Counter] = None,
) -> Tuple[float, List[str]]:
    """Reverse-TF score of a message against one labeled agent."""
    counts = counts if counts is not None else Counter(_tokens(text))
    if not counts:
        return 0.0, []
    label_tokens = set(agent.label_tokens())
    if not label_tokens:
        return 0.0, []
    score = 0.0
    reasons: List[str] = []
    hits: List[str] = []
    for tok, n in counts.items():
        if tok in label_tokens:
            gained = min(2.0 * n, 4.0)
            score += gained
            hits.append(tok)
    if hits:
        reasons.append(f"labels {', '.join(hits[:6])} (+{score:g})")
    # Mention boost
    mention = (getattr(agent, "mention", "") or "").casefold()
    aliases = [a.casefold() for a in (getattr(agent, "aliases", ()) or ())]
    for at in _at_tokens(text):
        if at.casefold() == mention or at.casefold() in aliases:
            score += 6.0
            reasons.append(f"explicit {at} (+6)")
            break
        # bare id mention @agent_foo / @qwen
        aid = getattr(agent, "id", "")
        if at.casefold() in {f"@{aid}".casefold(), f"@{aid.replace('-', '')}".casefold()}:
            score += 6.0
            reasons.append(f"explicit {at} (+6)")
            break
    # Stage / skill phrase boosts
    low = (text or "").casefold()
    for skill in getattr(agent, "skills", ()) or ():
        s = str(skill).casefold()
        if len(s) >= 4 and s in low:
            score += 2.0
            reasons.append(f"skill {skill!r} (+2)")
    return score, reasons


def rank_agents(
    text: str,
    agents: Sequence[Any],
) -> List[AgentRouteHit]:
    counts = Counter(_tokens(text))
    hits: List[AgentRouteHit] = []
    for agent in agents:
        if getattr(agent, "id", "") == "master":
            continue
        if not getattr(agent, "room_palette", True) and getattr(agent, "id", "") != "orchestrator":
            # Still allow orchestrator even if filtered elsewhere
            pass
        score, reasons = score_agent(text, agent, counts=counts)
        if score <= 0:
            continue
        hits.append(
            AgentRouteHit(
                agent_id=agent.id,
                name=getattr(agent, "name", agent.id),
                stage=getattr(agent, "stage", "") or "",
                score=score,
                reasons=reasons,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.agent_id))
    return hits


def route_team_message(
    text: str,
    *,
    roster_agents: Sequence[Any],
    mentioned_ids: Optional[Sequence[str]] = None,
    mentioned_stages: Optional[Sequence[str]] = None,
    allow_automations: bool = True,
) -> TeamRouteDecision:
    """Entry routing: complex → orchestrator; simple → TF agent/automation."""
    mentioned_ids = list(mentioned_ids or [])
    mentioned_stages = list(mentioned_stages or [])
    if not mentioned_ids and not mentioned_stages:
        # Derive from roster + @tokens when caller didn't resolve yet
        at = [t.casefold() for t in _at_tokens(text)]
        for agent in roster_agents:
            mention = (getattr(agent, "mention", "") or "").casefold()
            aliases = [a.casefold() for a in (getattr(agent, "aliases", ()) or ())]
            tokens = {mention, *aliases, f"@{getattr(agent, 'id', '')}".casefold()}
            if any(t in tokens for t in at):
                mentioned_ids.append(agent.id)
                mentioned_stages.append(getattr(agent, "stage", "") or "")

    complex_msg = _is_complex_message(
        text, mentioned_ids=mentioned_ids, mentioned_stages=mentioned_stages
    )
    if complex_msg:
        return TeamRouteDecision(
            path="orchestrator",
            complex=True,
            mentioned_ids=mentioned_ids,
            reasons=["complex_or_discussion"],
            chip="Orchestrator",
        )

    # Explicit single @agent (and not orchestrator) → mention path / that agent
    if len(mentioned_ids) == 1 and mentioned_ids[0] != "orchestrator":
        agent = next((a for a in roster_agents if a.id == mentioned_ids[0]), None)
        name = getattr(agent, "name", mentioned_ids[0]) if agent else mentioned_ids[0]
        return TeamRouteDecision(
            path="mention",
            complex=False,
            agent_id=mentioned_ids[0],
            agent_name=name,
            score=6.0,
            mentioned_ids=mentioned_ids,
            reasons=["explicit_single_mention"],
            chip=f"@{name}" if not str(name).startswith("@") else str(name),
        )

    threshold = _env_float(THRESHOLD_ENV, DEFAULT_THRESHOLD)
    margin = _env_float(MARGIN_ENV, DEFAULT_MARGIN)

    auto_decision = None
    if allow_automations and team_router_enabled():
        try:
            auto_decision = route_message(text)
        except Exception:
            auto_decision = None

    agent_hits = rank_agents(text, roster_agents)
    # Exclude orchestrator from simple TF targets
    agent_hits = [h for h in agent_hits if h.agent_id != "orchestrator"]
    top_agent = agent_hits[0] if agent_hits else None
    agent_runner_up = agent_hits[1].score if len(agent_hits) > 1 else 0.0

    auto_score = float(auto_decision.score) if auto_decision and auto_decision.matched else 0.0
    agent_score = float(top_agent.score) if top_agent else 0.0

    # Prefer automation when it wins with confidence
    if (
        auto_decision
        and auto_decision.matched
        and auto_score >= threshold
        and auto_score >= agent_score
        and (auto_score - max(agent_score, float(auto_decision.runner_up_score))) >= 0
    ):
        return TeamRouteDecision(
            path="automation",
            complex=False,
            score=auto_score,
            runner_up_score=max(agent_score, float(auto_decision.runner_up_score)),
            ritual_id=auto_decision.ritual_id,
            runner=auto_decision.runner,
            watchlist_override=auto_decision.watchlist_override,
            reasons=list(auto_decision.reasons),
            mentioned_ids=mentioned_ids,
            chip=f"TF→{auto_decision.ritual_id}",
        )

    if top_agent and agent_score >= threshold and (agent_score - agent_runner_up) >= margin:
        return TeamRouteDecision(
            path="agent",
            complex=False,
            score=agent_score,
            runner_up_score=agent_runner_up,
            agent_id=top_agent.agent_id,
            agent_name=top_agent.name,
            reasons=list(top_agent.reasons),
            mentioned_ids=mentioned_ids,
            chip=f"TF→@{top_agent.name}",
        )

    # Low margin but some signal — soft orchestrator for rostered teams
    if roster_agents and max(agent_score, auto_score) >= max(2.0, threshold * 0.4):
        return TeamRouteDecision(
            path="orchestrator",
            complex=True,
            score=max(agent_score, auto_score),
            runner_up_score=(
                min(agent_score, auto_score)
                if agent_score and auto_score
                else agent_runner_up
            ),
            reasons=["low_margin_soft_orchestrator"],
            mentioned_ids=mentioned_ids,
            chip="Orchestrator",
        )

    return TeamRouteDecision(
        path="clarify",
        complex=False,
        score=max(agent_score, auto_score),
        reasons=["no_confident_match"],
        mentioned_ids=mentioned_ids,
        chip="clarify",
    )
