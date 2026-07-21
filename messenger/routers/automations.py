"""Automations / rituals APIs (per-user ledger)."""

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
