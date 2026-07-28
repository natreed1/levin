"""Team Graph recipes — known-good pipelines Master can match and apply.

Recipes are ordered steps with role slots. Runtime allocates layer ids, hires
agents for slots, and writes room.config.graph. Master should prefer
apply_recipe over inventing free-form graphs.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

RECIPES: Dict[str, Dict[str, Any]] = {
    "equity_research": {
        "id": "equity_research",
        "name": "Equity research & trade ideas",
        "summary": "Bull case → contrarian → synthesis for equities / trade ideas.",
        "match": (
            "equity",
            "equities",
            "stock",
            "stocks",
            "ticker",
            "trade idea",
            "trade ideas",
            "bullish",
            "contrarian",
            "long/short",
            "research equit",
            "equity research",
        ),
        "title": "Equity Research & Trade Ideas",
        "objective": (
            "Produce balanced equity research and grounded trade ideas: "
            "evidence-backed bull case, honest contrarian pushback, then a synthesis."
        ),
        "orchestrator": "workflow",
        "skills": ["web_research", "sec_filings_check", "note_digest"],
        "agents": [
            {
                "role": "bullish",
                "name": "Bullish Researcher",
                "capability_ids": ["web_research", "sec_filings_check"],
                "prompt": (
                    "You build the bull case for an equity or theme. Cite concrete "
                    "filings, product, or market evidence. Separate facts from inference. "
                    "Never invent numbers or recommend a trade size."
                ),
                "summary": "Builds evidence-backed bull cases",
                "stage": "research",
            },
            {
                "role": "contrarian",
                "name": "Contrarian Researcher",
                "capability_ids": ["web_research", "sec_filings_check"],
                "prompt": (
                    "You stress-test the bull case. Surface risks, competitive threats, "
                    "accounting red flags, and base-rate doubts. Be specific; no vague FUD."
                ),
                "summary": "Contrarian risk and bear case",
                "stage": "critique",
            },
            {
                "role": "synthesizer",
                "name": "Trade Synthesizer",
                "capability_ids": ["note_digest", "web_research"],
                "prompt": (
                    "You synthesize bull and bear into a balanced takeaway: what would "
                    "need to be true, key uncertainties, and optional idea framing — "
                    "not a hard buy/sell. Preserve disagreement."
                ),
                "summary": "Balances research into trade-idea framing",
                "stage": "synthesize",
            },
        ],
        "steps": [
            {
                "key": "bull_case",
                "title": "Bull case",
                "role": "bullish",
                "goal": "Evidence-backed upside thesis",
                "prompt": "Develop the bull case with cited evidence.",
                "member_instructions": "Focus on catalysts and verifiable signals.",
            },
            {
                "key": "bear_case",
                "title": "Contrarian case",
                "role": "contrarian",
                "goal": "Material risks and falsifiers",
                "prompt": "Challenge the bull case with concrete risks.",
                "member_instructions": "Prioritize falsifiers over generic caveats.",
            },
            {
                "key": "synthesize",
                "title": "Synthesize",
                "role": "synthesizer",
                "goal": "Balanced takeaway / idea framing",
                "prompt": "Merge both sides into a cautious synthesis.",
                "member_instructions": "State what must be true; do not invent a price target.",
            },
        ],
        "guards": [
            {
                "from": "bull_case",
                "to": "bear_case",
                "prompt": (
                    "Did the bull case cite at least one concrete evidence source "
                    "(filing, product, or market fact)? Reply YES or NO."
                ),
            },
            {
                "from": "bear_case",
                "to": "synthesize",
                "prompt": (
                    "Did the contrarian name at least one specific risk or falsifier? "
                    "Reply YES or NO."
                ),
            },
        ],
    },
    "pr_security": {
        "id": "pr_security",
        "name": "PR review + security",
        "summary": "Diff review then security critique for GitHub PRs.",
        "match": (
            "github",
            "pull request",
            "pr review",
            "coding room",
            "code review",
            "exploit",
            "security review",
            "diff review",
        ),
        "title": "Coding review",
        "objective": (
            "Review pull request changes: summarize risk and missing tests, then "
            "run a security pass for auth gaps, injection, and secret leakage."
        ),
        "orchestrator": "workflow",
        "skills": ["web_research", "note_digest"],
        "agents": [
            {
                "role": "diff",
                "name": "Diff Reviewer",
                "capability_ids": ["web_research", "note_digest"],
                "prompt": (
                    "You review pull request changes: list files, summarize risk, "
                    "and call out missing tests. Never invent diff contents."
                ),
                "summary": "Lists and critiques GitHub PR changes",
                "stage": "critique",
            },
            {
                "role": "security",
                "name": "Security Critic",
                "capability_ids": ["web_research"],
                "prompt": (
                    "You hunt for security issues in proposed changes: auth gaps, "
                    "injection, secret leakage, unsafe defaults. Be concrete."
                ),
                "summary": "Security pass over diffs",
                "stage": "critique",
            },
        ],
        "steps": [
            {
                "key": "diff_review",
                "title": "Diff review",
                "role": "diff",
                "goal": "Change summary and test gaps",
                "prompt": "Review the PR diff risk and missing tests.",
                "member_instructions": "Stick to files/changes provided; do not invent diffs.",
            },
            {
                "key": "security_pass",
                "title": "Security pass",
                "role": "security",
                "goal": "Concrete security findings",
                "prompt": "Security-critique the proposed changes.",
                "member_instructions": "Prioritize exploitable issues over style nits.",
            },
        ],
        "guards": [
            {
                "from": "diff_review",
                "to": "security_pass",
                "prompt": (
                    "Did the diff review mention specific files or change areas? "
                    "Reply YES or NO."
                ),
            }
        ],
        "workspace_needs": [
            {
                "id": "github_repo",
                "kind": "github_repo",
                "label": "GitHub repository URL",
                "hint": "e.g. https://github.com/org/repo",
            },
            {
                "id": "github_token",
                "kind": "github_token",
                "label": "GitHub token",
                "hint": "Repo read access for PR diffs",
            },
        ],
        "workspace_notes": (
            "Fill repo URL + GitHub token in Room settings before review agents run."
        ),
    },
    "filings_digest": {
        "id": "filings_digest",
        "name": "SEC filings digest",
        "summary": "Scout filings then digest material changes.",
        "match": (
            "filing",
            "filings",
            "sec ",
            "10-k",
            "10-q",
            "8-k",
            "watchlist",
            "finance team",
            "finance room",
        ),
        "title": "Finance research",
        "objective": (
            "Scout SEC filings for material changes and produce a concise digest "
            "suitable for morning review."
        ),
        "orchestrator": "workflow",
        "skills": ["sec_filings_check", "note_digest", "web_research"],
        "agents": [
            {
                "role": "scout",
                "name": "Filings Scout",
                "capability_ids": ["sec_filings_check", "web_research"],
                "prompt": "Scout SEC filings and summarize material changes.",
                "summary": "SEC filings scout",
                "stage": "research",
            },
            {
                "role": "digest",
                "name": "Filings Digest",
                "capability_ids": ["note_digest"],
                "prompt": (
                    "Turn filing findings into a short digest: what changed, why it "
                    "might matter, and open questions. No trade recommendations."
                ),
                "summary": "Digests filing notes",
                "stage": "synthesize",
            },
        ],
        "steps": [
            {
                "key": "scout",
                "title": "Scout filings",
                "role": "scout",
                "goal": "Material filing changes",
                "prompt": "Find and summarize material SEC filing changes.",
                "member_instructions": "Prefer primary filing facts over commentary.",
            },
            {
                "key": "digest",
                "title": "Digest",
                "role": "digest",
                "goal": "Short morning-ready digest",
                "prompt": "Compress findings into a digest.",
                "member_instructions": "Keep uncertainty explicit.",
            },
        ],
        "guards": [
            {
                "from": "scout",
                "to": "digest",
                "prompt": (
                    "Did the scout identify at least one filing or clearly state none "
                    "found? Reply YES or NO."
                ),
            }
        ],
        "create_loop": {
            "name": "filings_morning",
            "capability_ids": ["sec_filings_check", "note_digest"],
            "schedule": "0 7 * * 1-5",
        },
    },
    "multi_analyst": {
        "id": "multi_analyst",
        "name": "Multi-analyst debate",
        "summary": "Two analysts then a synthesizer for open research questions.",
        "match": (
            "debate",
            "multi analyst",
            "multi-analyst",
            "research room",
            "research team",
            "analyst team",
        ),
        "title": "Research team",
        "objective": "Investigate the question with two perspectives, then synthesize.",
        "orchestrator": "workflow",
        "skills": ["web_research", "note_digest"],
        "agents": [
            {
                "role": "analyst_a",
                "name": "Primary Analyst",
                "capability_ids": ["web_research"],
                "prompt": (
                    "You are the primary researcher. Gather evidence and state a clear "
                    "working thesis with sources."
                ),
                "summary": "Primary research pass",
                "stage": "research",
            },
            {
                "role": "analyst_b",
                "name": "Challenger Analyst",
                "capability_ids": ["web_research"],
                "prompt": (
                    "You challenge the primary thesis with alternative explanations "
                    "and missing evidence."
                ),
                "summary": "Challenger research pass",
                "stage": "critique",
            },
            {
                "role": "synthesizer",
                "name": "Research Synthesizer",
                "capability_ids": ["note_digest"],
                "prompt": (
                    "Synthesize both analyses into a balanced brief with open questions."
                ),
                "summary": "Synthesizes dual research",
                "stage": "synthesize",
            },
        ],
        "steps": [
            {
                "key": "primary",
                "title": "Primary research",
                "role": "analyst_a",
                "goal": "Working thesis + evidence",
                "prompt": "Research the question and state a thesis.",
                "member_instructions": "Cite sources; mark speculation.",
            },
            {
                "key": "challenge",
                "title": "Challenge",
                "role": "analyst_b",
                "goal": "Counter-evidence and gaps",
                "prompt": "Challenge the primary thesis.",
                "member_instructions": "Focus on material disagreements.",
            },
            {
                "key": "synthesize",
                "title": "Synthesize",
                "role": "synthesizer",
                "goal": "Balanced brief",
                "prompt": "Produce a balanced synthesis.",
                "member_instructions": "Preserve unresolved uncertainty.",
            },
        ],
        "guards": [
            {
                "from": "primary",
                "to": "challenge",
                "prompt": "Did primary research state a clear thesis? Reply YES or NO.",
            },
            {
                "from": "challenge",
                "to": "synthesize",
                "prompt": "Did the challenger raise a specific counterpoint? Reply YES or NO.",
            },
        ],
    },
}


def list_recipes() -> List[Dict[str, Any]]:
    """Public catalog rows for Master / UI."""
    out: List[Dict[str, Any]] = []
    for recipe in RECIPES.values():
        out.append(
            {
                "id": recipe["id"],
                "name": recipe["name"],
                "summary": recipe.get("summary") or "",
                "title": recipe.get("title") or recipe["name"],
                "steps": [
                    {"key": s.get("key"), "title": s.get("title"), "role": s.get("role")}
                    for s in (recipe.get("steps") or [])
                ],
                "roles": [a.get("role") for a in (recipe.get("agents") or [])],
                "skills": list(recipe.get("skills") or []),
                "match_hints": list(recipe.get("match") or [])[:8],
            }
        )
    return out


def get_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    rid = str(recipe_id or "").strip()
    if not rid:
        return None
    return RECIPES.get(rid) or RECIPES.get(rid.casefold())


def match_recipe(message: str) -> Optional[Dict[str, Any]]:
    """Score user text against recipe match hints; return best recipe or None."""
    lower = (message or "").casefold()
    if not lower.strip():
        return None
    scored: List[Tuple[int, str]] = []
    for rid, recipe in RECIPES.items():
        score = 0
        for hint in recipe.get("match") or ():
            h = str(hint).casefold()
            if not h:
                continue
            if h in lower:
                # Longer hints count more (e.g. "equity research" > "stock")
                score += max(2, len(h.split()))
        # Soft boosts for create/setup + domain co-occurrence
        if score and any(w in lower for w in ("create", "set up", "setup", "build", "make")):
            score += 1
        if score and any(w in lower for w in ("room", "team", "graph")):
            score += 1
        if score:
            scored.append((score, rid))
    if not scored:
        # Generic research room without equity/filings/coding cues
        if any(w in lower for w in ("research room", "research team", "analyst team")):
            return get_recipe("multi_analyst")
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, best_id = scored[0]
    # Require a minimum so tiny accidental hits don't fire
    if best_score < 2:
        return None
    # Prefer equity_research when equities + research both present
    if "equit" in lower and "research" in lower:
        eq = get_recipe("equity_research")
        if eq:
            return eq
    return get_recipe(best_id)


def _slug(text: str, *, prefix: str = "layer_") -> str:
    raw = re.sub(r"[^a-z0-9]+", "_", (text or "").casefold()).strip("_")
    return f"{prefix}{(raw or 'step')[:48]}"


def build_graph_from_recipe(
    recipe: Dict[str, Any],
    *,
    role_to_agent: Dict[str, str],
    disable_steps: Optional[Sequence[str]] = None,
    guard_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Materialize layers + guards; members resolve via role_to_agent."""
    disabled = {str(x).strip() for x in (disable_steps or []) if str(x).strip()}
    overrides = guard_overrides if isinstance(guard_overrides, dict) else {}
    layers: List[Dict[str, Any]] = []
    key_to_layer_id: Dict[str, str] = {}
    for step in recipe.get("steps") or []:
        if not isinstance(step, dict):
            continue
        key = str(step.get("key") or "").strip()
        if key and key in disabled:
            continue
        role = str(step.get("role") or "").strip()
        agent_id = role_to_agent.get(role) or ""
        layer_id = _slug(key or str(step.get("title") or "step"))
        if key:
            key_to_layer_id[key] = layer_id
        members = []
        if agent_id:
            members.append(
                {
                    "agent_id": agent_id,
                    "instructions": str(step.get("member_instructions") or "")[:2000],
                }
            )
        layers.append(
            {
                "id": layer_id,
                "title": str(step.get("title") or key or "Step")[:80],
                "prompt": str(step.get("prompt") or "")[:2000],
                "goal": str(step.get("goal") or "")[:800],
                "members": members,
            }
        )
    guards: List[Dict[str, Any]] = []
    for idx, guard in enumerate(recipe.get("guards") or []):
        if not isinstance(guard, dict):
            continue
        from_key = str(guard.get("from") or "").strip()
        to_key = str(guard.get("to") or "").strip()
        from_id = key_to_layer_id.get(from_key)
        to_id = key_to_layer_id.get(to_key)
        if not from_id or not to_id:
            continue
        ov_key = f"{from_key}_to_{to_key}"
        prompt = str(overrides.get(ov_key) or guard.get("prompt") or "")[:2000]
        guards.append(
            {
                "id": f"guard_{idx + 1}",
                "from_layer_id": from_id,
                "to_layer_id": to_id,
                "prompt": prompt,
            }
        )
    return {"layers": layers, "guards": guards}


def summarize_graph(graph: Dict[str, Any]) -> str:
    layers = graph.get("layers") or []
    if not layers:
        return "empty graph"
    parts = []
    for layer in layers:
        title = layer.get("title") or layer.get("id")
        n = len(layer.get("members") or [])
        parts.append(f"{title} ({n} analyst{'s' if n != 1 else ''})")
    return " → ".join(parts)
