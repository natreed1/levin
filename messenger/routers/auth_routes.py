"""Account signup / login / logout / me + email verify + password reset."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Union
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from messenger.auth import (
    COOKIE_NAME,
    hash_auth_token,
    hash_password,
    new_auth_token,
    new_user_id,
    normalize_email,
    normalize_name,
    utc_expiry_iso,
    verify_password,
)
from messenger.db import MessageStore
from messenger.deps import (
    clear_session_cookie,
    current_user,
    current_user_optional,
    get_store,
    identity_optional,
    resolve_identity,
    set_session_cookie,
)
from messenger.emailer import (
    auto_verify_on_signup,
    email_delivery_available,
    email_backend,
    expose_dev_links,
    public_base_url,
    send_password_reset_email,
    send_verification_email,
)
from messenger.tenancy import user_data_dir

logger = logging.getLogger("messenger.auth_routes")
router = APIRouter(prefix="/api/auth", tags=["auth"])

VERIFY_HOURS = 24.0
RESET_HOURS = 1.0


def _email_action_allowed(request: Request, action: str, email: str) -> bool:
    limiter = getattr(request.app.state, "auth_email_limiter", None)
    if limiter is None:
        return True
    client_ip = request.client.host if request.client else "unknown"
    return bool(limiter.allow(f"{action}:{client_ip}:{email}"))


def _base(request: Request) -> str:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    if host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _issue_token(store: MessageStore, *, user_id: str, purpose: str, hours: float) -> str:
    raw = new_auth_token()
    store.create_auth_token(
        token_hash=hash_auth_token(raw),
        user_id=user_id,
        purpose=purpose,
        expires_at=utc_expiry_iso(hours=hours),
    )
    return raw


def _verify_link(request: Request, token: str) -> str:
    return f"{public_base_url(_base(request))}/api/auth/verify-email?token={quote(token)}"


def _reset_page_link(request: Request, token: str) -> str:
    return f"{public_base_url(_base(request))}/?reset={quote(token)}"


@router.post("/signup")
async def signup(request: Request, store: MessageStore = Depends(get_store)) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    email = normalize_email(str(body.get("email") or ""))
    name = normalize_name(str(body.get("display_name") or body.get("name") or ""))
    password = str(body.get("password") or "")
    if not email:
        return JSONResponse({"ok": False, "error": "bad_email"}, status_code=400)
    if not name:
        return JSONResponse({"ok": False, "error": "bad_name"}, status_code=400)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return JSONResponse(
            {"ok": False, "error": "bad_password", "message": str(exc)},
            status_code=400,
        )
    skip_verify = auto_verify_on_signup()
    if (
        (os.environ.get("FLY_APP_NAME") or "").strip()
        and not email_delivery_available()
        and not skip_verify
    ):
        logger.error("signup unavailable: outbound email is not configured")
        return JSONResponse(
            {
                "ok": False,
                "error": "email_delivery_unavailable",
                "message": "Account creation is temporarily unavailable. Please try again later.",
            },
            status_code=503,
        )
    if store.user_by_email(email):
        return JSONResponse({"ok": False, "error": "email_taken"}, status_code=409)
    user_id = new_user_id()
    try:
        from datetime import datetime, timezone

        verified_at = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            if skip_verify
            else None
        )
        user = store.create_user(
            user_id, email, password_hash, name, email_verified_at=verified_at
        )
    except ValueError:
        return JSONResponse({"ok": False, "error": "email_taken"}, status_code=409)
    user_data_dir(user_id)

    if skip_verify:
        identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
        room_id = (identity or {}).get("room_id") or "legacy"
        resp = JSONResponse(
            {
                "ok": True,
                "user_id": user["user_id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "name": user["display_name"],
                "email_verified": True,
                "verification_sent": False,
                "email_backend": email_backend(),
                "auto_verified": True,
                "message": "Account created. Email delivery is not configured, so verification was skipped.",
                "room_id": room_id,
            }
        )
        set_session_cookie(
            resp,
            store=store,
            name=user["display_name"],
            room_id=room_id,
            can_create=True,
            user_id=user["user_id"],
            email=user["email"],
            revoke_sid=(identity or {}).get("sid"),
        )
        return resp

    token = _issue_token(store, user_id=user_id, purpose="verify", hours=VERIFY_HOURS)
    verify_url = _verify_link(request, token)
    mail_error = None
    try:
        send_verification_email(
            to=email, verify_url=verify_url, display_name=name
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("verification email failed: %s", exc)
        mail_error = str(exc)

    payload: dict[str, Any] = {
        "ok": True,
        "user_id": user["user_id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "name": user["display_name"],
        "email_verified": False,
        "verification_sent": mail_error is None,
        "email_backend": email_backend(),
        "message": (
            "Check your email for a verification link before logging in."
            if mail_error is None
            else "Account created, but the verification email could not be sent."
        ),
    }
    if expose_dev_links():
        payload["dev_verify_url"] = verify_url
    if mail_error and expose_dev_links():
        payload["mail_error"] = mail_error
    # Do not set a session cookie until email is verified.
    return JSONResponse(payload)


@router.post("/login")
async def login(request: Request, store: MessageStore = Depends(get_store)) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    email = normalize_email(str(body.get("email") or ""))
    password = str(body.get("password") or "")
    client_ip = request.client.host if request.client else "unknown"
    login_key = f"login:{client_ip}:{email or ''}"
    login_limiter = getattr(request.app.state, "login_limiter", None)
    if login_limiter is not None and not login_limiter.allow(login_key):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
    if not email:
        return JSONResponse({"ok": False, "error": "bad_email"}, status_code=400)
    user = store.user_by_email(email)
    if not user or not verify_password(password, str(user["password_hash"])):
        return JSONResponse({"ok": False, "error": "bad_credentials"}, status_code=401)
    if not user.get("email_verified"):
        return JSONResponse(
            {
                "ok": False,
                "error": "email_unverified",
                "message": "Verify your email before logging in. Resend the link if needed.",
                "email": user["email"],
            },
            status_code=403,
        )
    identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
    room_id = (identity or {}).get("room_id") or "legacy"
    resp = JSONResponse(
        {
            "ok": True,
            "user_id": user["user_id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "name": user["display_name"],
            "room_id": room_id,
            "email_verified": True,
        }
    )
    set_session_cookie(
        resp,
        store=store,
        name=user["display_name"],
        room_id=room_id,
        can_create=True,
        user_id=user["user_id"],
        email=user["email"],
        revoke_sid=(identity or {}).get("sid"),
    )
    return resp


@router.post("/logout")
def logout(
    request: Request, store: MessageStore = Depends(get_store)
) -> JSONResponse:
    identity = resolve_identity(request.cookies.get(COOKIE_NAME), store)
    if not identity:
        return JSONResponse(
            {"ok": False, "error": "unauthorized"}, status_code=401
        )
    sid = (identity.get("sid") or "").strip()
    if sid:
        store.delete_session(sid)
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    return resp


@router.get("/me")
def me(
    store: MessageStore = Depends(get_store),
    user: Optional[dict[str, Any]] = Depends(current_user_optional),
    identity: Optional[dict[str, str]] = Depends(identity_optional),
) -> JSONResponse:
    if user:
        full = store.user_by_id(user["user_id"]) or {}
        room_id = user.get("room_id") or "legacy"
        room = store.room(room_id) if room_id != "legacy" else None
        return JSONResponse(
            {
                "ok": True,
                "authenticated": True,
                "user_id": user["user_id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "name": user["name"],
                "room_id": room_id,
                "room_title": (room or {}).get("title") or "Private room",
                "email_verified": bool(full.get("email_verified")),
                "created_at": full.get("created_at"),
                "session_count": store.count_sessions_for_user(user["user_id"]),
            }
        )
    if identity:
        room_id = identity["room_id"]
        room = store.room(room_id) if room_id != "legacy" else None
        return JSONResponse(
            {
                "ok": True,
                "authenticated": False,
                "legacy": True,
                "name": identity["name"],
                "room_id": room_id,
                "room_title": (room or {}).get("title") or "Private room",
            }
        )
    return JSONResponse({"ok": False, "authenticated": False}, status_code=401)


@router.patch("/profile")
async def update_profile(
    request: Request,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    display_name = normalize_name(str(body.get("display_name") or ""))
    if not display_name:
        return JSONResponse({"ok": False, "error": "bad_name"}, status_code=400)

    store.update_display_name(user["user_id"], display_name)
    store.delete_sessions_for_user(user["user_id"])
    resp = JSONResponse(
        {
            "ok": True,
            "display_name": display_name,
            "message": "Profile updated. Other sessions were signed out.",
        }
    )
    set_session_cookie(
        resp,
        store=store,
        name=display_name,
        room_id=user.get("room_id") or "legacy",
        can_create=True,
        user_id=user["user_id"],
        email=user["email"],
    )
    return resp


@router.post("/change-password")
async def change_password(
    request: Request,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    current_password = str(body.get("current_password") or "")
    new_password = str(body.get("new_password") or "")
    password_limiter = getattr(request.app.state, "login_limiter", None)
    if password_limiter is not None and not password_limiter.allow(
        f"change-password:{user['user_id']}"
    ):
        return JSONResponse(
            {"ok": False, "error": "rate_limited"},
            status_code=429,
        )
    full = store.user_by_id(user["user_id"]) or {}
    password_hash = str(full.get("password_hash") or "")
    if not verify_password(current_password, password_hash):
        return JSONResponse(
            {"ok": False, "error": "bad_current_password"},
            status_code=401,
        )
    if verify_password(new_password, password_hash):
        return JSONResponse(
            {
                "ok": False,
                "error": "password_unchanged",
                "message": "Choose a password you are not already using.",
            },
            status_code=400,
        )
    try:
        new_hash = hash_password(new_password)
    except ValueError as exc:
        return JSONResponse(
            {"ok": False, "error": "bad_password", "message": str(exc)},
            status_code=400,
        )

    store.update_password(user["user_id"], new_hash)
    store.delete_sessions_for_user(user["user_id"])
    resp = JSONResponse(
        {
            "ok": True,
            "message": "Password changed. Other sessions were signed out.",
        }
    )
    set_session_cookie(
        resp,
        store=store,
        name=user["display_name"],
        room_id=user.get("room_id") or "legacy",
        can_create=True,
        user_id=user["user_id"],
        email=user["email"],
    )
    return resp


@router.post("/logout-other-sessions")
def logout_other_sessions(
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    revoked = store.delete_other_sessions_for_user(
        user["user_id"], str(user.get("sid") or "")
    )
    return JSONResponse(
        {
            "ok": True,
            "revoked": revoked,
            "message": (
                f"Signed out {revoked} other session{'s' if revoked != 1 else ''}."
            ),
        }
    )


@router.post("/resend-verification")
async def resend_verification(
    request: Request, store: MessageStore = Depends(get_store)
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    email = normalize_email(str(body.get("email") or ""))
    if not email:
        return JSONResponse({"ok": False, "error": "bad_email"}, status_code=400)
    if not _email_action_allowed(request, "resend-verification", email):
        return JSONResponse(
            {"ok": False, "error": "rate_limited", "message": "Please try again later."},
            status_code=429,
        )
    user = store.user_by_email(email)
    # Always look successful to avoid email enumeration.
    payload: dict[str, Any] = {
        "ok": True,
        "message": "If that account needs verification, a new email is on the way.",
    }
    if user and not user.get("email_verified"):
        token = _issue_token(
            store, user_id=user["user_id"], purpose="verify", hours=VERIFY_HOURS
        )
        verify_url = _verify_link(request, token)
        try:
            send_verification_email(
                to=email,
                verify_url=verify_url,
                display_name=str(user["display_name"]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("resend verification failed: %s", exc)
            if expose_dev_links():
                payload["mail_error"] = str(exc)
        if expose_dev_links():
            payload["dev_verify_url"] = verify_url
    return JSONResponse(payload)


@router.get("/verify-email", response_model=None)
def verify_email_get(
    token: str = "",
    store: MessageStore = Depends(get_store),
):
    ok, message = _verify_token(store, token)
    if ok:
        # Land on login with a success banner.
        return RedirectResponse(
            url="/?verified=1",
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Verify email</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{{font-family:system-ui,sans-serif;background:#0e1116;color:#e8eef7;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
.card{{background:#161b22;border:1px solid #2a3344;border-radius:12px;padding:1.5rem;
max-width:28rem;width:92%}}
a{{color:#3d9cf0}}
</style></head><body><div class="card">
<h1>Verification failed</h1>
<p>{message}</p>
<p><a href="/">Back to Workflow</a></p>
</div></body></html>"""
    return HTMLResponse(
        body,
        status_code=400,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/verify-email")
async def verify_email_post(
    request: Request, store: MessageStore = Depends(get_store)
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = str((body or {}).get("token") or "")
    ok, message = _verify_token(store, token)
    if not ok:
        return JSONResponse({"ok": False, "error": "bad_token", "message": message}, status_code=400)
    return JSONResponse({"ok": True, "message": "Email verified. You can log in."})


def _verify_token(store: MessageStore, token: str) -> tuple[bool, str]:
    raw = (token or "").strip()
    if not raw:
        return False, "Missing verification token."
    row = store.consume_auth_token(
        token_hash=hash_auth_token(raw), purpose="verify"
    )
    if not row:
        return False, "This verification link is invalid or has expired."
    store.mark_email_verified(str(row["user_id"]))
    return True, "Email verified."


@router.post("/forgot-password")
async def forgot_password(
    request: Request, store: MessageStore = Depends(get_store)
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    email = normalize_email(str(body.get("email") or ""))
    if not email:
        return JSONResponse({"ok": False, "error": "bad_email"}, status_code=400)
    if not _email_action_allowed(request, "forgot-password", email):
        return JSONResponse(
            {"ok": False, "error": "rate_limited", "message": "Please try again later."},
            status_code=429,
        )
    payload: dict[str, Any] = {
        "ok": True,
        "message": "If an account exists for that email, a reset link is on the way.",
    }
    user = store.user_by_email(email)
    if user:
        token = _issue_token(
            store, user_id=user["user_id"], purpose="reset", hours=RESET_HOURS
        )
        reset_url = _reset_page_link(request, token)
        try:
            send_password_reset_email(
                to=email,
                reset_url=reset_url,
                display_name=str(user["display_name"]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reset email failed: %s", exc)
            if expose_dev_links():
                payload["mail_error"] = str(exc)
        if expose_dev_links():
            payload["dev_reset_url"] = reset_url
    return JSONResponse(payload)


@router.post("/reset-password")
async def reset_password(
    request: Request, store: MessageStore = Depends(get_store)
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    token = str(body.get("token") or "").strip()
    password = str(body.get("password") or "")
    if not token:
        return JSONResponse({"ok": False, "error": "bad_token"}, status_code=400)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return JSONResponse(
            {"ok": False, "error": "bad_password", "message": str(exc)},
            status_code=400,
        )
    row = store.consume_auth_token(
        token_hash=hash_auth_token(token), purpose="reset"
    )
    if not row:
        return JSONResponse(
            {
                "ok": False,
                "error": "bad_token",
                "message": "This reset link is invalid or has expired.",
            },
            status_code=400,
        )
    store.update_password(str(row["user_id"]), password_hash)
    # Verifying email via a successful reset is reasonable if they got the mail.
    store.mark_email_verified(str(row["user_id"]))
    store.delete_sessions_for_user(str(row["user_id"]))
    return JSONResponse(
        {"ok": True, "message": "Password updated. You can log in."}
    )
