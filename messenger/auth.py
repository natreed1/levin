"""Invite-token gate and signed session cookies."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Dict, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "messenger_session"
MAX_NAME_LEN = 40
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


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
    return URLSafeTimedSerializer(session_secret(), salt="messenger-session-v1")


def invite_ok(candidate: str) -> bool:
    expected = invite_token()
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


def normalize_name(name: str) -> Optional[str]:
    cleaned = " ".join((name or "").strip().split())
    if not cleaned or len(cleaned) > MAX_NAME_LEN:
        return None
    return cleaned


def mint_session(name: str, room_id: str = "legacy") -> str:
    return _serializer().dumps(
        {"name": name, "room_id": room_id or "legacy", "iat": int(time.time())}
    )


def read_identity(cookie_value: Optional[str]) -> Optional[Dict[str, str]]:
    if not cookie_value:
        return None
    try:
        data = _serializer().loads(cookie_value, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    name = normalize_name(str(data.get("name") or ""))
    if not name:
        return None
    room_id = str(data.get("room_id") or "legacy").strip()
    if not room_id or len(room_id) > 80:
        return None
    return {"name": name, "room_id": room_id}


def read_session(cookie_value: Optional[str]) -> Optional[str]:
    """Backward-compatible helper returning only the display name."""
    identity = read_identity(cookie_value)
    return identity["name"] if identity else None


def new_csrf() -> str:
    return secrets.token_urlsafe(16)
