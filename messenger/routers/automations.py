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
    from analyst_ledger.registry import list_automations_public

    with user_context(user["user_id"]) as ledger:
        return JSONResponse(
            {
                "ok": True,
                "automations": list_automations_public(ledger=ledger),
                "source": "registry",
                "hint": "Create new loops from a room with /automate (dual editor).",
            }
        )


@router.post("/from-chat")
async def create_automation_from_chat(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    """Draft an automation (capability loop) from a room /automate editor."""
    from analyst_ledger.registry import create_automation_from_chat as create_loop

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    steps_raw = body.get("steps") or body.get("capability_ids") or []
    if isinstance(steps_raw, str):
        steps_raw = [ln.strip() for ln in steps_raw.splitlines() if ln.strip()]
    try:
        with user_context(user["user_id"]):
            spec = create_loop(
                name=str(body.get("name") or body.get("ritual_id") or ""),
                capability_ids=steps_raw if isinstance(steps_raw, list) else [],
                schedule=(str(body.get("schedule") or "").strip() or None),
                room_id=(str(body.get("room_id") or "").strip() or None),
                transcript=str(body.get("transcript") or "")[:8000],
                watchlist=body.get("watchlist") or [],
            )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {"ok": True, "automation": spec, "ritual_id": spec.get("name"), "source": "registry"}
    )


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
