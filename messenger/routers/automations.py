"""Automations / rituals APIs (per-user ledger).

Layer 3: capability loops. Legacy list still returns ritual rows for
compat; ``/api/automations/loops`` returns the Automations-tab catalog.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from messenger.deps import current_user
from messenger.tenancy import user_context

router = APIRouter(prefix="/api/automations", tags=["automations"])


def _jobs(request: Request) -> Any:
    return request.app.state.jobs


@router.get("")
def list_automations(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    from analyst_ledger.rituals import list_automations as list_autos

    with user_context(user["user_id"]) as ledger:
        return JSONResponse({"ok": True, "automations": list_autos(ledger)})


@router.get("/loops")
def list_automation_loops(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    from messenger.layer_catalog import list_automation_loops as catalog

    with user_context(user["user_id"]) as ledger:
        return JSONResponse(
            {
                "ok": True,
                "automations": catalog(ledger),
                "hint": "Create new loops from a room with /automate (dual editor).",
            }
        )


@router.post("/from-chat")
async def create_automation_from_chat(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    """Draft an automation (capability loop) from a room /automate editor."""
    import json
    import re

    from analyst_ledger.paths import ritual_specs_dir
    from analyst_ledger.schema import utc_now_iso

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    raw_name = str(body.get("name") or body.get("ritual_id") or "").strip()
    rid = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_name).strip("_")[:80]
    if not rid or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,120}$", rid):
        return JSONResponse(
            {"ok": False, "error": "name must start with a letter/number"},
            status_code=400,
        )

    steps_raw = body.get("steps") or []
    if isinstance(steps_raw, str):
        steps_raw = [ln.strip() for ln in steps_raw.splitlines() if ln.strip()]
    if not isinstance(steps_raw, list) or not steps_raw:
        return JSONResponse({"ok": False, "error": "steps required"}, status_code=400)

    # Map capability ids → workflow step dicts or runner-only specs.
    from messenger.layer_catalog import BUILTIN_CAPABILITIES

    builtin = {c["id"]: c for c in BUILTIN_CAPABILITIES}
    steps: list[dict[str, Any]] = []
    runner = None
    for item in steps_raw[:12]:
        cid = str(item).strip()
        meta = builtin.get(cid) or {}
        if meta.get("action"):
            steps.append({meta["action"]: {}})
        elif meta.get("runner"):
            runner = runner or meta["runner"]
            steps.append({meta["runner"]: {}})
        else:
            # User / freeform capability id — keep as named step.
            steps.append({cid: {}})

    room_id = str(body.get("room_id") or "").strip() or None
    schedule = str(body.get("schedule") or "").strip() or None
    transcript = str(body.get("transcript") or "")[:8000]

    spec = {
        "name": rid,
        "version": 1,
        "approved": False,
        "enabled": False,
        "runner": runner or "note_digest",
        "schedule": schedule,
        "schedule_comment": "Drafted from room /automate",
        "watchlist": body.get("watchlist") or [],
        "steps": steps,
        "outputs": {"ledger_session": True},
        "room_id": room_id,
        "source_chat": {"transcript_excerpt": transcript[:2000]},
        "proposed_by": "room_automate",
        "created_at": utc_now_iso(),
        "description": f"Automation loop drafted from chat ({len(steps)} capability steps).",
    }

    with user_context(user["user_id"]):
        path = ritual_specs_dir() / f"{rid}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            if existing.get("approved"):
                return JSONResponse(
                    {"ok": False, "error": "an approved automation with that name exists"},
                    status_code=400,
                )
        path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    return JSONResponse({"ok": True, "automation": spec, "ritual_id": rid})


@router.get("/{ritual_id}")
def show_automation(
    ritual_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.rituals import load_spec, list_automations as list_autos

    with user_context(user["user_id"]) as ledger:
        autos = {a["ritual_id"]: a for a in list_autos(ledger)}
        row = autos.get(ritual_id)
        try:
            spec = load_spec(ritual_id)
        except Exception:
            spec = None
        if not row and not spec:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        return JSONResponse({"ok": True, "automation": row, "spec": spec})


@router.post("/{action}")
async def automation_action(
    action: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.dashboard import _api_automations_action

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        with user_context(user["user_id"]) as ledger:
            result = _api_automations_action(ledger, action, body, jobs=_jobs(request))
        if isinstance(result, dict):
            result.setdefault("ok", True)
            return JSONResponse(result)
        return JSONResponse({"ok": True, "result": result})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
