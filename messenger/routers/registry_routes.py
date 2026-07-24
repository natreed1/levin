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


@router.post("/agents/delete")
async def delete_many_agents(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.registry import delete_user_agents

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    ids = (body or {}).get("ids") or (body or {}).get("agent_ids") or []
    if not isinstance(ids, list):
        return JSONResponse({"ok": False, "error": "ids_required"}, status_code=400)
    with user_context(user["user_id"]):
        deleted = delete_user_agents(ids)
    return JSONResponse({"ok": True, "deleted": deleted})


@router.get("/agents/{agent_id}")
def get_one_agent(
    agent_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.registry import get_agent_public

    with user_context(user["user_id"]):
        pub = get_agent_public(agent_id, include_prompt=True)
    if not pub:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, "agent": pub})


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
    pub["editable"] = True
    return JSONResponse({"ok": True, "agent": pub})


@router.patch("/agents/{agent_id}")
async def patch_agent(
    agent_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.registry import update_composed_agent

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    kwargs: dict[str, Any] = {}
    if "name" in body:
        kwargs["name"] = body.get("name")
    if "lens_ids" in body:
        kwargs["lens_ids"] = body.get("lens_ids") or []
    if "capability_ids" in body or "capabilities" in body:
        kwargs["capability_ids"] = body.get("capability_ids") or body.get("capabilities") or []
    if "prompt" in body:
        kwargs["prompt"] = body.get("prompt")
    if "summary" in body:
        kwargs["summary"] = body.get("summary")
    try:
        with user_context(user["user_id"]):
            agent = update_composed_agent(agent_id, **kwargs)
    except ValueError as exc:
        msg = str(exc)
        code = 404 if msg == "agent_not_editable" else 400
        return JSONResponse({"ok": False, "error": msg}, status_code=code)
    pub = agent.to_public()
    pub.pop("prompt", None)
    pub["editable"] = True
    return JSONResponse({"ok": True, "agent": pub})


@router.delete("/agents/{agent_id}")
def delete_one_agent(
    agent_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.registry import delete_user_agents

    with user_context(user["user_id"]):
        deleted = delete_user_agents([agent_id])
    if not deleted:
        return JSONResponse(
            {"ok": False, "error": "not_found_or_builtin"}, status_code=404
        )
    return JSONResponse({"ok": True, "deleted": deleted})
