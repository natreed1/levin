"""Agents catalog API (layer 2) — lenses vs operators."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
def list_agents_catalog() -> JSONResponse:
    from messenger.layer_catalog import list_agents_catalog as catalog

    return JSONResponse(
        {
            "ok": True,
            "agents": catalog(),
            "layers": {
                "capabilities": "Reusable verbs (built-in or mined).",
                "agents": "How rooms use capabilities — lenses are prompts; operators call verbs.",
                "automations": "Loops of capabilities, preferably born from /automate in a chat.",
            },
        }
    )
