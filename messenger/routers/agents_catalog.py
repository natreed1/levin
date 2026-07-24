"""Agents catalog API (layer 2) — registry SoR."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
def list_agents_catalog() -> JSONResponse:
    from analyst_ledger.registry import list_agents_public

    return JSONResponse(
        {
            "ok": True,
            "agents": list_agents_public(),
            "source": "registry",
            "layers": {
                "capabilities": "Reusable verbs (built-in or mined).",
                "agents": "How rooms use capabilities — lenses are prompts; operators call verbs.",
                "automations": "Loops of capabilities, preferably born from /automate in a chat.",
            },
        }
    )
