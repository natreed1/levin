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
) -> None:
    text = body or ""
    try:
        from analyst_ledger.friend_personalities import MENTION_RE

        agent_mentioned = bool(MENTION_RE.search(text))
    except Exception:
        agent_mentioned = False
    if agent_mentioned:
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


def _room_model_profile_id(store: Any, room_id: str) -> Optional[str]:
    try:
        room = store.room(room_id) if store is not None else None
    except Exception:
        room = None
    if not isinstance(room, dict):
        return None
    raw = (room.get("config") or {}).get("model_profile_id")
    return str(raw).strip() or None if raw else None


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
    personalities = []
    try:
        from analyst_ledger.friend_personalities import mentioned_personalities

        personalities = mentioned_personalities(text)
    except Exception:
        personalities = []
    if not personalities:
        try:
            from analyst_ledger.friend_personalities import DEFAULT_PERSONALITY

            personalities = [DEFAULT_PERSONALITY]
        except Exception:
            personalities = []

    try:
        from analyst_ledger.friend_personalities import strip_personality_mentions

        snippet = strip_personality_mentions(text)
    except Exception:
        snippet = text
    snippet = " ".join(snippet.split())[:800] or "hello"
    if not personalities:
        personalities_fallback_name = "Analyst"
        reply = f"[{personalities_fallback_name}] Noted, {author}."
        msg = store.add_message(
            author=personalities_fallback_name, body=reply[:2000], room_id=room_id
        )
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

    def _chat_once(personality: Any, *, ep: Any) -> str:
        if call_chat_messages is None or use_llm_endpoint is None:
            return ""
        with use_llm_endpoint(ep):
            return call_chat_messages(
                [
                    {
                        "role": "user",
                        "content": (
                            f"{author} said in the room:\n{snippet}\n\n"
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
                    + " If the user asks for current news, filings, or a live lookup, "
                    "say you need a research pass rather than inventing sources."
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

    def _one(personality: Any, *, ep: Any, research: bool) -> str:
        if not research and (call_chat_messages is None or use_llm_endpoint is None):
            return ""
        try:
            if research:
                return _research_once(personality, ep=ep)
            return _chat_once(personality, ep=ep)
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
                            return _chat_once(personality, ep=new_ep)
                except Exception as retry_exc:  # noqa: BLE001
                    return _unavailable(retry_exc)
            if research:
                return f"Couldn't finish research: {exc}"
            return _unavailable(exc)

    for personality in personalities:
        do_research = wants_research
        if do_research:
            try:
                from analyst_ledger.registry import agent_has_capability

                do_research = agent_has_capability(personality.id, "web_research")
            except Exception:
                do_research = personality.id == "qwen"
        if do_research:
            ack = store.add_message(
                author=personality.name,
                body="On it — researching…",
                room_id=room_id,
            )
            _broadcast(hub, loop, room_id, {"type": "message", "message": ack})

        reply = (
            _one(personality, ep=endpoint, research=do_research)
            if (call_chat_messages is not None or do_research)
            else ""
        )
        if not reply:
            reply = (
                f"Noted, {author}. You said: {snippet!r}. "
                "(No model linked — open Settings to connect Claude or a local model; "
                "@Bullish runs on whichever model this room selects.)"
            )
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
