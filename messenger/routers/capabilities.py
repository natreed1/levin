"""Capabilities catalog API (layer 1) — registry SoR."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from messenger.deps import current_user
from messenger.tenancy import user_context

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


def _jobs(request: Request) -> Any:
    return request.app.state.jobs


@router.get("")
def list_capabilities(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    from analyst_ledger.registry import list_capabilities_public

    with user_context(user["user_id"]) as ledger:
        return JSONResponse(
            {
                "ok": True,
                "capabilities": list_capabilities_public(ledger=ledger),
                "source": "registry",
            }
        )


@router.post("/{action}")
async def capability_action(
    action: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    """Mine / approve / run — same actions as legacy automations API."""
    from analyst_ledger.dashboard import _api_automations_action

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if "ritual_id" not in body and body.get("capability_id"):
        body = {**body, "ritual_id": body["capability_id"]}
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
