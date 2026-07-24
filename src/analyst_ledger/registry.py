"""Flyleaf registry — system of record for Capabilities, Agents, Automations.

Capabilities and Agents are first-class typed records. Built-ins / product agents
are seeded in code; per-user capabilities and automations persist under the
active ledger data dir (``registry/`` + ritual specs).

Runtime (room specialists, research, tabs, /automate) must read through this
module so the UI and the code paths cannot drift apart.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """A reusable verb the system can invoke."""

    id: str
    name: str
    kind: str  # builtin | user
    summary: str
    invoke: str = ""
    schedulable: bool = False
    needs_model: bool = False
    runner: Optional[str] = None
    action: Optional[str] = None
    # User / draft fields
    approved: bool = True
    enabled: bool = True
    status: str = "ready"
    ritual_id: Optional[str] = None
    proposed_by: Optional[str] = None
    schedule: Optional[str] = None
    steps: Tuple[str, ...] = ()
    watchlist: Tuple[str, ...] = ()
    model: Optional[str] = None
    last_run: Optional[Dict[str, Any]] = None
    confidence: Optional[float] = None
    evidence_count: Optional[int] = None

    def to_public(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = list(self.steps)
        d["watchlist"] = list(self.watchlist)
        if self.ritual_id is None and self.kind == "user":
            d["ritual_id"] = self.id
        return d


@dataclass(frozen=True)
class Agent:
    """How capabilities get used in rooms — lens (prompt) or operator."""

    id: str
    name: str
    kind: str  # lens | operator
    role: str
    mention: str
    prompt: str
    capabilities: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()
    legacy_names: Tuple[str, ...] = ()
    cookie_key: str = ""
    summary: str = ""
    how: str = ""
    model: str = "room or Settings active profile"
    # Show in room Specialists palette / drag-drop.
    room_palette: bool = True

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "role": self.role,
            "mention": self.mention,
            "aliases": list(self.aliases),
            "legacy_names": list(self.legacy_names),
            "cookie_key": self.cookie_key or self.id,
            "summary": self.summary,
            "how": self.how,
            "capabilities": list(self.capabilities),
            "model": self.model,
            "room_palette": self.room_palette,
            "uses_capabilities": bool(self.capabilities),
            "prompt": self.prompt,
        }

    def can_use(self, capability_id: str) -> bool:
        return capability_id in self.capabilities


@dataclass(frozen=True)
class Automation:
    """An ordered loop of capabilities with an optional schedule."""

    id: str
    name: str
    capability_ids: Tuple[str, ...]
    approved: bool = False
    enabled: bool = False
    schedule: Optional[str] = None
    runner: Optional[str] = None
    model: Optional[str] = None
    room_id: Optional[str] = None
    source: str = "registry"
    watchlist: Tuple[str, ...] = ()
    last_run: Optional[Dict[str, Any]] = None
    proposed_by: Optional[str] = None

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ritual_id": self.id,
            "kind": "automation",
            "capabilities": list(self.capability_ids),
            "capability_ids": list(self.capability_ids),
            "approved": self.approved,
            "enabled": self.enabled,
            "schedule": self.schedule,
            "runner": self.runner,
            "model": self.model,
            "room_id": self.room_id,
            "source": self.source,
            "watchlist": list(self.watchlist),
            "last_run": self.last_run,
            "proposed_by": self.proposed_by,
        }


# ---------------------------------------------------------------------------
# Seed data (product SoR for builtins)
# ---------------------------------------------------------------------------

_BUILTIN_CAPABILITIES: Tuple[Capability, ...] = (
    Capability(
        id="web_research",
        name="Web research",
        kind="builtin",
        summary="Background public-web research pass for a question or ticker.",
        invoke="@Analyst research … (operator with web_research)",
        schedulable=False,
        needs_model=True,
    ),
    Capability(
        id="morning_yf_scan",
        name="Morning quote scan",
        kind="builtin",
        summary="Deterministic Yahoo-style watchlist quote / calendar / headlines scan.",
        invoke="runner · @workflow · Master router",
        schedulable=True,
        runner="morning_yf_scan",
    ),
    Capability(
        id="generic_watchlist_scan",
        name="Watchlist scan",
        kind="builtin",
        summary="Generic watchlist scan runner for recurring symbol checks.",
        invoke="runner · @workflow · Master router",
        schedulable=True,
        runner="generic_watchlist_scan",
    ),
    Capability(
        id="sec_filings_check",
        name="SEC filings check",
        kind="builtin",
        summary="Pull recent SEC EDGAR filings for watchlist symbols.",
        invoke="runner · @workflow · Master router",
        schedulable=True,
        runner="sec_filings_check",
    ),
    Capability(
        id="note_digest",
        name="Note digest",
        kind="builtin",
        summary="Digest recent ledger notes into a short brief.",
        invoke="runner · @workflow · Master router",
        schedulable=True,
        runner="note_digest",
    ),
    Capability(
        id="fetch_quote",
        name="Fetch quote",
        kind="builtin",
        summary="Governed workflow step: allowlisted quote fields.",
        invoke="WorkflowEngine step",
        action="fetch_quote",
    ),
    Capability(
        id="fetch_calendar",
        name="Fetch calendar",
        kind="builtin",
        summary="Governed workflow step: earnings / calendar fields.",
        invoke="WorkflowEngine step",
        action="fetch_calendar",
    ),
    Capability(
        id="fetch_headlines",
        name="Fetch headlines",
        kind="builtin",
        summary="Governed workflow step: recent headlines.",
        invoke="WorkflowEngine step",
        action="fetch_headlines",
    ),
    Capability(
        id="find_files",
        name="Find files",
        kind="builtin",
        summary="Search configured research folders (never the whole drive).",
        invoke="chat file-finder · WorkflowEngine step",
        action="find_files",
    ),
    Capability(
        id="public_web_search",
        name="Public web search",
        kind="builtin",
        summary="Allowlisted public web search step inside a governed workflow.",
        invoke="WorkflowEngine step",
        needs_model=True,
        action="public_web_search",
    ),
    Capability(
        id="classify_message",
        name="Classify message",
        kind="builtin",
        summary="Tag chat/tracking messages with kind / entity / topic.",
        invoke="capture + classify sweep",
        schedulable=True,
    ),
)

_BUILTIN_AGENTS: Tuple[Agent, ...] = (
    Agent(
        id="qwen",
        name="Analyst",
        kind="operator",
        role="analyst",
        mention="@Analyst",
        aliases=("@Qwen",),
        legacy_names=("Qwen",),
        cookie_key="qwen",
        capabilities=("web_research", "classify_message"),
        summary="Evidence-led balanced analyst. Can chat or kick off web research.",
        how="Uses capabilities when you ask to research / look up; otherwise prompt lens.",
        prompt=(
            "Your role is an evidence-led balanced analyst. Answer directly, separate "
            "verified facts from inference, present the strongest credible bull and "
            "bear considerations, and name the evidence needed to resolve uncertainty. "
            "Correct stale premises explicitly. Never invent a fact, date, number, "
            "source, product, or company initiative."
        ),
        room_palette=True,
    ),
    Agent(
        id="qwen-bull",
        name="Bullish Agent",
        kind="lens",
        role="bull",
        mention="@Bullish",
        aliases=("@Qwen-Bull", "@Bull"),
        legacy_names=("Qwen Bull",),
        cookie_key="qwen-bull",
        capabilities=(),
        summary="Steelmans the upside case. Does not own runners or schedules.",
        how="Prompt injection only — same model as the room, different system prompt.",
        prompt=(
            "Your role is the constructive bull case specialist. Steelman the upside: "
            "name the specific thesis, the mechanism that creates value, and the "
            "evidence that would confirm it. Acknowledge the strongest bear risk in "
            "one sentence, then return to what would make the bull case right. Never "
            "invent a fact, date, number, source, product, or company initiative."
        ),
        room_palette=True,
    ),
    Agent(
        id="qwen-contrarian",
        name="Contrarian Agent",
        kind="lens",
        role="bear",
        mention="@Contrarian",
        aliases=("@Qwen-Contrarian",),
        legacy_names=("Qwen Contrarian",),
        cookie_key="qwen-contrarian",
        capabilities=(),
        summary="Evidence-led downside / falsification lens. Not a capability.",
        how="Prompt injection only — same model as the room, different system prompt.",
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
        room_palette=True,
    ),
    Agent(
        id="qwen-synthesizer",
        name="Synthesizer Agent",
        kind="lens",
        role="synthesizer",
        mention="@Synthesizer",
        aliases=("@Qwen-Synthesizer",),
        legacy_names=("Qwen Synthesizer",),
        cookie_key="qwen-synthesizer",
        capabilities=(),
        summary="Closes multi-agent debate with shared facts and next checks.",
        how="Prompt injection in specialist present / debate / idea runs.",
        prompt=(
            "Your role is the synthesizer. After hearing multiple specialist views, "
            "extract shared facts, name the real disagreement, and propose 2–4 "
            "concrete research ideas or next checks that would move the debate. "
            "Prefer falsifiable questions over opinions. Never invent a fact, date, "
            "number, source, product, or company initiative."
        ),
        room_palette=True,
    ),
    Agent(
        id="master",
        name="Master",
        kind="operator",
        role="router",
        mention="Agents → Master thread",
        cookie_key="master",
        capabilities=(
            "morning_yf_scan",
            "generic_watchlist_scan",
            "sec_filings_check",
            "note_digest",
            "web_research",
            "find_files",
        ),
        summary="Routes asks to approved capability loops before calling a model.",
        how="Deterministic router → runners / workflows; model only on gaps.",
        prompt="",
        model="Settings active profile (for novel asks)",
        room_palette=False,
    ),
)


def _registry_dir() -> Path:
    from .paths import data_dir

    path = data_dir() / "registry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _user_capabilities_path() -> Path:
    return _registry_dir() / "capabilities.json"


# ---------------------------------------------------------------------------
# Capability API
# ---------------------------------------------------------------------------


def get_capability(capability_id: str) -> Optional[Capability]:
    cid = (capability_id or "").strip()
    for cap in _BUILTIN_CAPABILITIES:
        if cap.id == cid:
            return cap
    for cap in _load_user_capabilities():
        if cap.id == cid:
            return cap
    return None


def list_builtin_capabilities() -> List[Capability]:
    return list(_BUILTIN_CAPABILITIES)


def _load_user_capabilities() -> List[Capability]:
    path = _user_capabilities_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw if isinstance(raw, list) else raw.get("capabilities") or []
    out: List[Capability] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        out.append(
            Capability(
                id=str(row["id"]),
                name=str(row.get("name") or row["id"]),
                kind="user",
                summary=str(row.get("summary") or ""),
                invoke=str(row.get("invoke") or ""),
                schedulable=bool(row.get("schedulable")),
                needs_model=bool(row.get("needs_model")),
                runner=row.get("runner"),
                action=row.get("action"),
                approved=bool(row.get("approved", False)),
                enabled=bool(row.get("enabled", False)),
                status=str(row.get("status") or ("approved" if row.get("approved") else "draft")),
                ritual_id=str(row.get("ritual_id") or row["id"]),
                proposed_by=row.get("proposed_by"),
                schedule=row.get("schedule"),
                steps=tuple(row.get("steps") or ()),
                watchlist=tuple(row.get("watchlist") or ()),
                model=row.get("model"),
            )
        )
    return out


def save_user_capability(cap: Capability) -> Capability:
    """Upsert a user capability into registry/capabilities.json."""
    if cap.kind != "user":
        raise ValueError("only user capabilities can be persisted here")
    existing = {c.id: c for c in _load_user_capabilities()}
    existing[cap.id] = cap
    payload = {"capabilities": [c.to_public() for c in existing.values()]}
    path = _user_capabilities_path()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return cap


def _capability_from_ritual_row(row: Dict[str, Any], spec: Optional[Dict[str, Any]]) -> Capability:
    rid = str(row.get("ritual_id") or row.get("name") or "").strip()
    approved = bool(row.get("approved"))
    enabled = bool(row.get("enabled", True)) if approved else False
    status = "approved" if approved else "draft"
    if approved and not row.get("enabled", True):
        status = "disabled"
    steps: List[str] = []
    schedule = row.get("schedule")
    proposed_by = None
    summary = (
        f"Mined / drafted capability"
        f"{' · runner ' + str(row.get('runner')) if row.get('runner') else ''}."
    )
    if isinstance(spec, dict):
        schedule = schedule or spec.get("schedule")
        proposed_by = spec.get("proposed_by")
        desc = (spec.get("description") or "").strip()
        if desc:
            summary = desc[:240]
        for step in spec.get("steps") or []:
            if isinstance(step, dict) and step:
                steps.append(str(next(iter(step))))
            elif isinstance(step, str):
                steps.append(step)
    return Capability(
        id=rid,
        name=rid.replace("_", " "),
        kind="user",
        summary=summary,
        invoke=f"@workflow {rid}" if approved else "approve first",
        schedulable=bool(row.get("runner") or schedule),
        needs_model=bool(row.get("model") or (spec or {}).get("model")),
        runner=row.get("runner") or (spec or {}).get("runner"),
        approved=approved,
        enabled=enabled,
        status=status,
        ritual_id=rid,
        proposed_by=proposed_by,
        schedule=schedule,
        steps=tuple(steps),
        watchlist=tuple(row.get("watchlist") or []),
        model=row.get("model") or (spec or {}).get("model"),
        last_run=row.get("last_run"),
        confidence=row.get("confidence"),
        evidence_count=row.get("evidence_count"),
    )


def list_capabilities(*, ledger: Any = None) -> List[Capability]:
    """Built-ins + registry user caps + ritual-spec capabilities (deduped)."""
    by_id: Dict[str, Capability] = {c.id: c for c in _BUILTIN_CAPABILITIES}
    for cap in _load_user_capabilities():
        by_id[cap.id] = cap
    if ledger is not None:
        from .rituals import list_automations, load_spec

        for row in list_automations(ledger):
            rid = str(row.get("ritual_id") or "").strip()
            if not rid:
                continue
            try:
                spec = load_spec(rid)
            except Exception:
                spec = None
            # Rituals that are approved+enabled loops are still listed as
            # capabilities (verbs/instances); Automations tab filters loops.
            by_id[rid] = _capability_from_ritual_row(row, spec if isinstance(spec, dict) else None)
    return list(by_id.values())


def list_capabilities_public(*, ledger: Any = None) -> List[Dict[str, Any]]:
    return [c.to_public() for c in list_capabilities(ledger=ledger)]


# ---------------------------------------------------------------------------
# Agent API
# ---------------------------------------------------------------------------


def list_agents() -> List[Agent]:
    return list(_BUILTIN_AGENTS)


def get_agent(agent_id: str) -> Optional[Agent]:
    aid = (agent_id or "").strip()
    for agent in _BUILTIN_AGENTS:
        if agent.id == aid:
            return agent
    return None


def list_agents_public() -> List[Dict[str, Any]]:
    by_cap = {c.id: c for c in _BUILTIN_CAPABILITIES}
    rows: List[Dict[str, Any]] = []
    for agent in _BUILTIN_AGENTS:
        pub = agent.to_public()
        pub["capability_details"] = [
            {"id": cid, "name": (by_cap[cid].name if cid in by_cap else cid)}
            for cid in agent.capabilities
        ]
        # Don't leak full prompts on the catalog API by default.
        pub.pop("prompt", None)
        rows.append(pub)
    return rows


def list_room_palette_public() -> List[Dict[str, Any]]:
    """Agents shown in the room Specialists dock — same SoR as Agents tab."""
    return [
        {
            "id": a.id,
            "name": a.name,
            "mention": a.mention,
            "aliases": list(a.aliases),
            "legacy_names": list(a.legacy_names),
            "role": a.role,
            "kind": a.kind,
            "capabilities": list(a.capabilities),
        }
        for a in _BUILTIN_AGENTS
        if a.room_palette
    ]


def agent_has_capability(agent_id: str, capability_id: str) -> bool:
    agent = get_agent(agent_id)
    if agent is None:
        return False
    return agent.can_use(capability_id)


# ---------------------------------------------------------------------------
# Automation API (ritual specs as persistence)
# ---------------------------------------------------------------------------


def list_automations(*, ledger: Any = None) -> List[Automation]:
    """Approved + enabled capability loops."""
    if ledger is None:
        return []
    from .rituals import list_automations as list_ritual_rows, load_spec

    out: List[Automation] = []
    for row in list_ritual_rows(ledger):
        if not row.get("approved"):
            continue
        if not row.get("enabled", True):
            continue
        rid = str(row.get("ritual_id") or "")
        if not rid:
            continue
        try:
            spec = load_spec(rid)
        except Exception:
            spec = None
        steps: List[str] = []
        schedule = row.get("schedule")
        room_id = None
        proposed_by = None
        if isinstance(spec, dict):
            schedule = schedule or spec.get("schedule")
            room_id = spec.get("room_id")
            proposed_by = spec.get("proposed_by")
            for step in spec.get("steps") or []:
                if isinstance(step, dict) and step:
                    steps.append(str(next(iter(step))))
                elif isinstance(step, str):
                    steps.append(step)
        if not steps and row.get("runner"):
            steps = [str(row.get("runner"))]
        out.append(
            Automation(
                id=rid,
                name=rid.replace("_", " "),
                capability_ids=tuple(steps),
                approved=True,
                enabled=True,
                schedule=schedule,
                runner=row.get("runner") or (spec or {}).get("runner"),
                model=row.get("model") or (spec or {}).get("model"),
                room_id=str(room_id) if room_id else None,
                source="approved_capability",
                watchlist=tuple(row.get("watchlist") or []),
                last_run=row.get("last_run"),
                proposed_by=proposed_by,
            )
        )
    return out


def list_automations_public(*, ledger: Any = None) -> List[Dict[str, Any]]:
    return [a.to_public() for a in list_automations(ledger=ledger)]


def create_automation_from_chat(
    *,
    name: str,
    capability_ids: Sequence[str],
    schedule: Optional[str] = None,
    room_id: Optional[str] = None,
    transcript: str = "",
    watchlist: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Persist a draft automation (ritual spec) composed of capability ids."""
    from .paths import ritual_specs_dir
    from .schema import utc_now_iso

    raw_name = (name or "").strip()
    rid = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_name).strip("_")[:80]
    if not rid or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,120}$", rid):
        raise ValueError("name must start with a letter/number")

    caps = [str(c).strip() for c in capability_ids if str(c).strip()]
    if not caps:
        raise ValueError("steps required")

    builtin = {c.id: c for c in _BUILTIN_CAPABILITIES}
    steps: List[Dict[str, Any]] = []
    runner = None
    for cid in caps[:12]:
        meta = builtin.get(cid)
        if meta and meta.action:
            steps.append({meta.action: {}})
        elif meta and meta.runner:
            runner = runner or meta.runner
            steps.append({meta.runner: {}})
        else:
            steps.append({cid: {}})

    spec = {
        "name": rid,
        "version": 1,
        "approved": False,
        "enabled": False,
        "runner": runner or "note_digest",
        "schedule": schedule,
        "schedule_comment": "Drafted from room /automate",
        "watchlist": list(watchlist or []),
        "steps": steps,
        "capability_ids": caps[:12],
        "outputs": {"ledger_session": True},
        "room_id": room_id,
        "source_chat": {"transcript_excerpt": (transcript or "")[:2000]},
        "proposed_by": "room_automate",
        "created_at": utc_now_iso(),
        "description": f"Automation loop drafted from chat ({len(steps)} capability steps).",
    }

    path = ritual_specs_dir() / f"{rid}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if existing.get("approved"):
            raise ValueError("an approved automation with that name exists")
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    # Also mirror into registry/capabilities.json as a draft capability.
    save_user_capability(
        Capability(
            id=rid,
            name=rid.replace("_", " "),
            kind="user",
            summary=spec["description"],
            invoke="approve first",
            schedulable=bool(schedule),
            approved=False,
            enabled=False,
            status="draft",
            ritual_id=rid,
            proposed_by="room_automate",
            schedule=schedule,
            steps=tuple(caps[:12]),
            watchlist=tuple(watchlist or []),
        )
    )
    return spec
