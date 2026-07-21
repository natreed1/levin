"""Claude / chat-mining review APIs (per-user ledger)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from messenger.deps import current_user
from messenger.tenancy import user_context

router = APIRouter(prefix="/api/review", tags=["review"])


@router.get("")
def review_overview(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    """Open draft proposals (from Claude review or chat mining) + past memos."""
    from analyst_ledger.review import list_reviews, read_review
    from analyst_ledger.rituals import list_automations, load_spec

    with user_context(user["user_id"]) as ledger:
        proposals = []
        for row in list_automations(ledger):
            rid = row.get("ritual_id")
            if not rid:
                continue
            try:
                spec = load_spec(str(rid))
            except Exception:
                spec = None
            if not isinstance(spec, dict):
                continue
            proposed_by = spec.get("proposed_by")
            if proposed_by not in {"claude_review", "chat_mining"}:
                continue
            if row.get("approved"):
                continue
            proposals.append(
                {
                    "ritual_id": rid,
                    "name": row.get("name") or rid,
                    "runner": row.get("runner") or spec.get("runner"),
                    "proposed_by": proposed_by,
                    "schedule": row.get("schedule") or spec.get("schedule"),
                    "source_candidate": spec.get("source_candidate"),
                }
            )
        memos = list_reviews()
        latest = memos[0]["name"] if memos else None
        memo_text = read_review(latest) if latest else None
        return JSONResponse(
            {
                "ok": True,
                "proposals": proposals,
                "memos": memos,
                "latest_memo": latest,
                "memo_text": memo_text,
            }
        )


@router.post("/run")
async def review_run(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    from analyst_ledger.review import run_review

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    days = int(body.get("days") or 14)
    try:
        with user_context(user["user_id"]) as ledger:
            result = run_review(
                ledger,
                days=days,
                destination=body.get("destination"),
            )
        if isinstance(result, dict):
            result.setdefault("ok", True)
            return JSONResponse(result)
        return JSONResponse({"ok": True, "result": result})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
