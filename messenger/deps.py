"""Shared FastAPI dependencies for the unified workflow messenger."""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, Response

from messenger.auth import (
    COOKIE_NAME,
    SESSION_MAX_AGE,
    mint_session,
    read_identity,
    utc_expiry_iso,
)
from messenger.db import MessageStore


def cookie_secure() -> bool:
    raw = os.environ.get("MESSENGER_COOKIE_SECURE", "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    return bool(os.environ.get("FLY_APP_NAME"))


def resolve_identity(
    cookie_value: Optional[str], store: MessageStore
) -> Optional[dict[str, str]]:
    """Signature check + live session registry lookup."""
    identity = read_identity(cookie_value)
    if not identity:
        return None
    sid = (identity.get("sid") or "").strip()
    if not sid or not store.session_is_active(sid):
        return None
    return identity


def set_session_cookie(
    response: Response,
    *,
    store: MessageStore,
    name: str,
    room_id: str = "legacy",
    can_create: bool = False,
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    revoke_sid: Optional[str] = None,
) -> str:
    """Mint a signed cookie, register the session server-side, return sid."""
    if revoke_sid:
        store.delete_session(str(revoke_sid))
    cookie_value, sid = mint_session(
        name,
        room_id,
        can_create=can_create,
        user_id=user_id,
        email=email,
    )
    store.create_session(
        sid=sid,
        user_id=user_id,
        expires_at=utc_expiry_iso(hours=SESSION_MAX_AGE / 3600.0),
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        max_age=SESSION_MAX_AGE,
        path="/",
    )
    return sid


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=cookie_secure(),
        samesite="lax",
    )


def get_store(request: Request) -> MessageStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise RuntimeError("MessageStore not attached to app.state")
    return store


def identity_optional(
    request: Request,
    store: MessageStore = Depends(get_store),
) -> Optional[dict[str, str]]:
    return resolve_identity(request.cookies.get(COOKIE_NAME), store)


def identity_required(
    request: Request,
    store: MessageStore = Depends(get_store),
) -> dict[str, str]:
    identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
    if not identity:
        raise HTTPException(status_code=401, detail="unauthorized")
    return identity


def current_user(
    request: Request,
    store: MessageStore = Depends(get_store),
    identity: dict[str, str] = Depends(identity_required),
) -> dict[str, Any]:
    """Require a logged-in account (user_id present in the session cookie)."""
    user_id = (identity.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="account_required")
    user = store.user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="unknown_user")
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "name": identity.get("name") or user["display_name"],
        "room_id": identity.get("room_id") or "legacy",
        "can_create": bool(identity.get("can_create")),
        "email_verified": bool(user.get("email_verified")),
        "sid": identity.get("sid") or "",
    }


def current_user_optional(
    request: Request,
    store: MessageStore = Depends(get_store),
) -> Optional[dict[str, Any]]:
    identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
    if not identity:
        return None
    user_id = (identity.get("user_id") or "").strip()
    if not user_id:
        return None
    user = store.user_by_id(user_id)
    if not user:
        return None
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "name": identity.get("name") or user["display_name"],
        "room_id": identity.get("room_id") or "legacy",
        "can_create": bool(identity.get("can_create")),
        "email_verified": bool(user.get("email_verified")),
        "sid": identity.get("sid") or "",
    }
