"""Local Companion HTTP app — discover models + durable pipeline routes.

Run on the user's machine (loopback):

  python -m messenger.companion_app

Then link from Settings with base_url http://127.0.0.1:8791 and the printed token.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("messenger.companion_app")

LISTEN_HOST = os.environ.get("COMPANION_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("COMPANION_PORT", "8791"))
OLLAMA_URL = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
LMSTUDIO_URL = (os.environ.get("LMSTUDIO_URL") or "http://127.0.0.1:1234").rstrip("/")
GATEWAY_PORT = int(os.environ.get("QWEN_GATEWAY_PORT", "11435"))
DEFAULT_PULL = os.environ.get("ANALYST_QWEN_MODEL") or "qwen3:8b"


def _data_dir() -> Path:
    raw = os.environ.get("MESSENGER_DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / "data"
    base = base / "companion_local"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _token_path() -> Path:
    return _data_dir() / "companion_token"


def _state_path() -> Path:
    return _data_dir() / "pipeline_state.json"


def _gateway_token_path() -> Path:
    return _data_dir() / "gateway_token"


def ensure_companion_token() -> str:
    path = _token_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def _shared_gateway_token_path() -> Path:
    """Prefer the repo tunnel script token so we don't fight an existing gateway."""
    raw = os.environ.get("MESSENGER_DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / "data"
    return base / "qwen_gateway_token"


def ensure_gateway_token() -> str:
    # Prefer shared tunnel token if present (secure_qwen_tunnel.sh / existing gateway).
    shared = _shared_gateway_token_path()
    if shared.exists():
        tok = shared.read_text(encoding="utf-8").strip()
        if len(tok) >= 8:
            path = _gateway_token_path()
            path.write_text(tok + "\n", encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            return tok
    path = _gateway_token_path()
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 8:
            return tok
    token = secrets.token_urlsafe(48)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    # Also mirror into shared path so other tools stay in sync.
    try:
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(token + "\n", encoding="utf-8")
        shared.chmod(0o600)
    except OSError:
        pass
    return token


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _http_json(url: str, *, method: str = "GET", body: Any = None, timeout: float = 8.0) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8") or "{}"
        return json.loads(raw)


def ollama_available() -> bool:
    try:
        _http_json(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


def ollama_installed() -> bool:
    return bool(shutil.which("ollama")) or ollama_available()


def discover_candidates() -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    ollama_up = ollama_available()
    ollama_bin = bool(shutil.which("ollama"))
    if ollama_up:
        try:
            body = _http_json(f"{OLLAMA_URL}/api/tags", timeout=5.0)
            for m in body.get("models") or []:
                if not isinstance(m, dict):
                    continue
                name = str(m.get("name") or m.get("model") or "").strip()
                if not name:
                    continue
                candidates.append(
                    {
                        "id": f"ollama:{name}",
                        "runtime": "ollama",
                        "label": name,
                        "size_bytes": m.get("size"),
                        "ready": True,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("ollama tags failed: %s", exc)

    # LM Studio OpenAI-compatible port
    try:
        body = _http_json(f"{LMSTUDIO_URL}/v1/models", timeout=2.0)
        for m in body.get("data") or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            candidates.append(
                {
                    "id": f"lmstudio:{mid}",
                    "runtime": "lmstudio",
                    "label": mid,
                    "size_bytes": None,
                    "ready": True,
                }
            )
    except Exception:
        pass

    return {
        "ok": True,
        "candidates": candidates,
        "ollama": {
            "installed": ollama_bin or ollama_up,
            "reachable": ollama_up,
            "install_url": "https://ollama.com/download",
        },
        "recommended_model": DEFAULT_PULL,
        "empty": len(candidates) == 0,
    }


_pull_jobs: dict[str, dict[str, Any]] = {}
_pull_lock = threading.Lock()


def start_pull(model: str = "") -> dict[str, Any]:
    model_id = (model or DEFAULT_PULL).strip()
    if not ollama_installed():
        return {
            "ok": False,
            "error": "ollama_missing",
            "message": "Install Ollama first, then try again.",
            "install_url": "https://ollama.com/download",
        }
    job_id = secrets.token_hex(8)
    with _pull_lock:
        _pull_jobs[job_id] = {
            "id": job_id,
            "model": model_id,
            "status": "running",
            "progress": 0.0,
            "message": f"Downloading {model_id}…",
            "error": None,
        }

    def _run() -> None:
        try:
            # Prefer CLI pull for progress simplicity
            if shutil.which("ollama"):
                proc = subprocess.run(
                    ["ollama", "pull", model_id],
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    check=False,
                )
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "pull failed")
            else:
                # HTTP pull (no streaming progress)
                req = urllib.request.Request(
                    f"{OLLAMA_URL}/api/pull",
                    data=json.dumps({"name": model_id, "stream": False}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3600) as resp:
                    resp.read()
            with _pull_lock:
                job = _pull_jobs.get(job_id) or {}
                job.update(
                    {
                        "status": "done",
                        "progress": 1.0,
                        "message": f"{model_id} ready.",
                    }
                )
                _pull_jobs[job_id] = job
        except Exception as exc:  # noqa: BLE001
            with _pull_lock:
                job = _pull_jobs.get(job_id) or {}
                job.update(
                    {
                        "status": "error",
                        "error": str(exc),
                        "message": "Download failed.",
                    }
                )
                _pull_jobs[job_id] = job

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job": _pull_jobs[job_id]}


def pull_status(job_id: str) -> dict[str, Any]:
    with _pull_lock:
        job = _pull_jobs.get(job_id)
    if not job:
        return {"ok": False, "error": "unknown_job"}
    return {"ok": True, "job": job}


_gateway_proc: Optional[subprocess.Popen] = None
_tunnel_proc: Optional[subprocess.Popen] = None
_proc_lock = threading.Lock()


def _port_listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _gateway_healthy(token: str) -> bool:
    """Auth-sensitive check — /healthz is open, so probe /v1/models instead."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{GATEWAY_PORT}/v1/models",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _resolve_working_gateway_token(preferred: str) -> str:
    """Return a token that actually authorizes the gateway on GATEWAY_PORT."""
    if preferred and _gateway_healthy(preferred):
        return preferred
    for path in (_gateway_token_path(), _shared_gateway_token_path()):
        if not path.exists():
            continue
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 8 and _gateway_healthy(tok):
            return tok
    env_tok = (
        os.environ.get("QWEN_GATEWAY_TOKEN")
        or os.environ.get("ANALYST_QWEN_API_KEY")
        or ""
    ).strip()
    if len(env_tok) >= 8 and _gateway_healthy(env_tok):
        return env_tok
    return preferred


def _ensure_gateway(upstream: str, token: str) -> str:
    """Ensure an authenticated gateway is up; return the working token."""
    global _gateway_proc
    working = _resolve_working_gateway_token(token)
    if _gateway_healthy(working):
        return working
    if _port_listening(GATEWAY_PORT) and not _gateway_healthy(working):
        raise RuntimeError(
            f"Gateway already running on port {GATEWAY_PORT} but our token was rejected. "
            "Stop the other gateway or reuse its qwen_gateway_token."
        )
    env = os.environ.copy()
    env["QWEN_GATEWAY_TOKEN"] = working
    env["ANALYST_QWEN_API_KEY"] = working
    env["QWEN_GATEWAY_UPSTREAM"] = upstream.rstrip("/")
    env["QWEN_GATEWAY_PORT"] = str(GATEWAY_PORT)
    env["QWEN_GATEWAY_HOST"] = "127.0.0.1"
    with _proc_lock:
        if _gateway_proc and _gateway_proc.poll() is None:
            if _gateway_healthy(working):
                return working
        _gateway_proc = subprocess.Popen(
            [os.environ.get("PYTHON", "python3"), "-m", "messenger.qwen_gateway"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    for _ in range(30):
        if _gateway_healthy(working):
            return working
        time.sleep(0.15)
    raise RuntimeError("gateway failed to start")


def _try_cloudflared_tunnel(local_port: int) -> Optional[str]:
    """Start a quick tunnel; return https base without /v1, or None."""
    global _tunnel_proc
    if not shutil.which("cloudflared"):
        return None
    with _proc_lock:
        if _tunnel_proc and _tunnel_proc.poll() is None:
            state = _load_state()
            url = state.get("tunnel_base")
            if url:
                return str(url)
        _tunnel_proc = subprocess.Popen(
            [
                "cloudflared",
                "tunnel",
                "--url",
                f"http://127.0.0.1:{local_port}",
                "--no-autoupdate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    assert _tunnel_proc.stdout is not None
    deadline = time.time() + 25
    while time.time() < deadline:
        line = _tunnel_proc.stdout.readline()
        if not line:
            if _tunnel_proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        # Look for https://….trycloudflare.com
        if "trycloudflare.com" in line or "https://" in line:
            for part in line.split():
                if part.startswith("https://") and "trycloudflare.com" in part:
                    url = part.rstrip("/")
                    st = _load_state()
                    st["tunnel_base"] = url
                    _save_state(st)
                    return url
    return None


def pipeline_start(
    *,
    runtime: str = "ollama",
    model: str = "",
    gateway_mode: str = "auto",
    prefer_tunnel: bool = False,
) -> dict[str, Any]:
    runtime_id = (runtime or "ollama").strip().lower()
    model_id = (model or DEFAULT_PULL).strip()
    token = ensure_gateway_token()

    if runtime_id == "lmstudio":
        upstream = LMSTUDIO_URL
        if not _port_up(LMSTUDIO_URL):
            return {
                "ok": False,
                "error": "lmstudio_unreachable",
                "message": "LM Studio does not appear to be running on port 1234.",
            }
    else:
        if not ollama_available():
            if ollama_installed() and shutil.which("ollama"):
                # Best-effort: `ollama serve` is usually already managed by the app
                pass
            return {
                "ok": False,
                "error": "ollama_unreachable",
                "message": "Ollama is not reachable. Open the Ollama app, then try again.",
                "install_url": "https://ollama.com/download",
            }
        upstream = OLLAMA_URL
        # Ensure model present
        tags = _http_json(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        names = {
            str(m.get("name") or "")
            for m in (tags.get("models") or [])
            if isinstance(m, dict)
        }
        if model_id not in names and not any(n.startswith(model_id.split(":")[0]) for n in names):
            # Soft: allow establish to proceed if user will pull separately; else fail clearly
            return {
                "ok": False,
                "error": "model_missing",
                "message": f"{model_id} is not installed yet. Download it first.",
                "recommended_model": model_id,
            }

    try:
        token = _ensure_gateway(upstream, token)
    except RuntimeError as exc:
        return {"ok": False, "error": "gateway_failed", "message": str(exc)}

    # Persist the working token so reconnect stays consistent.
    for path in (_gateway_token_path(), _shared_gateway_token_path()):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token + "\n", encoding="utf-8")
            path.chmod(0o600)
        except OSError:
            pass

    mode = (gateway_mode or "auto").strip().lower()
    if mode == "auto":
        mode = "tunnel" if prefer_tunnel else "loopback"

    base_url = f"http://127.0.0.1:{GATEWAY_PORT}/v1"
    if mode == "tunnel":
        tun = _try_cloudflared_tunnel(GATEWAY_PORT)
        if tun:
            base_url = tun.rstrip("/") + "/v1"
            mode = "tunnel"
        else:
            # Fall back to loopback; cloud messenger will report needs if unreachable
            mode = "loopback"

    state = {
        "runtime": runtime_id,
        "model": model_id,
        "base_url": base_url,
        "token": token,
        "gateway_mode": mode,
        "upstream": upstream,
        "gateway_port": GATEWAY_PORT,
        "updated_at": time.time(),
    }
    _save_state(state)
    return {
        "ok": True,
        "base_url": base_url,
        "token": token,
        "model": model_id,
        "runtime": runtime_id,
        "gateway_mode": mode,
    }


def pipeline_reconnect(*, prefer_tunnel: bool = False) -> dict[str, Any]:
    state = _load_state()
    if not state:
        return {"ok": False, "error": "no_pipeline", "message": "No saved pipeline on this computer."}
    return pipeline_start(
        runtime=str(state.get("runtime") or "ollama"),
        model=str(state.get("model") or DEFAULT_PULL),
        gateway_mode=str(state.get("gateway_mode") or "auto"),
        prefer_tunnel=prefer_tunnel or str(state.get("gateway_mode")) == "tunnel",
    )


def pipeline_status() -> dict[str, Any]:
    state = _load_state()
    if not state:
        return {"ok": True, "active": False}
    token = str(state.get("token") or ensure_gateway_token())
    healthy = _gateway_healthy(token)
    return {
        "ok": True,
        "active": healthy,
        "base_url": state.get("base_url"),
        "model": state.get("model"),
        "runtime": state.get("runtime"),
        "gateway_mode": state.get("gateway_mode"),
        "reachable": healthy,
    }


def pipeline_stop() -> dict[str, Any]:
    global _gateway_proc, _tunnel_proc
    with _proc_lock:
        for proc in (_tunnel_proc, _gateway_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        _tunnel_proc = None
        _gateway_proc = None
    return {"ok": True, "stopped": True}


def _port_up(base: str) -> bool:
    try:
        parsed = urlparse(base if "://" in base else f"http://{base}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        import socket

        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _authorized(header: Optional[str], expected: str) -> bool:
    if not expected:
        return True
    if not header:
        return False
    raw = header.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return hmac.compare_digest(raw.encode("utf-8"), expected.encode("utf-8"))


class CompanionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - " + fmt, self.address_string(), *args)

    def _send_bytes(
        self, code: int, body: bytes, content_type: str = "application/json"
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        self._send_bytes(code, json.dumps(payload).encode("utf-8"))

    def _send_landing(self) -> None:
        """Browser-friendly status page — never embeds the companion token."""
        peer = self.client_address[0] if self.client_address else ""
        if peer not in {"127.0.0.1", "::1", "localhost"}:
            self._send(403, {"ok": False, "error": "loopback_only"})
            return
        base = f"http://{LISTEN_HOST}:{LISTEN_PORT}"
        token_path = str(_token_path())
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Local Companion</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0e1116; color:#e8eef7;
         max-width: 36rem; margin: 2.5rem auto; padding: 0 1.25rem; line-height:1.45; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 0.5rem; }}
  p {{ color:#8b9bb4; }}
  code {{ font-family: ui-monospace, monospace; background:#161b22; border:1px solid #2a3344;
         color:#e8eef7; border-radius:6px; padding:0.15rem 0.4rem; font-size:0.85rem; }}
  .ok {{ color:#34d399; font-size:0.9rem; }}
  ol {{ color:#8b9bb4; padding-left: 1.2rem; }}
  li {{ margin: 0.4rem 0; }}
</style></head><body>
  <p class="ok">Companion is running</p>
  <h1>Local Companion</h1>
  <p>URL for Settings: <code>{base}</code></p>
  <p>The link token is <strong>not</strong> shown in the browser (so it stays off screenshots and public tunnels).</p>
  <ol>
    <li>In the terminal where Companion started, copy the line <code>Token: …</code></li>
    <li>Or run: <code>cat {token_path}</code></li>
    <li>Paste URL + token under <strong>Settings → Open source → Add your own</strong></li>
  </ol>
  <p style="margin-top:1.25rem;font-size:0.85rem;">API health: <a href="/healthz" style="color:#3d9cf0">/healthz</a></p>
</body></html>
"""
        self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _check_auth(self) -> bool:
        expected = ensure_companion_token()
        if _authorized(self.headers.get("Authorization"), expected):
            return True
        self._send(401, {"ok": False, "error": "unauthorized"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            self._send_landing()
            return
        if path == "/healthz":
            self._send(
                200,
                {
                    "ok": True,
                    "service": "companion",
                    "ollama": ollama_available(),
                },
            )
            return
        if not self._check_auth():
            return
        if path == "/local-model/pipeline/status":
            self._send(200, pipeline_status())
            return
        if path.startswith("/local-model/pull/"):
            job_id = path.rsplit("/", 1)[-1]
            self._send(200, pull_status(job_id))
            return
        self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if not self._check_auth():
            return
        body = self._read_json()
        if path == "/local-model/discover":
            self._send(200, discover_candidates())
            return
        if path == "/local-model/pull":
            self._send(200, start_pull(str(body.get("model") or "")))
            return
        if path == "/local-model/pipeline/start":
            result = pipeline_start(
                runtime=str(body.get("runtime") or "ollama"),
                model=str(body.get("model") or ""),
                gateway_mode=str(body.get("gateway_mode") or "auto"),
                prefer_tunnel=bool(body.get("prefer_tunnel")),
            )
            self._send(200 if result.get("ok") else 400, result)
            return
        if path == "/local-model/pipeline/reconnect":
            result = pipeline_reconnect(
                prefer_tunnel=bool(body.get("prefer_tunnel")),
            )
            self._send(200 if result.get("ok") else 400, result)
            return
        if path == "/local-model/pipeline/stop":
            self._send(200, pipeline_stop())
            return
        self._send(404, {"ok": False, "error": "not_found"})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if LISTEN_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Companion must bind to loopback only.")
    token = ensure_companion_token()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), CompanionHandler)
    logger.info(
        "Local Companion on http://%s:%s (token saved under %s)",
        LISTEN_HOST,
        LISTEN_PORT,
        _token_path(),
    )
    print(f"Companion ready: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"Token: {token}")
    print("Link this URL + token under Settings → Models (Local Companion).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        pipeline_stop()
        server.server_close()


if __name__ == "__main__":
    main()
