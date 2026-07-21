"""Authenticated localhost gateway in front of Ollama.

Ollama itself ignores auth and must stay bound to 127.0.0.1. This gateway is
what you tunnel (ngrok / Cloudflare) so Fly can call Qwen without exposing a
naked model API.

Security layers:
  - Bearer token required (ANALYST_QWEN_API_KEY / QWEN_GATEWAY_TOKEN)
  - Path allowlist: /v1/chat/completions, /v1/models (+ optional trailing slash)
  - Method allowlist
  - Max request body size
  - Binds to 127.0.0.1 only

Run:
  QWEN_GATEWAY_TOKEN='…' python -m messenger.qwen_gateway
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("messenger.qwen_gateway")

ALLOWED_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/models",
        "/healthz",
    }
)
MAX_BODY_BYTES = 512 * 1024  # 512 KiB — chat payloads, not bulk uploads
UPSTREAM = (os.environ.get("QWEN_GATEWAY_UPSTREAM") or "http://127.0.0.1:11434").rstrip(
    "/"
)
LISTEN_HOST = os.environ.get("QWEN_GATEWAY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("QWEN_GATEWAY_PORT", "11435"))


def _token() -> str:
    return (
        os.environ.get("QWEN_GATEWAY_TOKEN")
        or os.environ.get("ANALYST_QWEN_API_KEY")
        or ""
    ).strip()


def _authorized(header_value: Optional[str], expected: str) -> bool:
    if not expected or not header_value:
        return False
    raw = header_value.strip()
    if raw.lower().startswith("bearer "):
        got = raw[7:].strip()
    else:
        got = raw
    if not got:
        return False
    return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code: int, error: str) -> None:
        self._send(code, json.dumps({"ok": False, "error": error}).encode("utf-8"))

    def _path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/healthz":
            self._send(200, b'{"ok":true,"service":"qwen-gateway"}')
            return
        if path not in {"/v1/models"}:
            self._deny(404, "not_found")
            return
        self._proxy("GET", path)

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        if path != "/v1/chat/completions":
            self._deny(404, "not_found")
            return
        self._proxy("POST", path)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No browser CORS needed — Fly server-side only — reject preflight noise.
        self._deny(405, "method_not_allowed")

    def _proxy(self, method: str, path: str) -> None:
        expected = _token()
        if not expected:
            self._deny(503, "gateway_token_not_configured")
            return
        if not _authorized(self.headers.get("Authorization"), expected):
            self._deny(401, "unauthorized")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._deny(413, "payload_too_large")
            return
        body = self.rfile.read(length) if length else b""

        upstream_url = f"{UPSTREAM}{path}"
        headers = {"Content-Type": self.headers.get("Content-Type") or "application/json"}
        # Do not forward client Authorization to Ollama (Ollama ignores it;
        # keeping secrets out of upstream logs is still good hygiene).
        req = urllib.request.Request(
            upstream_url, data=body if method == "POST" else None, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type") or "application/json"
                self._send(resp.status, payload, ctype)
        except urllib.error.HTTPError as exc:
            err = exc.read()
            self._send(
                exc.code,
                err or json.dumps({"error": str(exc)}).encode("utf-8"),
                exc.headers.get("Content-Type") or "application/json",
            )
        except urllib.error.URLError as exc:
            logger.warning("upstream unreachable: %s", exc)
            self._deny(502, "upstream_unreachable")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if not _token():
        raise SystemExit(
            "Set QWEN_GATEWAY_TOKEN or ANALYST_QWEN_API_KEY to a long random secret."
        )
    if LISTEN_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            f"Refusing to bind to {LISTEN_HOST!r} — gateway must stay on loopback. "
            "Tunnel with ngrok; do not expose this process on LAN/0.0.0.0."
        )
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), GatewayHandler)
    logger.info(
        "Qwen gateway on http://%s:%s → %s (auth required)",
        LISTEN_HOST,
        LISTEN_PORT,
        UPSTREAM,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
