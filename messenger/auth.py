"""Account auth (signup/login) + signed session cookies + room invite tokens."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Dict, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "messenger_session"
MAX_NAME_LEN = 40
MAX_TITLE_LEN = 80
MAX_EMAIL_LEN = 254
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_PBKDF2_ITERATIONS = 260_000
# Reject HTML / attribute breakout characters in user-facing labels.
_FORBIDDEN_LABEL_CHARS = frozenset("<>\"'`")


def invite_token() -> str:
    token = os.environ.get("MESSENGER_INVITE_TOKEN", "").strip()
    if not token:
        # Dev fallback so local runs work without secrets; never use in prod.
        token = "dev-invite-change-me"
    return token


def session_secret() -> str:
    secret = os.environ.get("MESSENGER_SESSION_SECRET", "").strip()
    if secret:
        return secret
    # Derive a stable secret from the invite token when unset.
    return hashlib.sha256(f"messenger:{invite_token()}".encode()).hexdigest()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(session_secret(), salt="messenger-session-v2")


def invite_ok(candidate: str) -> bool:
    expected = invite_token()
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


def _normalize_label(value: str, *, max_len: int) -> Optional[str]:
    cleaned = " ".join((value or "").strip().split())
    if not cleaned or len(cleaned) > max_len:
        return None
    if any(ch in _FORBIDDEN_LABEL_CHARS for ch in cleaned):
        return None
    if any(ord(ch) < 32 for ch in cleaned):
        return None
    return cleaned


def normalize_name(name: str) -> Optional[str]:
    return _normalize_label(name, max_len=MAX_NAME_LEN)


def normalize_title(title: str) -> Optional[str]:
    return _normalize_label(title, max_len=MAX_TITLE_LEN)


def normalize_email(email: str) -> Optional[str]:
    cleaned = (email or "").strip().lower()
    if not cleaned or len(cleaned) > MAX_EMAIL_LEN or "@" not in cleaned:
        return None
    local, _, domain = cleaned.partition("@")
    if not local or not domain or "." not in domain:
        return None
    return cleaned


def hash_password(password: str) -> str:
    """PBKDF2-SHA256 password hash (stdlib; no bcrypt C extension required)."""
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password) > 256:
        raise ValueError("password too long")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iters_s, salt_hex, digest_hex = (password_hash or "").split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(candidate, expected)


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def mint_session(
    name: str,
    room_id: str = "legacy",
    *,
    can_create: bool = False,
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    sid: Optional[str] = None,
) -> tuple[str, str]:
    """Return (signed_cookie_value, session_id)."""
    session_id = (sid or "").strip() or new_session_id()
    payload: Dict[str, Any] = {
        "name": name,
        "room_id": room_id or "legacy",
        "can_create": bool(can_create),
        "iat": int(time.time()),
        "sid": session_id,
    }
    if user_id:
        payload["user_id"] = str(user_id)
    if email:
        payload["email"] = str(email)
    return _serializer().dumps(payload), session_id


def read_identity(cookie_value: Optional[str]) -> Optional[Dict[str, str]]:
    """Verify signature only. Callers must also check the session registry."""
    if not cookie_value:
        return None
    try:
        data = _serializer().loads(cookie_value, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    sid = str(data.get("sid") or "").strip()
    if not sid or len(sid) > 80:
        # Pre-registry cookies fail closed.
        return None
    name = normalize_name(str(data.get("name") or ""))
    if not name:
        return None
    room_id = str(data.get("room_id") or "legacy").strip()
    if not room_id or len(room_id) > 80:
        return None
    out: Dict[str, str] = {
        "name": name,
        "room_id": room_id,
        "can_create": "1" if data.get("can_create") else "",
        "sid": sid,
    }
    user_id = str(data.get("user_id") or "").strip()
    if user_id:
        out["user_id"] = user_id
    email = str(data.get("email") or "").strip()
    if email:
        out["email"] = email
    return out


def read_session(cookie_value: Optional[str]) -> Optional[str]:
    """Backward-compatible helper returning only the display name."""
    identity = read_identity(cookie_value)
    return identity["name"] if identity else None


def new_csrf() -> str:
    return secrets.token_urlsafe(16)


def new_user_id() -> str:
    # URL-safe but must start with alphanumeric for path safety.
    return "u" + secrets.token_hex(12)


def new_auth_token() -> str:
    return secrets.token_urlsafe(32)


def hash_auth_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def utc_expiry_iso(*, hours: float = 24.0) -> str:
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")
