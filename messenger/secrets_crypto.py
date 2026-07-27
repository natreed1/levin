"""Encrypt short secrets (API keys, gateway tokens) at rest.

Uses stdlib only: HMAC-SHA256 keystream + truncated HMAC tag, keyed from
MESSENGER_SESSION_SECRET. Not a substitute for KMS, but avoids plaintext keys
in model_links.json.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional


PREFIX = "enc:v1:"


def _master_key() -> bytes:
    secret = (
        os.environ.get("MESSENGER_SESSION_SECRET")
        or os.environ.get("MESSENGER_SECRET")
        or "dev-insecure-messenger-secret"
    ).encode("utf-8")
    return hashlib.sha256(b"analyst-ledger:model-keys:" + secret).digest()


def encrypt_secret(plaintext: str) -> str:
    raw = (plaintext or "").encode("utf-8")
    if not raw:
        return ""
    key = _master_key()
    nonce = secrets.token_bytes(16)
    keystream = b""
    counter = 0
    while len(keystream) < len(raw):
        keystream += hmac.new(
            key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(raw, keystream))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    blob = base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")
    return PREFIX + blob


def decrypt_secret(stored: str) -> str:
    text = str(stored or "")
    if not text:
        return ""
    if not text.startswith(PREFIX):
        # Legacy plaintext
        return text
    try:
        blob = base64.urlsafe_b64decode(text[len(PREFIX) :].encode("ascii"))
    except (ValueError, OSError):
        return ""
    if len(blob) < 32:
        return ""
    nonce, tag, cipher = blob[:16], blob[16:32], blob[32:]
    key = _master_key()
    expect = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expect):
        return ""
    keystream = b""
    counter = 0
    while len(keystream) < len(cipher):
        keystream += hmac.new(
            key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(cipher, keystream)).decode("utf-8", errors="replace")


def secret_suffix(plaintext: Optional[str]) -> str:
    key = str(plaintext or "")
    if len(key) >= 4:
        return "…" + key[-4:]
    return ""
