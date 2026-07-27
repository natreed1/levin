"""In-process agent hooks for People-room mentions (@Analyst / @workflow).

Replaces the HTTP hop through messenger_bridge for the unified app: when a
message mentions an agent, we post a reply into the same room via the store +
RoomHub, optionally kicking a WorkflowEngine run against the room owner's ledger.

Agent mentions are role names (@Bullish, @Contrarian, …). Which model answers
(Claude, GPT, local open-source) comes from Settings or the room model toggle.
Legacy @Qwen* aliases still work.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("messenger.agent_hooks")

_WORKFLOW_RE = re.compile(
    r"(?<!\w)@workflow\s+([a-zA-Z0-9][a-zA-Z0-9_-]{0,120})\b", re.I
)


def _broadcast(hub: Any, loop: Any, room_id: str, payload: dict[str, Any]) -> None:
    if hub is None:
        return
    try:
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(hub.broadcast(room_id, payload), loop)
        else:
            # Best-effort sync fallback (tests / no loop).
            try:
                asyncio.get_event_loop().run_until_complete(
                    hub.broadcast(room_id, payload)
                )
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("broadcast failed: %s", exc)


def handle_room_mention(
    *,
    store: Any,
    hub: Any,
    room_id: str,
    author: str,
    body: str,
    owner_user_id: Optional[str],
    loop: Any = None,
    stub: bool = False,
) -> None:
    text = body or ""
    # Team TF / Orchestrator entry (simple vs complex). Explicit @workflow still
    # runs below; single @mentions may short-circuit to the classic reply path.
    routed = handle_team_dispatch(
        store=store,
        hub=hub,
        room_id=room_id,
        author=author,
        body=text,
        owner_user_id=owner_user_id,
        loop=loop,
        stub=stub,
    )
    if routed:
        match = _WORKFLOW_RE.search(text)
        if match and owner_user_id and routed.get("path") != "automation":
            # Explicit @workflow still honored alongside TF automation.
            _kick_workflow(
                store,
                hub,
                room_id,
                owner_user_id,
                match.group(1),
                text,
                loop=loop,
            )
        return

    # Legacy fallback: any @token may be a roster agent (builtin or composed).
    if re.search(r"(?<!\w)@[A-Za-z0-9]", text or ""):
        _reply_qwen(
            store,
            hub,
            room_id,
            author,
            text,
            owner_user_id=owner_user_id,
            loop=loop,
        )
    match = _WORKFLOW_RE.search(text)
    if match and owner_user_id:
        _kick_workflow(
            store,
            hub,
            room_id,
            owner_user_id,
            match.group(1),
            text,
            loop=loop,
        )


def handle_team_dispatch(
    *,
    store: Any,
    hub: Any,
    room_id: str,
    author: str,
    body: str,
    owner_user_id: Optional[str],
    loop: Any = None,
    stub: bool = False,
) -> Optional[dict[str, Any]]:
    """TF / Orchestrator routing for rostered teams. Returns decision public or None."""
    text = body or ""
    roster_ids = _room_roster_ids(store, room_id)
    # Without a roster, keep @-only legacy behavior (caller falls through).
    if roster_ids is None and not re.search(r"(?<!\w)@[A-Za-z0-9]", text):
        return None
    # Empty explicit roster → nothing to route to
    if roster_ids is not None and len(roster_ids) == 0 and "@" not in text:
        return None

    try:
        from analyst_ledger.registry import get_agent, list_agents
        from messenger.team_router import route_team_message, team_router_enabled
    except Exception as exc:  # noqa: BLE001
        logger.debug("team router import failed: %s", exc)
        return None

    if not team_router_enabled() and "@" not in text:
        return None

    def _load_roster() -> list:
        agents = []
        ids = roster_ids
        if ids is None:
            # Unscoped room with @: allow palette agents
            agents = [a for a in list_agents() if a.room_palette and a.prompt]
        else:
            for aid in ids:
                a = get_agent(aid)
                if a is not None:
                    agents.append(a)
            # Always allow Orchestrator on complex asks
            orch = get_agent("orchestrator")
            if orch is not None and all(a.id != "orchestrator" for a in agents):
                agents.append(orch)
        return agents

    if owner_user_id:
        try:
            from messenger.tenancy import user_context

            with user_context(owner_user_id):
                roster = _load_roster()
                decision = route_team_message(text, roster_agents=roster)
        except Exception as exc:  # noqa: BLE001
            logger.warning("team route failed: %s", exc)
            return None
    else:
        roster = _load_roster()
        decision = route_team_message(text, roster_agents=roster)

    path = decision.path
    pub = decision.public()
    has_at = bool(re.search(r"(?<!\w)@[A-Za-z0-9]", text or ""))

    # Casual room chatter without @: stay silent (no clarify spam).
    if path == "clarify" and not has_at:
        return None

    # Path chip for TF agent/automation only — Orchestrator uses the run banner.
    if decision.chip and path in {"agent", "automation"}:
        try:
            from messenger.specialist_room import _post as post_msg

            post_msg(
                store,
                hub,
                room_id,
                "Flyleaf",
                f"`{decision.chip}`",
                loop=loop,
            )
        except Exception:
            pass

    if path == "mention":
        # Classic multi-mention / single-@ reply path
        _reply_qwen(
            store,
            hub,
            room_id,
            author,
            text,
            owner_user_id=owner_user_id,
            loop=loop,
        )
        return pub

    if path == "agent" and decision.agent_id:
        # Synthesize a single-agent mention so research-first path still applies
        agent = next((a for a in roster if a.id == decision.agent_id), None)
        mention = (
            agent.mention
            if agent and getattr(agent, "mention", "").startswith("@")
            else f"@{decision.agent_name or decision.agent_id}"
        )
        synthetic = f"{mention} {text}"
        _reply_qwen(
            store,
            hub,
            room_id,
            author,
            synthetic,
            owner_user_id=owner_user_id,
            loop=loop,
        )
        return pub

    if path == "automation" and decision.ritual_id:
        if not owner_user_id:
            msg = store.add_message(
                author="Flyleaf",
                body=(
                    f"Matched automation `{decision.ritual_id}` but this room "
                    "has no owner ledger — can't run it."
                ),
                room_id=room_id,
            )
            _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
            return pub
        _kick_workflow(
            store,
            hub,
            room_id,
            owner_user_id,
            decision.ritual_id,
            text,
            loop=loop,
        )
        return pub

    if path == "orchestrator":
        room = _room_dict(store, room_id) or {}
        config = room.get("config") if isinstance(room.get("config"), dict) else {}
        skills = list(config.get("skills") or []) if isinstance(config, dict) else []
        try:
            from messenger.team_orchestrator import start_orchestrator_job

            start_orchestrator_job(
                store=store,
                hub=hub,
                room_id=room_id,
                author=author,
                text=text,
                roster=roster,
                owner_user_id=owner_user_id,
                loop=loop,
                room_skills=skills,
                harness=config if isinstance(config, dict) else None,
                stub=stub,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("orchestrator start failed: %s", exc)
            msg = store.add_message(
                author="Orchestrator",
                body=f"Couldn't start Orchestrator: {exc}",
                room_id=room_id,
            )
            _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
        return pub

    if path == "clarify":
        msg = store.add_message(
            author="Flyleaf",
            body=(
                "Not sure who should take that — @mention an agent, "
                "ask @Orchestrator to organize the team, or name an approved automation."
            ),
            room_id=room_id,
        )
        _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
        return pub

    return None


def _room_dict(store: Any, room_id: str) -> Optional[dict]:
    try:
        room = store.room(room_id) if store is not None else None
    except Exception:
        return None
    return room if isinstance(room, dict) else None


def _room_model_profile_id(store: Any, room_id: str) -> Optional[str]:
    room = _room_dict(store, room_id)
    if room is None:
        return None
    raw = (room.get("config") or {}).get("model_profile_id")
    return str(raw).strip() or None if raw else None


def _room_roster_ids(store: Any, room_id: str) -> Optional[list[str]]:
    """Return configured room agent ids, or None when the room has no roster key."""
    room = _room_dict(store, room_id)
    if room is None:
        return None
    config = room.get("config") if isinstance(room.get("config"), dict) else {}
    if not isinstance(config, dict):
        return None
    if "agents" not in config and "specialists" not in config:
        return None
    raw = config.get("agents") or config.get("specialists") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _resolve_mentioned(
    text: str,
    *,
    roster_ids: Optional[list[str]],
) -> list:
    from analyst_ledger.friend_personalities import mentioned_personalities

    extras = roster_ids if roster_ids is not None else ()
    found = mentioned_personalities(text, extra_agent_ids=extras)
    if roster_ids is None:
        return found
    allowed = set(roster_ids)
    return [p for p in found if p.id in allowed]


def _reply_qwen(
    store: Any,
    hub: Any,
    room_id: str,
    author: str,
    text: str,
    *,
    owner_user_id: Optional[str] = None,
    loop: Any = None,
) -> None:
    """Post in-room specialist replies for each mentioned personality."""
    roster_ids = _room_roster_ids(store, room_id)
    personalities: list = []
    try:
        if owner_user_id:
            from messenger.tenancy import user_context

            with user_context(owner_user_id):
                personalities = _resolve_mentioned(text, roster_ids=roster_ids)
        else:
            personalities = _resolve_mentioned(text, roster_ids=roster_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mention resolve failed: %s", exc)
        personalities = []

    try:
        from analyst_ledger.friend_personalities import (
            at_mention_tokens,
            strip_personality_mentions,
        )

        snippet = strip_personality_mentions(
            text, extra_agent_ids=roster_ids if roster_ids is not None else ()
        )
        at_tokens = [
            t for t in at_mention_tokens(text) if t.casefold() != "@workflow"
        ]
    except Exception:
        snippet = text
        at_tokens = []
    snippet = " ".join(snippet.split())[:800] or "hello"

    if not personalities:
        # Don't fall back to Analyst — that hid "custom agent never replied".
        if not at_tokens:
            return
        if roster_ids is not None:
            labels = ", ".join(at_tokens[:4])
            body = (
                f"{labels} isn't assigned to this team. "
                "Open Harness → Roles and add them to the roster, then @mention again."
            )
        else:
            labels = ", ".join(at_tokens[:4])
            body = (
                f"Couldn't find an agent for {labels}. "
                "Add them to this team's agents in Harness, then try again."
            )
        msg = store.add_message(author="Flyleaf", body=body[:2000], room_id=room_id)
        _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
        return

    endpoint = None
    profile_id = _room_model_profile_id(store, room_id)
    if owner_user_id:
        try:
            from messenger.model_link import registry as model_registry

            endpoint = model_registry().endpoint_for_call(
                owner_user_id,
                profile_id=profile_id,
            )
        except Exception:
            endpoint = None

    try:
        from analyst_ledger.synthesize import call_chat_messages, use_llm_endpoint
    except Exception:
        call_chat_messages = None  # type: ignore
        use_llm_endpoint = None  # type: ignore

    wants_research = False
    try:
        from analyst_ledger.friend_qwen import _is_research_request
        from analyst_ledger.registry import agent_has_capability, get_capability

        wants_research = bool(_is_research_request(text))
        # Research is the web_research capability — only agents that own it
        # (e.g. Analyst) may run it. Lenses like Bullish stay prompt-only.
        if wants_research and get_capability("web_research") is None:
            wants_research = False
    except Exception:
        wants_research = False

    context_text = ""
    if wants_research:
        try:
            recent = store.list_messages(limit=12, room_id=room_id) or []
            lines = []
            for m in recent:
                a = str((m or {}).get("author") or "?")
                b = str((m or {}).get("body") or "").strip()
                if b:
                    lines.append(f"{a}: {b}")
            context_text = "\n".join(lines[-12:])
        except Exception:
            context_text = f"{author}: {text}"

    room_guidance = ""
    try:
        room = store.room(room_id) if store is not None else None
        if isinstance(room, dict):
            from messenger.specialist_room import _room_guidance

            room_guidance = _room_guidance(room)
    except Exception:
        room_guidance = ""

    def _unavailable(exc: BaseException) -> str:
        return (
            f"(Live model unavailable: {exc}. "
            "Agents use whichever model this room has selected — "
            "switch the room model dropdown to Claude, or Start local model.)"
        )

    def _chat_once(personality: Any, *, ep: Any, research_context: str = "") -> str:
        if call_chat_messages is None or use_llm_endpoint is None:
            return ""
        research_bit = ""
        if research_context:
            research_bit = (
                "\nTeammate research already in this thread (use it; do not invent "
                "sources):\n"
                f"{research_context[:3500]}\n"
            )
        with use_llm_endpoint(ep):
            return call_chat_messages(
                [
                    {
                        "role": "user",
                        "content": (
                            f"{author} said in the room:\n{snippet}\n\n"
                            f"{research_bit}"
                            "Reply in character, briefly, in plain text."
                        ),
                    }
                ],
                max_tokens=500,
                system=(
                    f"You are {personality.name} in a chat room. "
                    f"{personality.prompt} Never invent facts. "
                    + (
                        f"\n{room_guidance}\nHonor the room objective and prompts."
                        if room_guidance
                        else ""
                    )
                    + (
                        " Use the teammate research above when discussing. "
                        "If evidence is still missing, say what is unknown — "
                        "do not claim you need a separate research pass."
                        if research_context
                        else (
                            " If the user asks for current news, filings, or a live "
                            "lookup and you were not asked to research it yourself, "
                            "discuss the question with clear uncertainty — do not "
                            "invent sources or demand a research pass."
                        )
                    )
                ),
                temperature=0.35,
            ).strip()

    def _research_once(personality: Any, *, ep: Any) -> str:
        from analyst_ledger.friend_qwen import compose_research_reply

        if not ep:
            raise RuntimeError(
                "No model linked for this room. Select Claude (or another model) "
                "in the room model menu, or Start local model."
            )
        if use_llm_endpoint is None:
            return compose_research_reply(
                text, context_text=context_text, personality=personality
            )
        with use_llm_endpoint(ep):
            return compose_research_reply(
                text, context_text=context_text, personality=personality
            )

    def _one(
        personality: Any, *, ep: Any, research: bool, research_context: str = ""
    ) -> str:
        if not research and (call_chat_messages is None or use_llm_endpoint is None):
            return ""
        try:
            if research:
                return _research_once(personality, ep=ep)
            return _chat_once(
                personality, ep=ep, research_context=research_context
            )
        except Exception as exc:  # noqa: BLE001
            # Stale trycloudflare / dead gateway: recover via Companion once, then retry.
            if owner_user_id and ep is not None:
                try:
                    from messenger import settings_models

                    if settings_models.is_local_route_failure(ep, exc):
                        recovered = settings_models.ensure_local_route(
                            owner_user_id,
                            profile_id,
                            force_recover=True,
                        )
                        if recovered.get("reachable") and recovered.get("endpoint"):
                            logger.info(
                                "recovered local model route for %s after: %s",
                                owner_user_id,
                                exc,
                            )
                            new_ep = recovered["endpoint"]
                            if research:
                                return _research_once(personality, ep=new_ep)
                            return _chat_once(
                                personality,
                                ep=new_ep,
                                research_context=research_context,
                            )
                except Exception as retry_exc:  # noqa: BLE001
                    return _unavailable(retry_exc)
            if research:
                return f"Couldn't finish research: {exc}"
            return _unavailable(exc)

    # Research-capable agents run first so discuss-only teammates can use the pass.
    research_capable: set[str] = set()
    if wants_research:
        try:
            from analyst_ledger.registry import agent_has_capability

            for p in personalities:
                if agent_has_capability(p.id, "web_research"):
                    research_capable.add(p.id)
        except Exception:
            for p in personalities:
                if p.id == "qwen":
                    research_capable.add(p.id)

    research_notes: list[str] = []
    ordered = sorted(
        personalities,
        key=lambda p: (0 if p.id in research_capable else 1, p.name or p.id),
    )

    for personality in ordered:
        do_research = wants_research and personality.id in research_capable
        if do_research:
            ack = store.add_message(
                author=personality.name,
                body="On it — researching…",
                room_id=room_id,
            )
            _broadcast(hub, loop, room_id, {"type": "message", "message": ack})

        research_context = "\n\n".join(research_notes[-3:]) if research_notes else ""
        reply = (
            _one(
                personality,
                ep=endpoint,
                research=do_research,
                research_context=research_context,
            )
            if (call_chat_messages is not None or do_research)
            else ""
        )
        if not reply:
            reply = (
                f"Noted, {author}. You said: {snippet!r}. "
                "(No model linked — open Settings to connect Claude or a local model; "
                "@Bullish runs on whichever model this room selects.)"
            )
        if do_research and reply:
            research_notes.append(f"{personality.name}: {reply[:1200]}")
        msg = store.add_message(
            author=personality.name, body=reply[:2000], room_id=room_id
        )
        _broadcast(hub, loop, room_id, {"type": "message", "message": msg})


def _kick_workflow(
    store: Any,
    hub: Any,
    room_id: str,
    owner_user_id: str,
    ritual_id: str,
    request_text: str,
    *,
    loop: Any = None,
) -> None:
    try:
        from messenger.tenancy import user_context
        from analyst_ledger.rituals import _validate_ritual_id, list_automations
        from analyst_ledger.workflow_engine import WorkflowEngine

        ritual_id = _validate_ritual_id(ritual_id)
        with user_context(owner_user_id) as ledger:
            approved = {
                a["ritual_id"]
                for a in list_automations(ledger)
                if a.get("approved") and a.get("enabled", True)
            }
            if ritual_id not in approved:
                msg = store.add_message(
                    author="Workflow",
                    body=(
                        f"Skill '{ritual_id}' is not approved/enabled for this "
                        "account. Approve it under Agents → Capabilities, then add "
                        "it to a room’s skills or an agent."
                    ),
                    room_id=room_id,
                )
                _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
                return
            started = store.add_message(
                author="Workflow",
                body=f"Ran `{ritual_id}`…",
                room_id=room_id,
            )
            _broadcast(hub, loop, room_id, {"type": "message", "message": started})
            try:
                result = WorkflowEngine(ledger).run(
                    ritual_id, request=request_text, stub=True
                )
                summary = (
                    (result or {}).get("summary")
                    or (result or {}).get("final")
                    or str(result)[:500]
                )
                done = store.add_message(
                    author="Workflow",
                    body=f"Ran `{ritual_id}` — finished.\n{summary}",
                    room_id=room_id,
                )
                _broadcast(hub, loop, room_id, {"type": "message", "message": done})
            except Exception as exc:  # noqa: BLE001
                err = store.add_message(
                    author="Workflow",
                    body=f"`{ritual_id}` failed: {exc}",
                    room_id=room_id,
                )
                _broadcast(hub, loop, room_id, {"type": "message", "message": err})
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow kick failed: %s", exc)
