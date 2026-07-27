"""Tracking / session / ingest / timeline APIs (per-user ledger)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from messenger.deps import current_user
from messenger.tenancy import user_context

router = APIRouter(prefix="/api/tracking", tags=["tracking"])

CAPTURE_SCOPES = frozenset(
    {
        "active_tab",
        "all_tabs",
        "selected_tabs",
        "research_sites",
        "notes_only",
    }
)


def _normalize_capture_scope(raw: Any) -> str:
    scope = str(raw or "active_tab").strip().lower()
    if scope not in CAPTURE_SCOPES:
        raise ValueError(
            f"Unknown capture_scope '{raw}'. Expected one of: {', '.join(sorted(CAPTURE_SCOPES))}"
        )
    return scope


def _surface_for_scope(scope: str, requested: Optional[str]) -> str:
    from analyst_ledger.schema import Surface

    if requested:
        return str(requested)
    if scope == "notes_only":
        return Surface.NOTES.value
    return Surface.BROWSER.value


def _is_restricted(row: dict[str, Any]) -> bool:
    return str(row.get("sensitivity") or "").strip().lower() == "restricted"


def _session_action(ledger: Any, action: str, data: dict) -> dict:
    from analyst_ledger.schema import Sensitivity

    if action == "start":
        capture_scope = _normalize_capture_scope(data.get("capture_scope"))
        title = str(data.get("title") or "Research session").strip()
        surface = _surface_for_scope(capture_scope, data.get("surface"))
        sensitivity = str(data.get("sensitivity") or Sensitivity.INTERNAL.value)
        active = ledger.get_active_session_id()
        if active:
            existing = ledger.get_session(active)
            if existing and existing.status == "open":
                ledger.end_session(session_id=active, tags=["neutral"])
        session = ledger.start_session(
            title=title,
            surface=surface,
            sensitivity=sensitivity,
            capture_scope=capture_scope,
        )
        out = session.to_dict()
        out["capture_scope"] = capture_scope
        return {"ok": True, "session": out, "capture_scope": capture_scope}
    if action == "note":
        text = str(data.get("text") or "").strip()
        if not text:
            raise ValueError("text required")
        event = ledger.add_note(text)
        return {"ok": True, "event": event.to_dict()}
    if action == "end":
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        session = ledger.end_session(tags=tags)
        return {"ok": True, "session": session.to_dict()}
    if action == "tag":
        tag = str(data.get("tag") or "").strip()
        if not tag:
            raise ValueError("tag required")
        event = ledger.add_tag(tag, session_id=data.get("session_id"))
        sid = event.session_id
        session = ledger.get_session(sid) if sid else None
        return {
            "ok": True,
            "event": event.to_dict(),
            "session": session.to_dict() if session else None,
        }
    raise ValueError(f"unknown session action: {action}")


def _active_session_payload(ledger: Any) -> Optional[dict]:
    active_id = ledger.get_active_session_id()
    active = ledger.get_session(active_id) if active_id else None
    if not active:
        return None
    if _is_restricted(active.to_dict()):
        return None
    out = active.to_dict()
    out["capture_scope"] = ledger.get_capture_scope() or "active_tab"
    return out


def _enrich_events(ledger: Any, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop restricted rows; attach resolved_kind + safe excerpts for Tracking UI."""
    visible = [ev for ev in events if not _is_restricted(ev)]
    content_by_id: dict[str, str] = {}
    for ev in visible:
        if ev.get("type") != "chat_message":
            continue
        payload = ev.get("payload") or {}
        content_by_id[str(ev.get("event_id") or "")] = str(
            payload.get("content") or ""
        )

    out: list[dict[str, Any]] = []
    for ev in visible:
        row = dict(ev)
        payload = dict(row.get("payload") or {})
        etype = row.get("type")
        if etype == "chat_message":
            eid = str(row.get("event_id") or "")
            try:
                kind = ledger.latest_kind_for(eid, row.get("session_id"))
            except Exception:  # noqa: BLE001
                kind = None
            if kind:
                row["resolved_kind"] = kind
        elif etype == "label":
            target = str(payload.get("target_event_id") or "")
            if target and target in content_by_id:
                excerpt = content_by_id[target].strip().replace("\n", " ")
                if len(excerpt) > 120:
                    excerpt = excerpt[:117] + "…"
                row["message_excerpt"] = excerpt
            kinds = [
                str(lbl).split(":", 1)[1]
                for lbl in (payload.get("labels") or [])
                if str(lbl).startswith("kind:")
            ]
            if kinds:
                row["resolved_kind"] = kinds[0]
            elif target:
                try:
                    kind = ledger.latest_kind_for(target, row.get("session_id"))
                except Exception:  # noqa: BLE001
                    kind = None
                if kind:
                    row["resolved_kind"] = kind
        out.append(row)
    return out


@router.get("/summary")
def tracking_summary(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    with user_context(user["user_id"]) as ledger:
        summary = ledger.summary()
        return JSONResponse(
            {
                "ok": True,
                "summary": summary,
                "active_session": _active_session_payload(ledger),
                "capture_scopes": sorted(CAPTURE_SCOPES),
            }
        )


@router.get("/sessions")
def list_sessions(
    limit: int = 50,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    with user_context(user["user_id"]) as ledger:
        sessions = [
            s for s in ledger.list_sessions(limit=limit) if not _is_restricted(s)
        ]
        return JSONResponse({"ok": True, "sessions": sessions})


@router.get("/events")
def list_events(
    session_id: Optional[str] = None,
    limit: int = 100,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    with user_context(user["user_id"]) as ledger:
        raw = ledger.list_events(session_id=session_id, limit=max(limit * 2, limit))
        events = _enrich_events(ledger, raw)[:limit]
        return JSONResponse({"ok": True, "events": events})


@router.get("/labels/vocab")
def labels_vocab(user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
    from analyst_ledger.labels import INTENTS, KINDS, STATES, TOPICS

    return JSONResponse(
        {
            "ok": True,
            "kinds": sorted(KINDS),
            "topics": sorted(TOPICS),
            "intents": sorted(INTENTS),
            "states": sorted(STATES),
        }
    )


@router.post("/labels/correct")
async def labels_correct(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(data, dict):
        data = {}
    session_id = str(data.get("session_id") or "")
    target_event_id = str(data.get("event_id") or data.get("target_event_id") or "")
    kind = str(data.get("kind") or "").strip()
    if not (session_id and target_event_id and kind):
        return JSONResponse(
            {"ok": False, "error": "session_id, event_id and kind are required"},
            status_code=400,
        )
    try:
        with user_context(user["user_id"]) as ledger:
            labels = ledger.correct_message_kind(
                session_id,
                target_event_id,
                kind,
                entity=str(data["entity"]) if data.get("entity") else None,
                auto_kind=str(data["auto_kind"]) if data.get("auto_kind") else None,
            )
            return JSONResponse(
                {
                    "ok": True,
                    "labels": labels,
                    "kind": ledger.latest_kind_for(target_event_id, session_id),
                }
            )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/classify-pending")
async def classify_pending_endpoint(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    limit = int(data.get("limit") or 20)
    try:
        with user_context(user["user_id"]) as ledger:
            from analyst_ledger.classify import classify_pending

            result = classify_pending(ledger, limit=limit)
        return JSONResponse({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/session/{action}")
async def session_action(
    action: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        with user_context(user["user_id"]) as ledger:
            result = _session_action(ledger, action, body)
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/ingest-browser")
async def ingest_browser(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    try:
        with user_context(user["user_id"]) as ledger:
            from analyst_ledger.dashboard import _ingest_browser

            event = _ingest_browser(ledger, data if isinstance(data, dict) else {})
        return JSONResponse(event)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/ingest-tv")
async def ingest_tv(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    try:
        with user_context(user["user_id"]) as ledger:
            from analyst_ledger.dashboard import _ingest_tv

            event = _ingest_tv(ledger, data if isinstance(data, dict) else {})
        return JSONResponse(event)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
