"""Team harness workflow runner — posts executable capability steps into room chat.

Used when ``room.config.orchestrator == "workflow"`` (Run harness / Run workflow once).
Debate/chat modes stay in ``specialist_room``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional, Sequence

from messenger.specialist_room import _post, job_registry

logger = logging.getLogger("messenger.team_harness")


def capability_is_executable(cap: Any) -> bool:
    """True when a capability can invoke a runner/action (not prompt-only)."""
    if cap is None:
        return False
    if isinstance(cap, dict):
        runner = cap.get("runner")
        action = cap.get("action")
        cid = str(cap.get("id") or "")
        steps = cap.get("steps") or ()
    else:
        runner = getattr(cap, "runner", None)
        action = getattr(cap, "action", None)
        cid = str(getattr(cap, "id", "") or "")
        steps = getattr(cap, "steps", ()) or ()
    if cid == "web_research":
        return True
    if runner or action:
        return True
    # User drafts may store capability ids in steps
    return bool(steps)


def enrich_capability_public(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["executable"] = capability_is_executable(row)
    out["execution_kind"] = (
        "executable" if out["executable"] else "prompt_only"
    )
    return out


def _room_config(room: dict[str, Any]) -> dict[str, Any]:
    config = room.get("config") if isinstance(room.get("config"), dict) else {}
    if not config and room.get("config_json"):
        try:
            config = json.loads(room["config_json"])
        except Exception:
            config = {}
    return config if isinstance(config, dict) else {}


def _allowlisted_caps(config: dict[str, Any]) -> List[str]:
    skills = config.get("skills") or []
    if not isinstance(skills, list):
        return []
    return [str(s).strip() for s in skills if str(s).strip()][:20]


def _loops_for_room(ledger: Any, room_id: str) -> List[Dict[str, Any]]:
    """Ritual specs bound to this room (capability loops)."""
    from analyst_ledger.paths import ritual_specs_dir

    out: List[Dict[str, Any]] = []
    root = ritual_specs_dir()
    if not root.exists():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(spec, dict):
            continue
        if str(spec.get("room_id") or "") != str(room_id):
            continue
        out.append(spec)
    return out


def _resolve_step_ids(config: dict[str, Any], room_id: str, ledger: Any) -> List[str]:
    """Ordered executable capability ids: first bound loop steps, else allowlist."""
    loops = _loops_for_room(ledger, room_id)
    for spec in loops:
        caps = spec.get("capability_ids") or []
        if isinstance(caps, list) and caps:
            return [str(c).strip() for c in caps if str(c).strip()][:12]
        steps = spec.get("steps") or []
        ids: List[str] = []
        for step in steps:
            if isinstance(step, dict) and step:
                ids.append(str(next(iter(step))))
            elif isinstance(step, str) and step.strip():
                ids.append(step.strip())
        if ids:
            return ids[:12]
    return _allowlisted_caps(config)


def _run_capability(
    cap_id: str,
    *,
    ledger: Any,
    stub: bool,
    objective: str,
) -> Dict[str, Any]:
    from analyst_ledger.orchestration import ALLOWED_ACTIONS, ALLOWED_RUNNERS
    from analyst_ledger.registry import get_capability
    from analyst_ledger.runners import RUNNERS

    cap = get_capability(cap_id)
    runner_name = None
    action = None
    if cap is not None:
        runner_name = getattr(cap, "runner", None)
        action = getattr(cap, "action", None)
    if not runner_name and cap_id in ALLOWED_RUNNERS:
        runner_name = cap_id
    if not action and cap_id in ALLOWED_ACTIONS:
        action = cap_id

    if stub:
        return {
            "ok": True,
            "stub": True,
            "capability_id": cap_id,
            "summary": f"(stub) Would run {runner_name or action or cap_id}",
        }

    if runner_name and runner_name in RUNNERS:
        fn = RUNNERS[runner_name]
        try:
            result = fn(
                ledger=ledger,
                ritual_id=cap_id,
                stub=False,
                require_approved=False,
            )
            return {"ok": True, "capability_id": cap_id, "runner": runner_name, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "capability_id": cap_id, "error": str(exc)[:500]}

    if action and action in ALLOWED_ACTIONS:
        try:
            from analyst_ledger.workflow_engine import WorkflowEngine

            # Minimal one-off via temporary allowlisted step list
            engine = WorkflowEngine(ledger)
            # Prefer note_digest-style compact: execute_action if available
            execute = getattr(engine, "_execute_action", None)
            if callable(execute):
                result = execute(action, spec={"watchlist": [], "description": objective}, stub=False)
                return {"ok": True, "capability_id": cap_id, "action": action, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "capability_id": cap_id, "error": str(exc)[:500]}

    if cap_id == "web_research":
        return {
            "ok": True,
            "capability_id": cap_id,
            "summary": "web_research is available via @Analyst research in chat; not a harness step yet.",
        }

    return {
        "ok": False,
        "capability_id": cap_id,
        "error": "not_executable",
        "hint": "Capability is prompt-only or needs a Phase 2 script.",
    }


def start_workflow_job(
    *,
    store: Any,
    hub: Any,
    room: dict[str, Any],
    owner_user_id: str,
    stub: bool = False,
    loop: Any = None,
    continuous: bool = False,
) -> Any:
    """Background job: run allowlisted / bound-loop capabilities into the team chat."""
    import uuid

    from messenger.specialist_room import SpecialistJob

    room_id = str(room.get("room_id") or "")
    if not room_id:
        raise ValueError("room_id required")

    existing = job_registry().active_for_room(room_id)
    if existing:
        raise ValueError("A harness run is already active in this team.")

    config = _room_config(room)
    objective = str(config.get("objective") or "").strip()
    job = SpecialistJob(
        job_id="job_" + uuid.uuid4().hex[:12],
        room_id=room_id,
        action="workflow",
        topic=objective or "team workflow",
        continuous=bool(continuous),
        rounds=20 if continuous else 1,
    )
    job_registry().register(job)

    def work() -> None:
        from messenger.tenancy import user_context

        try:
            with user_context(owner_user_id) as ledger:
                step_ids = _resolve_step_ids(config, room_id, ledger)
                if not step_ids:
                    _post(
                        store,
                        hub,
                        room_id,
                        "Harness",
                        "No executable capabilities on this team. Open Harness and add an allowlist or loop.",
                        loop=loop,
                    )
                    job.status = "completed"
                    return

                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    (
                        f"Workflow started"
                        + (f" — {objective}" if objective else "")
                        + f". Steps: {', '.join(step_ids)}."
                    ),
                    loop=loop,
                )

                rounds = 0
                max_rounds = 20 if continuous else 1
                while rounds < max_rounds:
                    if job.stop_event.is_set():
                        _post(store, hub, room_id, "Harness", "Workflow stopped.", loop=loop)
                        job.status = "stopped"
                        return
                    rounds += 1
                    job.round_num = rounds
                    for cap_id in step_ids:
                        if job.stop_event.is_set():
                            _post(store, hub, room_id, "Harness", "Workflow stopped.", loop=loop)
                            job.status = "stopped"
                            return
                        outcome = _run_capability(
                            cap_id,
                            ledger=ledger,
                            stub=stub,
                            objective=objective,
                        )
                        if outcome.get("ok"):
                            summary = outcome.get("summary")
                            if not summary:
                                compact = outcome.get("result")
                                try:
                                    summary = json.dumps(compact, ensure_ascii=False)[:900]
                                except Exception:
                                    summary = str(compact)[:900]
                            _post(
                                store,
                                hub,
                                room_id,
                                "Harness",
                                f"[{cap_id}] {summary}",
                                loop=loop,
                            )
                        else:
                            _post(
                                store,
                                hub,
                                room_id,
                                "Harness",
                                f"[{cap_id}] failed: {outcome.get('error') or 'unknown'}"
                                + (
                                    f" ({outcome.get('hint')})"
                                    if outcome.get("hint")
                                    else ""
                                ),
                            )
                        job.posted += 1
                    if not continuous:
                        break
                if job.status == "running":
                    _post(store, hub, room_id, "Harness", "Workflow finished.", loop=loop)
                    job.status = "completed"
        except Exception as exc:  # noqa: BLE001
            logger.exception("team harness failed room=%s", room_id)
            try:
                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    f"Workflow error: {exc}",
                    loop=loop,
                )
            except Exception:
                pass
            job.status = "failed"
            job.error = str(exc)[:500]

    threading.Thread(target=work, name=f"harness-{job.job_id}", daemon=True).start()
    return job


def draft_capability_from_description(
    description: str,
    *,
    ledger: Any = None,
    stub: bool = False,
) -> Dict[str, Any]:
    """Map a natural-language need onto allowlisted runners/actions; save as draft."""
    from analyst_ledger.orchestration import ALLOWED_ACTIONS, ALLOWED_RUNNERS, extract_json
    from analyst_ledger.registry import (
        Capability,
        get_capability,
        list_capabilities_public,
        save_user_capability,
    )
    from analyst_ledger.registry import _slug_id  # noqa: PLC0415 — shared slug helper

    text = (description or "").strip()
    if len(text) < 8:
        raise ValueError("description_too_short")

    catalog = []
    for c in list_capabilities_public(ledger=ledger):
        if c.get("kind") == "builtin" or c.get("approved"):
            catalog.append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "summary": c.get("summary"),
                    "runner": c.get("runner"),
                    "action": c.get("action"),
                    "executable": capability_is_executable(c),
                }
            )

    allow = {
        "runners": sorted(ALLOWED_RUNNERS),
        "actions": sorted(ALLOWED_ACTIONS),
        "capabilities": catalog[:40],
    }

    proposal: Optional[Dict[str, Any]] = None
    if stub:
        # Deterministic offline mapping for tests / no model
        lower = text.lower()
        if "filing" in lower or "sec" in lower:
            proposal = {
                "name": "Filings scan",
                "summary": text[:400],
                "capability_ids": ["sec_filings_check"],
                "runner": "sec_filings_check",
                "in_scope": True,
            }
        elif "note" in lower or "digest" in lower:
            proposal = {
                "name": "Note digest",
                "summary": text[:400],
                "capability_ids": ["note_digest"],
                "runner": "note_digest",
                "in_scope": True,
            }
        elif "quote" in lower or "price" in lower or "watchlist" in lower:
            proposal = {
                "name": "Watchlist scan",
                "summary": text[:400],
                "capability_ids": ["morning_yf_scan"],
                "runner": "morning_yf_scan",
                "in_scope": True,
            }
        else:
            proposal = {
                "name": "",
                "summary": text[:400],
                "capability_ids": [],
                "in_scope": False,
                "reason": "No allowlisted runner/action matches; needs Phase 2 custom script.",
            }
    else:
        prompt = (
            "You draft Flyleaf capabilities. Reply with JSON only:\n"
            '{"in_scope":bool,"name":str,"summary":str,"capability_ids":[str],'
            '"runner":str|null,"action":str|null,"reason":str}\n'
            "Use ONLY ids from the allowlist. If the need cannot be met "
            "(e.g. Alibaba scraping, flight APIs, cargo tracking), set "
            "in_scope=false and explain in reason.\n"
            f"Allowlist: {json.dumps(allow)}\n"
            f"User need: {text[:1500]}"
        )
        try:
            from analyst_ledger.synthesize import call_chat_messages

            raw = call_chat_messages(
                [{"role": "user", "content": prompt}],
                max_tokens=800,
            )
            proposal = extract_json(raw if isinstance(raw, str) else str(raw))
        except Exception as exc:  # noqa: BLE001
            # Fall back to stub heuristics if model unavailable
            logger.info("draft-from-prompt model failed: %s", exc)
            return draft_capability_from_description(text, ledger=ledger, stub=True)

    if not isinstance(proposal, dict):
        raise ValueError("invalid_draft_response")

    if not proposal.get("in_scope"):
        return {
            "ok": True,
            "in_scope": False,
            "needs_script": True,
            "reason": str(
                proposal.get("reason")
                or "Cannot satisfy with current allowlisted runners/actions."
            ),
            "description": text[:400],
        }

    name = str(proposal.get("name") or "").strip() or "Draft capability"
    summary = str(proposal.get("summary") or text)[:400]
    caps = [
        str(c).strip()
        for c in (proposal.get("capability_ids") or [])
        if str(c).strip()
    ]
    runner = str(proposal.get("runner") or "").strip() or None
    action = str(proposal.get("action") or "").strip() or None
    if runner and runner not in ALLOWED_RUNNERS:
        runner = None
    if action and action not in ALLOWED_ACTIONS:
        action = None
    # Validate capability ids against allowlist / builtins
    allowed_ids = {c["id"] for c in catalog if c.get("id")}
    caps = [c for c in caps if c in allowed_ids or c in ALLOWED_RUNNERS or c in ALLOWED_ACTIONS]
    if not caps and runner:
        caps = [runner]
    if not caps and action:
        caps = [action]
    if not caps:
        return {
            "ok": True,
            "in_scope": False,
            "needs_script": True,
            "reason": "Model proposed nothing on the allowlist.",
            "description": text[:400],
        }

    rid = _slug_id(name)
    if get_capability(rid) is not None:
        rid = _slug_id(f"{name}_{len(caps)}")
    cap = Capability(
        id=rid,
        name=name[:80],
        kind="user",
        summary=summary,
        invoke="approve first",
        schedulable=bool(runner),
        needs_model=False,
        runner=runner,
        action=action,
        approved=False,
        enabled=False,
        status="draft",
        ritual_id=rid,
        proposed_by="hire_describe",
        steps=tuple(caps[:12]),
    )
    saved = save_user_capability(cap)
    pub = enrich_capability_public(saved.to_public())
    return {
        "ok": True,
        "in_scope": True,
        "needs_script": False,
        "capability": pub,
        "mapped_from": caps[:12],
    }
