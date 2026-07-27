"""Frontier Orchestrator — one message draws org/flow and executes with progress.

Default assembled pattern when labels fit: Research → Fact-check → Critique(+Q)
→ Research until dry (budgets). No confirm gate.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from messenger import fact_checker, orch_grader, room_kg, task_tiers

logger = logging.getLogger("messenger.team_orchestrator")

MAX_ROUNDS = 3
MAX_QUESTIONS_PER_ROUND = 4
_QUESTION_RE = re.compile(
    r"(?:^\s*(?:[-*]|\d+[.)])?\s*(.+\?)\s*$|(?:question|open(?:ing)?|follow[- ]?up)[:\s]+(.+\?))",
    re.I | re.M,
)


@dataclass
class OrchStage:
    kind: str
    label: str
    agent_ids: List[str] = field(default_factory=list)
    capability_ids: List[str] = field(default_factory=list)
    power: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "agent_ids": list(self.agent_ids),
            "capability_ids": list(self.capability_ids),
            "power": self.power or task_tiers.default_power(self.kind),
            "tier": task_tiers.map_step(
                self.kind, self.power or None
            ).public(),
        }


@dataclass
class OrchPlan:
    stages: List[OrchStage]
    summary: str
    pattern: str = "custom"

    def public(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "pattern": self.pattern,
            "stages": [s.public() for s in self.stages],
        }


def _agents_by_stage(agents: Sequence[Any]) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {}
    for a in agents:
        stage = (getattr(a, "stage", "") or "critique").casefold()
        out.setdefault(stage, []).append(a)
    return out


def draw_org(
    text: str,
    *,
    roster: Sequence[Any],
    room_skills: Optional[Sequence[str]] = None,
    harness: Optional[Dict[str, Any]] = None,
) -> OrchPlan:
    """Invent a short org/flow from labeled agents + room caps/harness."""
    by_stage = _agents_by_stage(roster)
    research = by_stage.get("research") or []
    critique = by_stage.get("critique") or []
    synthesize = by_stage.get("synthesize") or []
    automate = by_stage.get("automate") or []

    skills = [str(s).strip() for s in (room_skills or []) if str(s).strip()]
    if harness and isinstance(harness.get("skills"), list):
        for s in harness["skills"]:
            sid = str(s).strip()
            if sid and sid not in skills:
                skills.append(sid)

    retrieve_caps = [
        s
        for s in skills
        if s
        in {
            "sec_filings_check",
            "morning_yf_scan",
            "generic_watchlist_scan",
            "web_research",
            "note_digest",
            "public_web_search",
            "find_files",
        }
    ]
    # Also pull from research agent capabilities (executable only)
    _non_retrieve = {"classify_message"}
    for a in research:
        for cid in getattr(a, "capabilities", ()) or ():
            if cid in _non_retrieve:
                continue
            if cid not in retrieve_caps and cid in {
                "sec_filings_check",
                "morning_yf_scan",
                "generic_watchlist_scan",
                "web_research",
                "note_digest",
                "public_web_search",
                "find_files",
            }:
                retrieve_caps.append(cid)
            elif cid == "web_research" and cid not in retrieve_caps:
                retrieve_caps.append(cid)

    # Prefer caps that match the ask (filings → sec_filings_check first)
    low = (text or "").casefold()
    if any(k in low for k in ("filing", "filings", "8-k", "10-k", "10-q", "edgar", "sec")):
        if "sec_filings_check" not in retrieve_caps:
            try:
                from analyst_ledger.registry import get_capability

                if get_capability("sec_filings_check") is not None:
                    retrieve_caps.insert(0, "sec_filings_check")
            except Exception:
                retrieve_caps.insert(0, "sec_filings_check")
        else:
            retrieve_caps = ["sec_filings_check"] + [
                c for c in retrieve_caps if c != "sec_filings_check"
            ]

    wants_debate = any(
        k in low
        for k in (
            "bull",
            "bear",
            "contrarian",
            "discuss",
            "debate",
            "both",
        )
    ) or ("think" in low and any(k in low for k in ("bull", "bear", "both", "team"))) or (
        len(critique) >= 2 and any(k in low for k in ("discuss", "debate", "both", "vs", "versus"))
    )
    # Dual critique roster + multi-agent ask without verbs still debates when
    # the user @mentioned multiple roles or said "and".
    if not wants_debate and len(critique) >= 2 and (
        " and " in low or low.count("@") >= 2
    ):
        wants_debate = True

    stages: List[OrchStage] = []
    pattern = "custom"

    if retrieve_caps or research:
        stages.append(
            OrchStage(
                kind="retrieve",
                label="Retrieving…",
                agent_ids=[a.id for a in research[:2]],
                capability_ids=retrieve_caps[:4],
                power="low",
            )
        )
        stages.append(
            OrchStage(
                kind="fact_check",
                label="Fact-checking claims…",
                agent_ids=[],
                power="high",
            )
        )

    if wants_debate and critique:
        names = " / ".join(
            getattr(a, "name", a.id).replace(" Agent", "") for a in critique[:3]
        )
        stages.append(
            OrchStage(
                kind="critique",
                label=f"{names} responding…",
                agent_ids=[a.id for a in critique[:4]],
                power="high",
            )
        )
        # Follow-up retrieve/fact-check are loop iterations in execute_plan —
        # do not duplicate them in the plan card.
        if retrieve_caps or research:
            pattern = "research_critique_loop"
        else:
            pattern = "critique_only"
    elif research and not critique:
        pattern = "research_only"
    elif critique and not (retrieve_caps or research):
        names = " / ".join(
            getattr(a, "name", a.id).replace(" Agent", "") for a in critique[:3]
        )
        stages.append(
            OrchStage(
                kind="critique",
                label=f"{names} responding…",
                agent_ids=[a.id for a in critique[:3]],
                power="high",
            )
        )
        pattern = "critique_only"
    elif retrieve_caps or research:
        pattern = "research_only"

    if synthesize and pattern == "research_critique_loop":
        stages.append(
            OrchStage(
                kind="synthesize",
                label="Loop dry — summarizing",
                agent_ids=[a.id for a in synthesize[:1]],
                power="high",
            )
        )

    if not stages and automate:
        stages.append(
            OrchStage(
                kind="automate",
                label="Running automation…",
                agent_ids=[a.id for a in automate[:1]],
                capability_ids=retrieve_caps[:2],
                power="low",
            )
        )
        pattern = "automate"

    if not stages:
        # Fallback: any roster agent as reason
        any_ids = [a.id for a in roster if getattr(a, "id", "") != "orchestrator"][:3]
        stages.append(
            OrchStage(
                kind="reason",
                label="Working…",
                agent_ids=any_ids,
                power="high",
            )
        )
        pattern = "fallback"

    summary = (
        f"Flow ({pattern}): "
        + " → ".join(s.label.rstrip("…") for s in stages)
    )
    return OrchPlan(stages=stages, summary=summary, pattern=pattern)


def _extract_questions(
    text: str,
    *,
    limit: int = MAX_QUESTIONS_PER_ROUND,
    exclude_text: str = "",
) -> List[str]:
    found: List[str] = []
    exclude_low = re.sub(r"\s+", " ", (exclude_text or "").casefold().strip())

    def _too_similar(q: str) -> bool:
        ql = re.sub(r"\s+", " ", q.casefold().strip())
        if not ql:
            return True
        if exclude_low and (ql in exclude_low or exclude_low in ql):
            return True
        # Stub / meta noise
        if ql.startswith("[stub") or "using context" in ql:
            return True
        return False

    for m in _QUESTION_RE.finditer(text or ""):
        q = (m.group(1) or m.group(2) or "").strip()
        if q and not _too_similar(q) and q not in found:
            found.append(q[:300])
        if len(found) >= limit:
            break
    if not found:
        for part in re.split(r"(?<=\?)\s+", text or ""):
            part = part.strip()
            if part.endswith("?") and len(part) > 12 and not _too_similar(part):
                found.append(part[:300])
            if len(found) >= limit:
                break
    return found[:limit]


def _post(
    store: Any,
    hub: Any,
    room_id: str,
    author: str,
    body: str,
    *,
    loop: Any = None,
) -> None:
    from messenger.specialist_room import _post as post_msg

    post_msg(store, hub, room_id, author, body, loop=loop)


def _notify_run(
    hub: Any,
    room_id: str,
    *,
    status: str,
    topic: str = "",
    stage: str = "",
    loop: Any = None,
) -> None:
    """Drive the in-chat run banner (no transcript spam)."""
    from messenger.specialist_room import _broadcast

    label = stage or ("Working…" if status == "running" else "")
    topic_bit = (topic or "").strip()
    if len(topic_bit) > 72:
        topic_bit = topic_bit[:69] + "…"
    message = (
        f"Orchestrator · {label}"
        + (f" — “{topic_bit}”" if topic_bit and status == "running" else "")
    )
    _broadcast(
        hub,
        loop,
        room_id,
        {
            "type": "orch_run",
            "job": {
                "status": status,
                "action": "orchestrate",
                "topic": topic_bit,
                "stage": stage,
                "message": message,
                "continuous": False,
            },
        },
    )


def _compose_consensus(
    *,
    ask: str,
    evidence: str,
    critiques: Sequence[tuple[str, str]],
    synth_brief: str,
    checks: Sequence[Dict[str, Any]],
    grade: Any,
    dry: bool,
) -> str:
    """Single final report for the room transcript."""
    supported = sum(1 for c in checks if str(c.get("verdict")) == "supported")
    contradicted = sum(1 for c in checks if str(c.get("verdict")) == "contradicted")
    insufficient = sum(
        1
        for c in checks
        if str(c.get("verdict")) in {"insufficient", "unavailable"}
    )

    parts: List[str] = [
        f"**Consensus report** — {(ask or '').strip()[:160]}",
        "",
    ]
    if synth_brief.strip():
        parts.append(synth_brief.strip()[:1600])
        parts.append("")
    elif critiques:
        parts.append("### Positions")
        for name, body in critiques[-4:]:
            # First meaningful line of each critique
            line = next(
                (ln.strip() for ln in body.splitlines() if ln.strip()),
                "",
            )
            if line:
                parts.append(f"- **{name}:** {line[:280]}")
        parts.append("")

    ev = (evidence or "").strip()
    if ev:
        # Skip KG header noise for the brief evidence line
        ev_lines = [
            ln
            for ln in ev.splitlines()
            if ln.strip() and not ln.startswith("[Room knowledge")
        ]
        snippet = " ".join(ev_lines[:4])[:500]
        if snippet:
            parts.append("### Evidence (grounded)")
            parts.append(snippet)
            parts.append("")

    parts.append(
        f"_Fact-check: {supported} supported · {contradicted} disputed · "
        f"{insufficient} insufficient"
        + (" · loop dry_" if dry else "_")
    )
    if grade is not None:
        parts.append(f"_{orch_grader.format_grade_chip(grade)}_")
    return "\n".join(parts).strip()[:2000]


def _resolve_endpoint(owner_user_id: Optional[str], store: Any, room_id: str) -> Any:
    if not owner_user_id:
        return None
    try:
        from messenger.model_link import registry as model_registry

        room = store.room(room_id) if store is not None else None
        profile_id = None
        if isinstance(room, dict):
            profile_id = (room.get("config") or {}).get("model_profile_id")
            profile_id = str(profile_id).strip() if profile_id else None
        return model_registry().endpoint_for_call(
            owner_user_id, profile_id=profile_id
        )
    except Exception:
        return None


def _run_retrieve(
    *,
    text: str,
    agents: Sequence[Any],
    capability_ids: Sequence[str],
    owner_user_id: Optional[str],
    stub: bool,
) -> str:
    notes: List[str] = []
    # Prefer approved automation runners when capability maps to a runner
    if owner_user_id and capability_ids and not stub:
        try:
            from messenger.tenancy import user_context
            from analyst_ledger.runners import RUNNERS, resolve_runner

            with user_context(owner_user_id) as ledger:
                for cid in capability_ids[:2]:
                    fn = None
                    if cid in RUNNERS:
                        fn = RUNNERS[cid]
                    elif cid != "web_research":
                        try:
                            _name, fn = resolve_runner(cid)
                        except Exception:
                            fn = None
                    if fn is None:
                        continue
                    try:
                        result = fn(
                            ledger=ledger,
                            ritual_id=cid,
                            stub=False,
                            require_approved=True,
                        )
                        note = str((result or {}).get("note") or "").strip()
                        if note:
                            notes.append(f"[{cid}]\n{note[:1500]}")
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"[{cid}] unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"(retrieve ledger path failed: {exc})")

    if stub:
        caps = ", ".join(capability_ids[:4]) or "web_research"
        primary = (capability_ids[0] if capability_ids else "web_research")
        notes.append(
            f"[stub retrieve:{primary}] Evidence pack for: {(text or '')[:200]}\n"
            f"Caps: {caps}. Sample: TSLA had recent EDGAR activity (stub — not live)."
        )
        return "\n\n".join(notes)

    # Web research via first research agent when no runner notes
    if not notes and agents:
        try:
            from analyst_ledger.friend_qwen import compose_research_reply
            from analyst_ledger.friend_personalities import personality_from_agent_id

            agent = agents[0]
            personality = personality_from_agent_id(agent.id)
            if personality is not None:
                notes.append(
                    compose_research_reply(
                        text, context_text=text, personality=personality
                    )[:1800]
                )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"(research unavailable: {exc})")

    if not notes:
        notes.append(
            "No retrieve backend available — Unavailable (not inventing)."
        )
    return "\n\n".join(notes)


def _chat_as(
    agent: Any,
    *,
    author: str,
    snippet: str,
    context: str,
    endpoint: Any,
    room_guidance: str = "",
    stub: bool = False,
) -> str:
    if stub:
        return _stub_persona_reply(agent, snippet=snippet, context=context)
    if endpoint is None:
        return (
            f"({getattr(agent, 'name', agent.id)}: no model linked — "
            "select a room model or Start local model.)"
        )
    try:
        from analyst_ledger.synthesize import call_chat_messages, use_llm_endpoint
    except Exception as exc:
        return f"(model stack unavailable: {exc})"
    system = (
        f"You are {getattr(agent, 'name', agent.id)}. "
        f"{getattr(agent, 'prompt', '')} Never invent facts. "
        "Ground every material claim in the fact-checked material below. "
        "If evidence is missing, say Unavailable. "
        "End with up to 3 concrete follow-up questions only when material gaps remain "
        "(do not restate the user's question)."
    )
    if room_guidance:
        system += f"\n{room_guidance}"
    try:
        with use_llm_endpoint(endpoint):
            return call_chat_messages(
                [
                    {
                        "role": "user",
                        "content": (
                            f"{author} asked:\n{snippet}\n\n"
                            f"Fact-checked material:\n{context[:3500]}\n\n"
                            "Reply in character, briefly."
                        ),
                    }
                ],
                max_tokens=650,
                system=system,
                temperature=0.35,
            ).strip()
    except Exception as exc:  # noqa: BLE001
        return f"({getattr(agent, 'name', agent.id)} unavailable: {exc})"


def _stub_persona_reply(agent: Any, *, snippet: str, context: str) -> str:
    """Deterministic, role-shaped stub replies for offline orch tests."""
    role = (getattr(agent, "role", "") or getattr(agent, "stage", "") or "").casefold()
    name = getattr(agent, "name", getattr(agent, "id", "agent"))
    ctx_bit = (context or "").strip()
    evidence_line = (
        ctx_bit.splitlines()[0][:160]
        if ctx_bit
        else "Unavailable — no fact-checked material yet."
    )
    ask = (snippet or "").strip()[:120]
    follow_up_pass = "follow-up research" in (snippet or "").casefold() or (
        "follow-up research" in (context or "").casefold()
    )
    if role in {"bull"} or "bull" in name.casefold():
        body = (
            f"**Bull case** (grounded in pack): {evidence_line}\n"
            f"Upside thesis vs “{ask}”: if the retrieved facts hold, the constructive "
            "case is that execution continues without a thesis-breaking filing.\n"
            "Strongest bear risk in one line: a adverse 8-K could invalidate this quickly."
        )
        if not follow_up_pass:
            body += (
                "\nFollow-ups:\n"
                "- What is the most recent 8-K item that would confirm growth?\n"
                "- Which segment disclosure would falsify the upside?"
            )
        else:
            body += "\nNo new material questions — pack already addressed prior follow-ups."
        return body
    if role in {"bear", "contrarian"} or "contrarian" in name.casefold():
        body = (
            f"**Bear case** (grounded in pack): {evidence_line}\n"
            f"Caution on “{ask}”: treat unverified numbers as Unavailable; the "
            "downside is incentive or disclosure risk not yet ruled out by the pack.\n"
            "Falsification test: a clean sequential filing without adverse items "
            "would weaken this caution."
        )
        if not follow_up_pass:
            body += (
                "\nFollow-ups:\n"
                "- Which filing line item is still missing from the pack?\n"
                "- What evidence would make the caution unanswerable?"
            )
        else:
            body += "\nNo new material questions — remaining gaps are unanswerable from this pack."
        return body
    if role in {"synthesizer", "synthesize"} or "synth" in name.casefold():
        return (
            f"**Synthesis** for “{ask}”: shared fact — {evidence_line}\n"
            "Disagreement: bull stresses continuity; bear stresses missing falsifiers.\n"
            "Next checks: (1) latest filing item, (2) one metric both sides accept, "
            "(3) mark unresolved questions unanswerable if still open."
        )
    body = (
        f"**{name}** on “{ask}”: {evidence_line}\n"
        "Separating pack facts from inference; gaps marked Unavailable."
    )
    if not follow_up_pass:
        body += (
            "\nFollow-ups:\n"
            "- What additional source would resolve the top open question?"
        )
    return body


def execute_plan(
    plan: OrchPlan,
    *,
    store: Any,
    hub: Any,
    room_id: str,
    author: str,
    text: str,
    roster: Sequence[Any],
    owner_user_id: Optional[str] = None,
    loop: Any = None,
    stub: bool = False,
    max_rounds: int = MAX_ROUNDS,
) -> Dict[str, Any]:
    """Run the plan quietly; show loading banner; post one consensus report."""
    by_id = {a.id: a for a in roster}
    endpoint = None if stub else _resolve_endpoint(owner_user_id, store, room_id)
    kg = room_kg.load_kg(room_id)
    kg_snap = room_kg.compress_snapshot(kg)

    room_guidance = ""
    try:
        room = store.room(room_id) if store is not None else None
        if isinstance(room, dict):
            from messenger.specialist_room import _room_guidance

            room_guidance = _room_guidance(room)
    except Exception:
        room_guidance = ""

    _notify_run(
        hub,
        room_id,
        status="running",
        topic=text,
        stage="Starting…",
        loop=loop,
    )

    evidence = kg_snap
    all_checks: List[Dict[str, Any]] = []
    open_questions: List[str] = []
    resolved_questions = 0
    questions_opened = 0
    stages_run: List[str] = []
    rounds = 0
    dry = False
    critique_notes: List[tuple[str, str]] = []
    synth_brief = ""

    # Expand research_critique_loop into round iterations
    loop_pattern = plan.pattern == "research_critique_loop"
    retrieve_stage = next((s for s in plan.stages if s.kind == "retrieve"), None)
    fact_stage = next((s for s in plan.stages if s.kind == "fact_check"), None)
    critique_stage = next((s for s in plan.stages if s.kind == "critique"), None)
    synth_stage = next((s for s in plan.stages if s.kind == "synthesize"), None)

    def do_retrieve(label: str, stage: OrchStage, query: str) -> str:
        nonlocal evidence
        _notify_run(
            hub, room_id, status="running", topic=text, stage=label, loop=loop
        )
        stages_run.append(stage.kind)
        agents = [by_id[i] for i in stage.agent_ids if i in by_id]
        chunk = _run_retrieve(
            text=query,
            agents=agents,
            capability_ids=stage.capability_ids,
            owner_user_id=owner_user_id,
            stub=stub,
        )
        evidence = (evidence + "\n\n" + chunk).strip()
        return chunk

    def do_fact_check(label: str, draft: str) -> str:
        _notify_run(
            hub, room_id, status="running", topic=text, stage=label, loop=loop
        )
        stages_run.append("fact_check")
        checks = fact_checker.check_claims(
            draft,
            evidence=evidence,
            endpoint=endpoint,
            stub=stub,
            allow_web=not stub,
        )
        pubs = fact_checker.checks_public(checks)
        all_checks.extend(pubs)
        room_kg.apply_fact_check(kg, pubs)
        return fact_checker.rewrite_with_checks(draft, checks)

    def do_critique(stage: OrchStage, material: str) -> List[str]:
        nonlocal questions_opened
        _notify_run(
            hub,
            room_id,
            status="running",
            topic=text,
            stage=stage.label,
            loop=loop,
        )
        stages_run.append("critique")
        packed = (
            f"{material}\n\n{room_kg.compress_snapshot(kg)}"
            if material
            else room_kg.compress_snapshot(kg)
        )
        new_qs: List[str] = []
        for aid in stage.agent_ids:
            agent = by_id.get(aid)
            if agent is None:
                continue
            reply = _chat_as(
                agent,
                author=author,
                snippet=text,
                context=packed,
                endpoint=endpoint,
                room_guidance=room_guidance,
                stub=stub,
            )
            name = getattr(agent, "name", aid)
            critique_notes.append((name, reply))
            for q in _extract_questions(reply, exclude_text=text):
                if q not in open_questions and q not in new_qs:
                    new_qs.append(q)
                    room_kg.upsert_question(
                        kg, q, status="open", from_agent=aid
                    )
        questions_opened += len(new_qs)
        open_questions.extend(new_qs)
        return new_qs

    try:
        if loop_pattern and retrieve_stage and critique_stage:
            query = text
            researched_qs: set[str] = set()
            for round_i in range(max(1, max_rounds)):
                rounds = round_i + 1
                label = (
                    retrieve_stage.label
                    if round_i == 0
                    else "Research follow-up on open questions…"
                )
                draft = do_retrieve(label, retrieve_stage, query)
                material = do_fact_check(
                    fact_stage.label if fact_stage else "Fact-checking claims…",
                    draft,
                )
                new_qs = do_critique(critique_stage, material)
                fresh = [q for q in new_qs if q not in researched_qs]
                for q in new_qs:
                    if q not in researched_qs:
                        researched_qs.add(q)
                if not fresh or round_i >= max_rounds - 1:
                    dry = not fresh
                    if fresh and round_i >= max_rounds - 1:
                        for q in fresh:
                            room_kg.upsert_question(
                                kg,
                                q,
                                status="unanswerable",
                                from_agent="orchestrator",
                            )
                    break
                take = fresh[:MAX_QUESTIONS_PER_ROUND]
                for q in fresh[MAX_QUESTIONS_PER_ROUND:]:
                    room_kg.upsert_question(
                        kg, q, status="unanswerable", from_agent="orchestrator"
                    )
                    if q in open_questions:
                        open_questions.remove(q)
                query = "Follow-up research:\n- " + "\n- ".join(take)
                for q in take:
                    room_kg.upsert_question(
                        kg, q, status="resolved", from_agent="orchestrator"
                    )
                    resolved_questions += 1
                    if q in open_questions:
                        open_questions.remove(q)
            if synth_stage:
                _notify_run(
                    hub,
                    room_id,
                    status="running",
                    topic=text,
                    stage="Summarizing…",
                    loop=loop,
                )
                stages_run.append("synthesize")
                agent = (
                    by_id.get(synth_stage.agent_ids[0])
                    if synth_stage.agent_ids
                    else None
                )
                if agent is not None:
                    critique_blob = "\n\n".join(
                        f"{n}:\n{b}" for n, b in critique_notes[-6:]
                    )
                    synth_brief = _chat_as(
                        agent,
                        author=author,
                        snippet=text,
                        context=(evidence + "\n\n" + critique_blob)[:3500],
                        endpoint=endpoint,
                        room_guidance=room_guidance,
                        stub=stub,
                    )
        else:
            material = evidence
            for stage in plan.stages:
                if stage.kind in {"retrieve", "automate"}:
                    material = do_retrieve(stage.label, stage, text)
                elif stage.kind == "fact_check":
                    material = do_fact_check(stage.label, material or text)
                elif stage.kind == "critique":
                    do_critique(stage, material or evidence)
                elif stage.kind in {"synthesize", "reason"}:
                    _notify_run(
                        hub,
                        room_id,
                        status="running",
                        topic=text,
                        stage=stage.label,
                        loop=loop,
                    )
                    stages_run.append(stage.kind)
                    for aid in stage.agent_ids[:3]:
                        agent = by_id.get(aid)
                        if agent is None:
                            continue
                        reply = _chat_as(
                            agent,
                            author=author,
                            snippet=text,
                            context=material or evidence,
                            endpoint=endpoint,
                            room_guidance=room_guidance,
                            stub=stub,
                        )
                        if stage.kind == "synthesize":
                            synth_brief = reply
                        else:
                            critique_notes.append(
                                (getattr(agent, "name", aid), reply)
                            )
            dry = True
    finally:
        try:
            room_kg.save_kg(kg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("kg save failed: %s", exc)

    grade = orch_grader.grade_run(
        path="orchestrator",
        rounds=rounds,
        dry=dry,
        questions_opened=questions_opened,
        questions_resolved=resolved_questions,
        fact_checks=all_checks,
        stages=stages_run,
    )
    report = _compose_consensus(
        ask=text,
        evidence=evidence,
        critiques=critique_notes,
        synth_brief=synth_brief,
        checks=all_checks,
        grade=grade,
        dry=dry,
    )
    _post(store, hub, room_id, "Orchestrator", report, loop=loop)
    _notify_run(hub, room_id, status="completed", topic=text, loop=loop)
    return {
        "ok": True,
        "plan": plan.public(),
        "grade": grade.public(),
        "rounds": rounds,
        "dry": dry,
        "fact_checks": all_checks,
        "report": report,
    }


def start_orchestrator_job(
    *,
    store: Any,
    hub: Any,
    room_id: str,
    author: str,
    text: str,
    roster: Sequence[Any],
    owner_user_id: Optional[str] = None,
    loop: Any = None,
    room_skills: Optional[Sequence[str]] = None,
    harness: Optional[Dict[str, Any]] = None,
    stub: bool = False,
) -> Dict[str, Any]:
    """Draw plan and execute in a daemon thread (or sync when stub)."""
    if room_skills is None or harness is None:
        try:
            room = store.room(room_id) if store is not None else None
            config = room.get("config") if isinstance(room, dict) else None
            if isinstance(config, dict):
                if harness is None:
                    harness = config
                if room_skills is None and isinstance(config.get("skills"), list):
                    room_skills = [
                        str(s).strip() for s in config["skills"] if str(s).strip()
                    ]
        except Exception:
            pass
    plan = draw_org(
        text, roster=roster, room_skills=room_skills, harness=harness
    )

    def work() -> Dict[str, Any]:
        try:
            if owner_user_id:
                from messenger.tenancy import user_context

                with user_context(owner_user_id):
                    return execute_plan(
                        plan,
                        store=store,
                        hub=hub,
                        room_id=room_id,
                        author=author,
                        text=text,
                        roster=roster,
                        owner_user_id=owner_user_id,
                        loop=loop,
                        stub=stub,
                    )
            return execute_plan(
                plan,
                store=store,
                hub=hub,
                room_id=room_id,
                author=author,
                text=text,
                roster=roster,
                owner_user_id=owner_user_id,
                loop=loop,
                stub=stub,
            )
        except Exception:
            logger.exception("orchestrator job failed room=%s", room_id)
            try:
                _notify_run(hub, room_id, status="failed", topic=text, loop=loop)
            except Exception:
                pass
            try:
                _post(
                    store,
                    hub,
                    room_id,
                    "Orchestrator",
                    "Orchestrator run failed — see server logs.",
                    loop=loop,
                )
            except Exception:
                pass
            return {"ok": False, "plan": plan.public()}

    if stub:
        out = work()
        out.setdefault("sync", True)
        out.setdefault("plan", plan.public())
        return out

    threading.Thread(target=work, name="team-orch", daemon=True).start()
    return {"ok": True, "plan": plan.public(), "started": True}
