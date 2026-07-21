"""Agentic (workflow) chat APIs — local per-user ledger threads."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from messenger.deps import current_user
from messenger.tenancy import user_context

router = APIRouter(prefix="/api/agent-chats", tags=["agent-chats"])


def _jobs(request: Request) -> Any:
    return request.app.state.jobs


@router.get("")
def list_threads(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    with user_context(user["user_id"]) as ledger:
        # Ensure the master thread always exists for the Agents rail.
        ledger.get_or_create_chat_thread(master=True)
        threads = ledger.list_chat_threads()
        return JSONResponse({"ok": True, "threads": threads})


@router.get("/messages")
def list_messages(
    thread_id: str,
    limit: int = 300,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    with user_context(user["user_id"]) as ledger:
        session = ledger.get_session(thread_id)
        if not session or session.surface != "chat":
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        messages = ledger.list_chat_messages(thread_id, limit=limit)
        return JSONResponse(
            {
                "ok": True,
                "thread_id": thread_id,
                "messages": messages,
                "title": session.title,
                "desk_tag": session.desk_tag,
            }
        )


@router.post("/message")
async def post_message(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.rituals import _validate_ritual_id, list_automations
    from analyst_ledger.router import execute_routed_run, route_message, router_enabled
    from analyst_ledger.workflow_engine import MasterCoordinator, WorkflowEngine

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    thread_id = str((data or {}).get("thread_id") or "")
    content = str((data or {}).get("content") or "").strip()
    stub = bool((data or {}).get("stub", False))
    if not content:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    try:
        with user_context(user["user_id"]) as ledger:
            session = ledger.get_session(thread_id)
            if not session or session.surface != "chat":
                return JSONResponse(
                    {"ok": False, "error": "not_found"}, status_code=404
                )
            user_event = ledger.append_chat_message(
                thread_id, role="user", content=content
            )
            jobs = _jobs(request)
            uid = user["user_id"]

            # Deterministic chat layer (file finder → ritual router → model).
            if router_enabled():
                try:
                    from analyst_ledger.file_search import (
                        execute_file_search,
                        match_file_request,
                    )
                    from analyst_ledger.paths import file_search_roots

                    fquery = match_file_request(content)
                    roots = file_search_roots()
                except Exception:  # noqa: BLE001 — must never break chat
                    fquery, roots = None, []
                if fquery is not None and roots:
                    fq = fquery
                    job = jobs.start(
                        f"user:{uid}:file_search",
                        "file_search",
                        lambda job: execute_file_search(
                            ledger, thread_id, fq, stub=stub
                        ),
                    )
                    return JSONResponse({"ok": True, "job": job.public()})
                decision = None
                try:
                    restrict = None
                    if session.desk_tag != "chat:master":
                        restrict = str(session.desk_tag or "").removeprefix("chat:")
                    decision = route_message(content, restrict_to=restrict)
                except Exception:  # noqa: BLE001
                    decision = None
                if decision is not None and decision.matched:
                    routed = decision
                    job = jobs.start(
                        f"user:{uid}:workflow:{routed.ritual_id}",
                        "workflow_run",
                        lambda job: execute_routed_run(
                            ledger, thread_id, routed, stub=stub
                        ),
                    )
                    return JSONResponse({"ok": True, "job": job.public()})

                # Layer 0.5 — classify the message (framework only; agent does
                # NOT act). Deterministic-only here so sending never blocks on a
                # model call; the classify_pending sweep fills in Qwen kinds for
                # the fuzzy ones. Kill-switch: ANALYST_CHAT_ACTIONABLE=off.
                try:
                    from analyst_ledger.actionable import actionable_enabled
                    from analyst_ledger.classify import classify_message

                    classified = (
                        classify_message(content, allow_qwen=False)
                        if actionable_enabled()
                        else None
                    )
                except Exception:  # noqa: BLE001 — tagging must never break chat
                    classified = None
                if classified and classified.get("labels"):
                    try:
                        ledger.record_ask_labels(
                            thread_id,
                            classified["labels"],
                            source="chat_classify",
                            meta={
                                "classification": {
                                    "kind": classified["kind"],
                                    "entity": classified["entity"],
                                    "source": classified["source"],
                                },
                                "target_event_id": user_event.event_id,
                            },
                        )
                    except Exception:  # noqa: BLE001 — never block chat on tagging
                        pass

            if session.desk_tag == "chat:master":
                job = jobs.start(
                    f"user:{uid}:chat:master",
                    "master_chat",
                    lambda job: MasterCoordinator(ledger).run(content, job=job),
                )
                return JSONResponse({"ok": True, "job": job.public()})
            ritual_id = str(session.desk_tag or "").removeprefix("chat:")
            # Optional @workflow mention inside an agent thread.
            match = re.search(
                r"(?<!\w)@workflow\s+([a-zA-Z0-9][a-zA-Z0-9_-]{0,120})\b",
                content,
                flags=re.I,
            )
            if match:
                ritual_id = _validate_ritual_id(match.group(1))
                approved = {
                    a["ritual_id"]
                    for a in list_automations(ledger)
                    if a.get("approved") and a.get("enabled", True)
                }
                if ritual_id not in approved:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "workflow_blocked",
                            "message": (
                                f"Workflow '{ritual_id}' is not approved/enabled."
                            ),
                        },
                        status_code=400,
                    )
            job = jobs.start(
                f"user:{uid}:workflow:{ritual_id}",
                "workflow_chat",
                lambda job: WorkflowEngine(ledger).run(
                    ritual_id, request=content, stub=False, job=job
                ),
            )
            return JSONResponse({"ok": True, "job": job.public()})
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/jobs/{job_id}")
def job_status(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    jobs = _jobs(request)
    job = jobs.get(job_id) if hasattr(jobs, "get") else None
    if job is None:
        # JobManager stores by id in _jobs
        job = getattr(jobs, "_jobs", {}).get(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    public = job.public() if hasattr(job, "public") else job
    return JSONResponse({"ok": True, "job": public, "user_id": user["user_id"]})
