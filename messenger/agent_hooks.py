"""In-process agent hooks for People-room mentions (@Qwen / @workflow).

Replaces the HTTP hop through messenger_bridge for the unified app: when a
message mentions an agent, we post a reply into the same room via the store +
RoomHub, optionally kicking a WorkflowEngine run against the room owner's ledger.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("messenger.agent_hooks")

_QWEN_RE = re.compile(
    r"(?<!\w)@qwen(?:-contrarian|-bull|-synthesizer)?\b", re.I
)
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
    if _QWEN_RE.search(text):
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

    snippet = _QWEN_RE.sub("", text)
    snippet = " ".join(snippet.split())[:800] or "hello"
    if not personalities:
        personalities_fallback_name = "Qwen"
        reply = f"[{personalities_fallback_name}] Noted, {author}."
        msg = store.add_message(
            author=personalities_fallback_name, body=reply[:2000], room_id=room_id
        )
        _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
        return

    endpoint = None
    if owner_user_id:
        try:
            from messenger.model_link import registry as model_registry

            endpoint = model_registry().endpoint_for_call(owner_user_id)
        except Exception:
            endpoint = None

    try:
        from analyst_ledger.synthesize import call_chat_messages, use_llm_endpoint
    except Exception:
        call_chat_messages = None  # type: ignore
        use_llm_endpoint = None  # type: ignore

    def _one(personality: Any) -> str:
        if call_chat_messages is None:
            return ""
        try:
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
                    f"{personality.prompt} Never invent facts."
                ),
                temperature=0.35,
            ).strip()
        except Exception as exc:  # noqa: BLE001
            return (
                f"(Live model unavailable: {exc}. "
                "Add Claude, GPT, or a local tunnel under Model.)"
            )

    for personality in personalities:
        reply = None
        if use_llm_endpoint is not None and call_chat_messages is not None:
            with use_llm_endpoint(endpoint):
                reply = _one(personality)
        if not reply:
            reply = (
                f"Noted, {author}. You said: {snippet!r}. "
                "(No model linked — open the Model tab to connect Claude, GPT, or Ollama.)"
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
                        f"Workflow '{ritual_id}' is not approved/enabled for this "
                        "account. Approve it under Automations first."
                    ),
                    room_id=room_id,
                )
                _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
                return
            started = store.add_message(
                author="Workflow",
                body=f"Starting workflow `{ritual_id}`…",
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
                    body=f"`{ritual_id}` finished.\n{summary}",
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
