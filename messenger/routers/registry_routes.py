"""Registry studio APIs — lenses, capabilities, composed agents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from messenger.deps import current_user
from messenger.tenancy import user_context

router = APIRouter(prefix="/api/registry", tags=["registry"])


@router.get("/lenses")
def get_lenses(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    from analyst_ledger.registry import list_lenses_public

    with user_context(user["user_id"]):
        return JSONResponse({"ok": True, "lenses": list_lenses_public(), "source": "registry"})


@router.post("/lenses")
async def post_lens(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.registry import create_lens

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    try:
        with user_context(user["user_id"]):
            lens = create_lens(
                name=str((body or {}).get("name") or ""),
                prompt=str((body or {}).get("prompt") or ""),
                summary=str((body or {}).get("summary") or ""),
            )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "lens": lens.to_public()})


@router.get("/capabilities")
def get_caps(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    from analyst_ledger.registry import list_capabilities_public

    with user_context(user["user_id"]) as ledger:
        return JSONResponse(
            {"ok": True, "capabilities": list_capabilities_public(ledger=ledger)}
        )


@router.post("/capabilities")
async def post_cap(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.registry import create_user_capability

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    try:
        with user_context(user["user_id"]):
            cap = create_user_capability(
                name=str((body or {}).get("name") or ""),
                summary=str((body or {}).get("summary") or ""),
                runner=(str((body or {}).get("runner") or "").strip() or None),
            )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "capability": cap.to_public()})


@router.get("/agents")
def get_agents(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    from analyst_ledger.registry import list_agents_public

    with user_context(user["user_id"]):
        return JSONResponse({"ok": True, "agents": list_agents_public(), "source": "registry"})


@router.post("/agents")
async def post_agent(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.registry import create_composed_agent

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    try:
        with user_context(user["user_id"]):
            agent = create_composed_agent(
                name=str(body.get("name") or ""),
                lens_ids=body.get("lens_ids") or [],
                capability_ids=body.get("capability_ids") or body.get("capabilities") or [],
                prompt=str(body.get("prompt") or ""),
                summary=str(body.get("summary") or ""),
            )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    pub = agent.to_public()
    pub.pop("prompt", None)
    return JSONResponse({"ok": True, "agent": pub})
