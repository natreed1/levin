"""Outbound email for verification + password reset.

Backends (first match wins):
  1. MESSENGER_RESEND_API_KEY  → Resend HTTP API
  2. MESSENGER_SMTP_HOST       → stdlib SMTP
  3. otherwise                 → console (logs the message; local/dev)

Set MESSENGER_EMAIL_FROM (e.g. "Workflow <onboarding@resend.dev>").
Set MESSENGER_PUBLIC_BASE_URL if links should not use the request host
(e.g. https://levin.fly.dev).
"""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any, Optional

logger = logging.getLogger("messenger.emailer")


def email_from() -> str:
    return (
        os.environ.get("MESSENGER_EMAIL_FROM", "").strip()
        or "Workflow <noreply@localhost>"
    )


def public_base_url(request_base: Optional[str] = None) -> str:
    configured = (os.environ.get("MESSENGER_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    if request_base:
        return str(request_base).rstrip("/")
    fly = (os.environ.get("FLY_APP_NAME") or "").strip()
    if fly:
        return f"https://{fly}.fly.dev"
    return "http://127.0.0.1:8790"


def email_backend() -> str:
    if (os.environ.get("MESSENGER_RESEND_API_KEY") or "").strip():
        return "resend"
    if (os.environ.get("MESSENGER_SMTP_HOST") or "").strip():
        return "smtp"
    return "console"


def expose_dev_links() -> bool:
    raw = (os.environ.get("MESSENGER_EMAIL_DEV_EXPOSE") or "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    # Never expose magic links on Fly unless explicitly opted in.
    if (os.environ.get("FLY_APP_NAME") or "").strip():
        return False
    # Local/dev: expose when no real mail backend is configured.
    return email_backend() == "console"


def auto_verify_on_signup() -> bool:
    """Skip inbox verification when mail cannot reach the user.

    Local keeps the verify flow (dev links). On Fly with only the console
    backend, verification emails never arrive — auto-verify so signup works.
    Override with MESSENGER_AUTO_VERIFY=1/0.
    """
    raw = (os.environ.get("MESSENGER_AUTO_VERIFY") or "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    return bool((os.environ.get("FLY_APP_NAME") or "").strip()) and email_backend() == "console"


def send_email(*, to: str, subject: str, text: str, html: Optional[str] = None) -> dict[str, Any]:
    backend = email_backend()
    if backend == "resend":
        return _send_resend(to=to, subject=subject, text=text, html=html)
    if backend == "smtp":
        return _send_smtp(to=to, subject=subject, text=text, html=html)
    logger.info("EMAIL console to=%s subject=%s\n%s", to, subject, text)
    return {"ok": True, "backend": "console"}


def _send_resend(*, to: str, subject: str, text: str, html: Optional[str]) -> dict[str, Any]:
    api_key = (os.environ.get("MESSENGER_RESEND_API_KEY") or "").strip()
    payload: dict[str, Any] = {
        "from": email_from(),
        "to": [to],
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        return {"ok": True, "backend": "resend", "id": body.get("id")}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        logger.warning("Resend HTTP %s: %s", exc.code, err)
        raise RuntimeError(f"email_send_failed: {err}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"email_send_failed: {exc}") from exc


def _send_smtp(*, to: str, subject: str, text: str, html: Optional[str]) -> dict[str, Any]:
    host = (os.environ.get("MESSENGER_SMTP_HOST") or "").strip()
    port = int(os.environ.get("MESSENGER_SMTP_PORT") or "587")
    user = (os.environ.get("MESSENGER_SMTP_USER") or "").strip()
    password = (os.environ.get("MESSENGER_SMTP_PASSWORD") or "").strip()
    use_tls = (os.environ.get("MESSENGER_SMTP_TLS") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }

    msg = EmailMessage()
    msg["From"] = email_from()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls(context=context)
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)
    return {"ok": True, "backend": "smtp"}


def send_verification_email(*, to: str, verify_url: str, display_name: str) -> dict[str, Any]:
    subject = "Verify your Workflow email"
    safe_name = html.escape(display_name or "", quote=True)
    text = (
        f"Hi {display_name},\n\n"
        "Confirm your email to finish creating your Workflow account:\n"
        f"{verify_url}\n\n"
        "This link expires in 24 hours. If you did not sign up, ignore this email.\n"
    )
    html_body = (
        f"<p>Hi {safe_name},</p>"
        "<p>Confirm your email to finish creating your Workflow account:</p>"
        f'<p><a href="{verify_url}">Verify email</a></p>'
        "<p>This link expires in 24 hours.</p>"
    )
    return send_email(to=to, subject=subject, text=text, html=html_body)


def send_password_reset_email(*, to: str, reset_url: str, display_name: str) -> dict[str, Any]:
    subject = "Reset your Workflow password"
    safe_name = html.escape(display_name or "", quote=True)
    text = (
        f"Hi {display_name},\n\n"
        "Use this link to choose a new password:\n"
        f"{reset_url}\n\n"
        "This link expires in 1 hour. If you did not request a reset, ignore this email.\n"
    )
    html_body = (
        f"<p>Hi {safe_name},</p>"
        "<p>Use this link to choose a new password:</p>"
        f'<p><a href="{reset_url}">Reset password</a></p>'
        "<p>This link expires in 1 hour.</p>"
    )
    return send_email(to=to, subject=subject, text=text, html=html_body)
