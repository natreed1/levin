"""Multi-specialist room orchestration: present, multi-round debate, synthesize.

Runs are designed to continue in a background thread after the requester leaves
the room UI — messages are written to SQLite and broadcast to whoever is still
subscribed. Callers can stop a run via the stop event on the job.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from analyst_ledger.friend_personalities import (
    FriendPersonality,
    resolve_specialists,
    specialists_public,
)

logger = logging.getLogger("messenger.specialist_room")

VALID_ACTIONS = frozenset({"present", "debate", "idea"})
MAX_FIXED_ROUNDS = 5
MAX_CONTINUOUS_ROUNDS = 20  # hard safety cap when looping until stopped


def list_specialists() -> list[dict]:
    return specialists_public()


def _sanitize_body(text: str) -> str:
    """Strip illegal control chars so JSON/WS clients don't choke."""
    return "".join(
        ch if (ord(ch) >= 32 or ch in "\n\r\t") else " " for ch in (text or "")
    )[:2000]


def _broadcast(hub: Any, loop: Any, room_id: str, payload: dict[str, Any]) -> None:
    if hub is None:
        return
    try:
        import asyncio

        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(hub.broadcast(room_id, payload), loop)
    except Exception as exc:  # noqa: BLE001
        logger.debug("broadcast failed: %s", exc)


def _post(
    store: Any,
    hub: Any,
    room_id: str,
    author: str,
    body: str,
    *,
    loop: Any = None,
) -> dict[str, Any]:
    msg = store.add_message(
        author=author, body=_sanitize_body(body), room_id=room_id
    )
    _broadcast(hub, loop, room_id, {"type": "message", "message": msg})
    return msg


def _room_specialists(room: dict[str, Any]) -> List[FriendPersonality]:
    config = room.get("config") if isinstance(room.get("config"), dict) else {}
    if not config and room.get("config_json"):
        import json

        try:
            config = json.loads(room["config_json"])
        except Exception:
            config = {}
    ids = (
        config.get("agents") or config.get("specialists")
        if isinstance(config, dict)
        else None
    )
    return resolve_specialists(ids)


def _recent_work_brief(owner_user_id: Optional[str], *, limit: int = 8) -> str:
    """INTERNAL-only recent notes/sessions for present mode."""
    if not owner_user_id:
        return "(No owner ledger linked to this room.)"
    try:
        from analyst_ledger.redact import redact_text
        from analyst_ledger.schema import Sensitivity, sensitivity_allows_egress
        from analyst_ledger.schema import parse_sensitivity
        from messenger.tenancy import user_context

        lines: list[str] = []
        with user_context(owner_user_id) as ledger:
            sessions = ledger.list_sessions(limit=5)
            for s in sessions:
                title = redact_text(str(s.get("title") or s.get("session_id") or ""))
                lines.append(
                    f"- Session: {title} ({s.get('status')}, {s.get('surface')})"
                )
            events = ledger.list_events(limit=40)
            notes = 0
            for ev in events:
                if ev.get("type") != "note":
                    continue
                try:
                    level = parse_sensitivity(ev.get("sensitivity"))
                except Exception:
                    continue
                if not sensitivity_allows_egress(level, Sensitivity.INTERNAL):
                    continue
                text = redact_text(str((ev.get("payload") or {}).get("text") or ""))
                if not text.strip():
                    continue
                lines.append(f"- Note: {text[:240]}")
                notes += 1
                if notes >= limit:
                    break
        if not lines:
            return "(No recent INTERNAL notes or sessions in the ledger yet.)"
        return "\n".join(lines[: limit + 5])
    except Exception as exc:  # noqa: BLE001
        logger.warning("recent work brief failed: %s", exc)
        return f"(Could not load recent work: {exc})"


def _stub_reply(personality: FriendPersonality, brief: str, mode: str, topic: str) -> str:
    topic_bit = topic.strip() or "the open question"
    if mode == "present":
        return (
            f"**Presentation — {personality.name}**\n"
            f"Take on recent work regarding {topic_bit}:\n{brief[:700]}"
        )
    if mode == "debate":
        return (
            f"**{personality.name}** on “{topic_bit}” "
            f"({personality.role}). Responding to prior turns. (stub)"
        )
    return f"**{personality.name} — idea pass** on “{topic_bit}”."


def _live_reply(
    personality: FriendPersonality,
    *,
    system_extra: str,
    user_prompt: str,
) -> str:
    from analyst_ledger.synthesize import call_chat_messages

    system = (
        f"You are {personality.name} in a specialist research workshop. "
        f"{personality.prompt}\n{system_extra}"
    )
    return call_chat_messages(
        [{"role": "user", "content": user_prompt}],
        max_tokens=700,
        system=system,
        temperature=0.35,
    ).strip()


def _speak(
    personality: FriendPersonality,
    *,
    mode: str,
    topic: str,
    brief: str,
    prior: Sequence[str],
    stub: bool,
    round_num: int = 1,
    rounds_total: int = 1,
    continuous: bool = False,
) -> str:
    round_hint = ""
    if mode == "debate":
        if continuous:
            round_hint = (
                f"This is loop round {round_num} (ongoing until stopped). "
                "Advance the debate; respond to prior turns."
            )
        elif rounds_total > 1:
            if round_num == 1:
                round_hint = f"Opening round 1 of {rounds_total}. State your case."
            elif round_num < rounds_total:
                round_hint = (
                    f"Rebuttal round {round_num} of {rounds_total}. "
                    "Respond to prior turns; tighten or concede."
                )
            else:
                round_hint = (
                    f"Final argument round {round_num} of {rounds_total}. "
                    "Close your case; name what would change your mind."
                )
    user_prompt = (
        f"Topic: {topic or '(none)'}\n"
        f"{round_hint}\n\n"
        f"Ledger brief (INTERNAL only):\n{brief}\n\n"
        f"Prior turns:\n" + ("\n---\n".join(prior[-12:]) if prior else "(none)") + "\n\n"
        "Write your turn now. Plain text, under 350 words. No markdown fences."
    )
    system_extra = (
        f"Mode={mode}. Stay in character as {personality.name} ({personality.role}). "
        "Never invent facts, dates, numbers, or sources. "
        "If evidence is thin, say what is missing."
    )
    if not stub:
        try:
            live = _live_reply(
                personality,
                system_extra=system_extra,
                user_prompt=user_prompt,
            )
            if live:
                prefix = f"**{personality.name}**"
                if mode == "debate":
                    if continuous:
                        prefix = f"**{personality.name}** (loop {round_num})"
                    elif rounds_total > 1:
                        prefix = f"**{personality.name}** (round {round_num}/{rounds_total})"
                return f"{prefix}\n{live}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("live specialist reply failed for %s: %s", personality.id, exc)
            return (
                f"**{personality.name}**\n"
                f"(Live model call failed: {exc}. "
                "Check Model tab — Claude, GPT, or local Ollama tunnel.)"
            )
    return _stub_reply(personality, brief, mode, topic)


@dataclass
class SpecialistJob:
    job_id: str
    room_id: str
    action: str
    topic: str
    continuous: bool = False
    rounds: int = 1
    status: str = "running"  # running | stopped | completed | failed
    round_num: int = 0
    posted: int = 0
    error: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "room_id": self.room_id,
            "action": self.action,
            "topic": self.topic,
            "continuous": self.continuous,
            "rounds": self.rounds,
            "status": self.status,
            "round_num": self.round_num,
            "posted": self.posted,
            "error": self.error,
            "stop_requested": self.stop_event.is_set(),
        }


class SpecialistJobRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, SpecialistJob] = {}
        self._by_room: Dict[str, str] = {}

    def get(self, job_id: str) -> Optional[SpecialistJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for_room(self, room_id: str) -> Optional[SpecialistJob]:
        with self._lock:
            jid = self._by_room.get(room_id)
            if not jid:
                return None
            job = self._jobs.get(jid)
            if job and job.status == "running":
                return job
            return None

    def list_active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                j.public()
                for j in self._jobs.values()
                if j.status == "running"
            ]

    def register(self, job: SpecialistJob) -> None:
        with self._lock:
            existing_id = self._by_room.get(job.room_id)
            if existing_id:
                old = self._jobs.get(existing_id)
                if old and old.status == "running":
                    old.stop_event.set()
            self._jobs[job.job_id] = job
            self._by_room[job.room_id] = job.job_id

    def stop_room(self, room_id: str) -> Optional[SpecialistJob]:
        with self._lock:
            jid = self._by_room.get(room_id)
            job = self._jobs.get(jid) if jid else None
            if job and job.status == "running":
                job.stop_event.set()
                return job
            return job


_REGISTRY = SpecialistJobRegistry()


def job_registry() -> SpecialistJobRegistry:
    return _REGISTRY


def run_specialist_action(
    *,
    store: Any,
    hub: Any,
    room: dict[str, Any],
    action: str,
    topic: str = "",
    owner_user_id: Optional[str] = None,
    stub: bool = False,
    rounds: int = 1,
    continuous: bool = False,
    stop_event: Optional[threading.Event] = None,
    job: Optional[SpecialistJob] = None,
    loop: Any = None,
) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unknown action '{action}'. Expected: present, debate, idea")

    stop = stop_event or threading.Event()
    continuous = bool(continuous) and action == "debate"

    try:
        rounds_n = int(rounds or 1)
    except (TypeError, ValueError):
        rounds_n = 1
    if continuous:
        rounds_n = MAX_CONTINUOUS_ROUNDS
    else:
        rounds_n = max(1, min(rounds_n, MAX_FIXED_ROUNDS))

    room_id = room["room_id"]
    specialists = _room_specialists(room)
    if len(specialists) < 2 and action != "present":
        raise ValueError("Specialist rooms need at least two specialists for debate/idea")
    if not specialists:
        specialists = resolve_specialists(None)

    topic = " ".join(str(topic or "").split())[:400]
    brief = _recent_work_brief(owner_user_id)

    endpoint = None
    if owner_user_id:
        try:
            from messenger.model_link import registry as model_registry

            endpoint = model_registry().endpoint_for_call(owner_user_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("model link lookup failed: %s", exc)

    from analyst_ledger.synthesize import use_llm_endpoint

    with use_llm_endpoint(endpoint):
        return _run_specialist_action_body(
            store=store,
            hub=hub,
            room=room,
            action=action,
            topic=topic,
            brief=brief,
            specialists=specialists,
            stub=stub,
            rounds_n=rounds_n,
            continuous=continuous,
            stop=stop,
            job=job,
            loop=loop,
            room_id=room_id,
        )


def _run_specialist_action_body(
    *,
    store: Any,
    hub: Any,
    room: dict[str, Any],
    action: str,
    topic: str,
    brief: str,
    specialists: List[FriendPersonality],
    stub: bool,
    rounds_n: int,
    continuous: bool,
    stop: threading.Event,
    job: Optional[SpecialistJob],
    loop: Any,
    room_id: str,
) -> dict[str, Any]:
    if continuous:
        intro = (
            f"⚖ **Specialist loop** — “{topic or 'open question'}”\n"
            f"Sides: {', '.join(p.mention for p in specialists)}. "
            "They will keep debating until you Stop — safe to leave this room; "
            "turns keep posting here."
        )
    elif action == "debate":
        intro = (
            f"⚖ **Specialist debate** — “{topic or 'open question'}”\n"
            f"Sides: {', '.join(p.mention for p in specialists)}. "
            f"{rounds_n} round{'s' if rounds_n != 1 else ''} of argument, "
            "then synthesizer closes. Safe to leave — turns keep posting."
        )
    elif action == "present":
        intro = (
            f"🎙 **Specialist presentations**"
            + (f" — focus: {topic}" if topic else " — recent ledger work")
            + f"\nRoster: {', '.join(p.mention for p in specialists)}"
        )
    else:
        intro = f"💡 **Idea pass** from the roster on “{topic or 'open question'}”"

    _post(store, hub, room_id, "Moderator", intro, loop=loop)
    time.sleep(0.15)

    speakers = [p for p in specialists if p.role != "synthesizer"]
    synth = [p for p in specialists if p.role == "synthesizer"]
    if not speakers and action == "debate":
        speakers = list(specialists)

    prior: list[str] = []
    posted = 0
    stopped_early = False

    def _stopped() -> bool:
        return stop.is_set()

    if action == "present":
        for personality in specialists:
            if _stopped():
                stopped_early = True
                break
            body = _speak(
                personality,
                mode=action,
                topic=topic,
                brief=brief,
                prior=prior,
                stub=stub,
            )
            _post(store, hub, room_id, personality.name, body, loop=loop)
            prior.append(f"{personality.name}: {body}")
            posted += 1
            if job:
                job.posted = posted
            time.sleep(0.2)
    elif action == "idea":
        order = synth or specialists
        for personality in order:
            if _stopped():
                stopped_early = True
                break
            body = _speak(
                personality,
                mode=action,
                topic=topic,
                brief=brief,
                prior=prior,
                stub=stub,
            )
            _post(store, hub, room_id, personality.name, body, loop=loop)
            prior.append(f"{personality.name}: {body}")
            posted += 1
            if job:
                job.posted = posted
            time.sleep(0.2)
    else:
        for round_num in range(1, rounds_n + 1):
            if _stopped():
                stopped_early = True
                break
            if job:
                job.round_num = round_num
            label = (
                f"— Loop {round_num} (ongoing; Stop anytime) —"
                if continuous
                else f"— Round {round_num}/{rounds_n} —"
            )
            _post(store, hub, room_id, "Moderator", label, loop=loop)
            time.sleep(0.1)
            for personality in speakers:
                if _stopped():
                    stopped_early = True
                    break
                body = _speak(
                    personality,
                    mode="debate",
                    topic=topic,
                    brief=brief,
                    prior=prior,
                    stub=stub,
                    round_num=round_num,
                    rounds_total=rounds_n,
                    continuous=continuous,
                )
                _post(store, hub, room_id, personality.name, body, loop=loop)
                prior.append(f"{personality.name} (r{round_num}): {body}")
                posted += 1
                if job:
                    job.posted = posted
                time.sleep(0.2)
            if stopped_early:
                break
            # Mini synthesis every round when continuous, so leaving still yields ideas
            if continuous and synth:
                if _stopped():
                    stopped_early = True
                    break
                body = _speak(
                    synth[0],
                    mode="debate",
                    topic=topic,
                    brief=brief,
                    prior=prior,
                    stub=stub,
                    round_num=round_num,
                    rounds_total=rounds_n,
                    continuous=True,
                )
                _post(store, hub, room_id, synth[0].name, body, loop=loop)
                prior.append(f"{synth[0].name} (r{round_num}): {body}")
                posted += 1
                if job:
                    job.posted = posted
                time.sleep(0.2)

        if not continuous and not stopped_early:
            synth_p = synth[0] if synth else resolve_specialists(["qwen-synthesizer"])[0]
            body = _speak(
                synth_p,
                mode="debate",
                topic=topic,
                brief=brief,
                prior=prior,
                stub=stub,
                round_num=rounds_n,
                rounds_total=rounds_n,
            )
            _post(store, hub, room_id, synth_p.name, body, loop=loop)
            posted += 1
            if job:
                job.posted = posted

    if stopped_early:
        _post(
            store,
            hub,
            room_id,
            "Moderator",
            f"⏹ Stopped after {posted} posts"
            + (f" (round {job.round_num if job else '?'})." if action == "debate" else "."),
            loop=loop,
        )
        if job:
            job.status = "stopped"
    else:
        if continuous:
            _post(
                store,
                hub,
                room_id,
                "Moderator",
                f"Loop hit safety cap ({MAX_CONTINUOUS_ROUNDS} rounds). Stopped.",
                loop=loop,
            )
        if job and job.status == "running":
            job.status = "completed"

    if job:
        job.posted = posted

    return {
        "ok": True,
        "action": action,
        "posted": posted,
        "rounds": rounds_n if action == "debate" else 1,
        "continuous": continuous,
        "stopped": stopped_early,
        "specialists": [p.id for p in specialists],
        "topic": topic,
        "job_id": job.job_id if job else None,
    }


def start_specialist_job(
    *,
    store: Any,
    hub: Any,
    room: dict[str, Any],
    action: str,
    topic: str = "",
    owner_user_id: Optional[str] = None,
    stub: bool = False,
    rounds: int = 1,
    continuous: bool = False,
    loop: Any = None,
) -> SpecialistJob:
    """Register + spawn a background thread; returns immediately."""
    continuous = bool(continuous) and str(action or "").strip().lower() == "debate"
    job = SpecialistJob(
        job_id="job_" + uuid.uuid4().hex[:12],
        room_id=room["room_id"],
        action=action,
        topic=topic,
        continuous=continuous,
        rounds=MAX_CONTINUOUS_ROUNDS if continuous else int(rounds or 1),
    )
    _REGISTRY.register(job)

    def work() -> None:
        try:
            run_specialist_action(
                store=store,
                hub=hub,
                room=room,
                action=action,
                topic=topic,
                owner_user_id=owner_user_id,
                stub=stub,
                rounds=rounds,
                continuous=continuous,
                stop_event=job.stop_event,
                job=job,
                loop=loop,
            )
            if job.status == "running":
                job.status = "completed"
        except Exception as exc:  # noqa: BLE001
            logger.exception("specialist job failed")
            job.status = "failed"
            job.error = str(exc)
            try:
                _post(
                    store,
                    hub,
                    room["room_id"],
                    "Moderator",
                    f"Specialist run failed: {exc}",
                    loop=loop,
                )
            except Exception:
                pass

    threading.Thread(
        target=work, name=f"specialist-{job.job_id}", daemon=True
    ).start()
    return job
