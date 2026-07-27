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

# LaunchAgents often get a stripped PATH (/usr/bin:/bin) that omits Homebrew.
_CLOUDFLARED_CANDIDATES = (
    "/opt/homebrew/bin/cloudflared",
    "/usr/local/bin/cloudflared",
)


def _cloudflared_bin() -> Optional[str]:
    found = shutil.which("cloudflared")
    if found:
        return found
    for path in _CLOUDFLARED_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _durable_tunnel_path() -> Path:
    return _data_dir() / "durable_tunnel.json"


def load_durable_tunnel() -> dict[str, Any]:
    path = _durable_tunnel_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_durable_tunnel(
    *,
    token: str,
    public_base_url: str,
) -> dict[str, Any]:
    """Persist a named Cloudflare tunnel (stable hostname across reboots)."""
    tok = (token or "").strip()
    url = (public_base_url or "").strip().rstrip("/")
    if not tok:
        raise ValueError("Cloudflare tunnel token is required")
    if not url.startswith("https://"):
        raise ValueError("public_base_url must be https://… (your stable hostname)")
    if url.endswith("/v1"):
        url = url[:-3]
    payload = {
        "token": tok,
        "public_base_url": url,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _durable_tunnel_path()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return durable_tunnel_public()


def clear_durable_tunnel() -> dict[str, Any]:
    path = _durable_tunnel_path()
    try:
        path.unlink(missing_ok=True)
    except TypeError:
        # py3.7 compat — not needed on 3.12, but keep safe
        if path.exists():
            path.unlink()
    except OSError:
        pass
    return durable_tunnel_public()


def durable_tunnel_public() -> dict[str, Any]:
    cfg = load_durable_tunnel()
    url = str(cfg.get("public_base_url") or "").strip().rstrip("/")
    has_token = bool(str(cfg.get("token") or "").strip())
    running = False
    try:
        with _proc_lock:
            if (
                _companion_tunnel_proc is not None
                and _companion_tunnel_proc.poll() is None
            ):
                running = True
    except NameError:
        running = False
    live = False
    if url:
        try:
            live = _tunnel_still_live(url, is_companion=True)
        except NameError:
            live = False
    return {
        "configured": bool(has_token and url),
        "mode": "named" if (has_token and url) else "quick",
        "public_base_url": url or None,
        "running": running,
        "reachable": live,
        "message": (
            f"Durable tunnel ready at {url}"
            if has_token and url
            else "No durable tunnel yet — quick tunnels get a new random name each time."
        ),
    }


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
    # Merge so tunnel bookkeeping (tunnel_base / companion_tunnel_base) survives
    # pipeline_start overwrites that only set runtime/model/base_url.
    existing = _load_state()
    merged = dict(existing)
    merged.update(data)
    _state_path().write_text(json.dumps(merged, indent=2), encoding="utf-8")


def _terminate_proc(proc: Optional[subprocess.Popen]) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def _https_probe(url: str, *, headers: Optional[dict[str, str]] = None, timeout: float = 4.0) -> bool:
    try:
        req = urllib.request.Request(
            url,
            headers=headers or {"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(64)
        return True
    except Exception:
        return False


# Cloudflare anycast IPv4s observed for trycloudflare.com — used when this Mac's
# resolver only returns AAAA (no route) so local probes still work.
_CF_ANYCAST_V4 = ("104.16.230.132", "104.16.231.132", "104.19.140.88")


def _https_probe_trycloudflare(
    url: str, *, headers: Optional[dict[str, str]] = None, timeout: float = 4.0
) -> bool:
    if _https_probe(url, headers=headers, timeout=timeout):
        return True
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if "trycloudflare.com" not in host:
        return False
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    hdrs = dict(headers or {"Accept": "application/json"})
    hdrs["Host"] = host
    import http.client
    import socket
    import ssl

    ctx = ssl.create_default_context()
    for ip in _CF_ANYCAST_V4:
        try:
            sock = socket.create_connection((ip, 443), timeout=timeout)
            ssock = ctx.wrap_socket(sock, server_hostname=host)
            conn = http.client.HTTPSConnection(ip, 443, timeout=timeout)
            conn.sock = ssock
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            resp.read(64)
            ok = 200 <= resp.status < 500 and resp.status != 404
            conn.close()
            if ok:
                return True
        except Exception:
            continue
    return False


def _tunnel_probe_url(base: str, *, is_companion: bool) -> str:
    root = base.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    if is_companion:
        return f"{root}/healthz"
    return f"{root}/v1/models"


def _tunnel_still_live(base: str, *, is_companion: bool) -> bool:
    headers: dict[str, str] = {"Accept": "application/json"}
    if not is_companion:
        tok = (_load_state().get("token") or "") or ensure_gateway_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    probe = _tunnel_probe_url(base, is_companion=is_companion)
    if "trycloudflare.com" in probe:
        return _https_probe_trycloudflare(probe, headers=headers)
    return _https_probe(probe, headers=headers)


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
_tunnel_proc: Optional[subprocess.Popen] = None  # gateway (11435) tunnel
_companion_tunnel_proc: Optional[subprocess.Popen] = None  # companion (8791) tunnel
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


def _candidate_tunnel_bases(*, is_companion: bool, state_key: str) -> list[str]:
    """URLs we might already have live (without /v1)."""
    state = _load_state()
    out: list[str] = []
    for key in (state_key, "companion_tunnel_base" if is_companion else "tunnel_base"):
        raw = str(state.get(key) or "").strip().rstrip("/")
        if raw.startswith("https://"):
            if raw.endswith("/v1"):
                raw = raw[:-3]
            if raw not in out:
                out.append(raw)
    if not is_companion:
        bu = str(state.get("base_url") or "").strip().rstrip("/")
        if bu.startswith("https://"):
            if bu.endswith("/v1"):
                bu = bu[:-3]
            if bu not in out:
                out.append(bu)
    return out


def _try_named_cloudflared_tunnel(local_port: int) -> Optional[str]:
    """Start or reuse a *named* Cloudflare tunnel (stable hostname).

    Requires one-time durable_tunnel.json with Cloudflare tunnel token + public URL.
    Ingress in the Cloudflare dashboard must point at
    ``http://127.0.0.1:{local_port}`` (Companion).
    """
    global _companion_tunnel_proc
    cfg = load_durable_tunnel()
    token = str(cfg.get("token") or "").strip()
    url = str(cfg.get("public_base_url") or "").strip().rstrip("/")
    if not token or not url.startswith("https://"):
        return None
    if url.endswith("/v1"):
        url = url[:-3]

    if local_port != LISTEN_PORT:
        # Named tunnel ingress is configured for Companion only.
        return None

    if _tunnel_still_live(url, is_companion=True):
        st = _load_state()
        st["companion_tunnel_base"] = url
        _save_state(st)
        logger.info("reusing live named tunnel %s", url)
        return url

    with _proc_lock:
        tracked = _companion_tunnel_proc
        if tracked is not None and tracked.poll() is None:
            st = _load_state()
            st["companion_tunnel_base"] = url
            _save_state(st)
            logger.info("reusing tracked named tunnel process for %s", url)
            return url

    cf = _cloudflared_bin()
    if not cf:
        logger.warning("cloudflared not found; cannot start named tunnel")
        return None

    log_path = _data_dir() / "run" / "companion_named_tunnel.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    with _proc_lock:
        _terminate_proc(_companion_tunnel_proc)
        try:
            log_f = open(log_path, "w", encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot write named tunnel log %s: %s", log_path, exc)
            return None
        # Token mode uses remote ingress from the Cloudflare dashboard.
        new_proc = subprocess.Popen(
            [cf, "tunnel", "--no-autoupdate", "run", "--token", token],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            log_f.close()
        except OSError:
            pass
        _companion_tunnel_proc = new_proc
        proc = new_proc

    registered = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            logger.warning(
                "named cloudflared exited early (log=%s)", log_path
            )
            break
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "Registered tunnel connection" in text:
            registered = True
            break
        if _tunnel_still_live(url, is_companion=True):
            registered = True
            break
        time.sleep(0.25)

    if not registered and proc.poll() is not None:
        with _proc_lock:
            if _companion_tunnel_proc is proc:
                _companion_tunnel_proc = None
        return None

    # Grace for edge/DNS; accept registered even if local probe fails (IPv6 quirks).
    ready_deadline = time.time() + 20
    while time.time() < ready_deadline:
        if _tunnel_still_live(url, is_companion=True):
            st = _load_state()
            st["companion_tunnel_base"] = url
            _save_state(st)
            logger.info("named tunnel ready %s", url)
            return url
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    if registered and proc.poll() is None:
        st = _load_state()
        st["companion_tunnel_base"] = url
        _save_state(st)
        logger.warning(
            "named tunnel %s registered; local probe inconclusive — returning for cloud probe",
            url,
        )
        return url

    logger.warning("named tunnel %s failed to come up", url)
    _terminate_proc(proc)
    with _proc_lock:
        if _companion_tunnel_proc is proc:
            _companion_tunnel_proc = None
    return None


def _try_cloudflared_tunnel(
    local_port: int, *, state_key: str = "tunnel_base"
) -> Optional[str]:
    """Start or reuse a quick tunnel; return https base without /v1, or None.

    ``state_key`` lets companion (8791) and gateway (11435) keep separate tunnels.
    Reuses a still-reachable URL instead of spawning a new cloudflared each time
    (establish/reconnect used to leak orphans and race DNS).

    Readiness: prefer a successful local HTTPS probe, but if this machine cannot
    resolve the trycloudflare hostname (common when the edge publishes AAAA-only
    and local IPv6/DNS is broken), accept the tunnel once cloudflared logs
    ``Registered tunnel connection``. Flyleaf's cloud probe is the real check.
    """
    global _tunnel_proc, _companion_tunnel_proc
    cf = _cloudflared_bin()
    if not cf:
        logger.warning("cloudflared not found on PATH or common Homebrew locations")
        return None
    is_companion = local_port == LISTEN_PORT

    for cand in _candidate_tunnel_bases(is_companion=is_companion, state_key=state_key):
        if _tunnel_still_live(cand, is_companion=is_companion):
            st = _load_state()
            st[state_key] = cand
            _save_state(st)
            logger.info("reusing live tunnel %s", cand)
            return cand

    # If our tracked cloudflared is still running, reuse its saved URL even when
    # this Mac cannot locally resolve trycloudflare DNS (AAAA-only / IPv6 quirks).
    with _proc_lock:
        tracked = _companion_tunnel_proc if is_companion else _tunnel_proc
        if tracked is not None and tracked.poll() is None:
            cands = _candidate_tunnel_bases(is_companion=is_companion, state_key=state_key)
            if cands:
                logger.info("reusing tracked cloudflared tunnel %s", cands[0])
                return cands[0]

    log_path = _data_dir() / "run" / (
        "companion_tunnel.log" if is_companion else "gateway_tunnel.log"
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    with _proc_lock:
        old = _companion_tunnel_proc if is_companion else _tunnel_proc
        _terminate_proc(old)
        try:
            log_f = open(log_path, "w", encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot write tunnel log %s: %s", log_path, exc)
            return None
        new_proc = subprocess.Popen(
            [
                cf,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{local_port}",
                "--no-autoupdate",
            ],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            log_f.close()
        except OSError:
            pass
        if is_companion:
            _companion_tunnel_proc = new_proc
        else:
            _tunnel_proc = new_proc
        proc = new_proc

    url: Optional[str] = None
    registered = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if url is None:
            for line in text.splitlines():
                if "trycloudflare.com" not in line:
                    continue
                for part in line.split():
                    cleaned = part.strip("|$ \t\r\n")
                    if cleaned.startswith("https://") and "trycloudflare.com" in cleaned:
                        url = cleaned.rstrip("/")
                        break
                if url:
                    break
        if "Registered tunnel connection" in text:
            registered = True
        if url and registered:
            break
        if url and _tunnel_still_live(url, is_companion=is_companion):
            break
        time.sleep(0.25)

    if not url:
        logger.warning("cloudflared did not print a trycloudflare URL (log=%s)", log_path)
        _terminate_proc(proc)
        with _proc_lock:
            if is_companion and _companion_tunnel_proc is proc:
                _companion_tunnel_proc = None
            elif (not is_companion) and _tunnel_proc is proc:
                _tunnel_proc = None
        return None

    # Extra grace for DNS/edge after registration (or after URL print).
    ready_deadline = time.time() + (15 if registered else 45)
    while time.time() < ready_deadline:
        if _tunnel_still_live(url, is_companion=is_companion):
            st = _load_state()
            st[state_key] = url
            _save_state(st)
            logger.info("tunnel ready (local probe ok) %s", url)
            return url
        if proc.poll() is not None:
            logger.warning("cloudflared exited before tunnel became reachable")
            break
        # Re-check registration in case it landed after URL print.
        try:
            if "Registered tunnel connection" in log_path.read_text(
                encoding="utf-8", errors="replace"
            ):
                registered = True
        except OSError:
            pass
        time.sleep(0.5)

    if registered and proc.poll() is None:
        st = _load_state()
        st[state_key] = url
        _save_state(st)
        logger.warning(
            "tunnel %s registered but not locally reachable "
            "(often IPv6-only DNS on this Mac); returning URL for cloud probe",
            url,
        )
        return url

    logger.warning("tunnel URL %s never became reachable", url)
    _terminate_proc(proc)
    with _proc_lock:
        if is_companion and _companion_tunnel_proc is proc:
            _companion_tunnel_proc = None
        elif (not is_companion) and _tunnel_proc is proc:
            _tunnel_proc = None
    return None


def prepare_cloud_link() -> dict[str, Any]:
    """Expose this companion via a public tunnel so Flyleaf can reach it.

    Called from the *browser* (which can hit 127.0.0.1) when the user is on
    levin.fly.dev — the cloud server cannot dial localhost itself.

    Prefers a configured *named* Cloudflare tunnel (stable hostname). Falls
    back to ephemeral quick tunnels only when none is configured.
    """
    if not _cloudflared_bin():
        return {
            "ok": False,
            "error": "cloudflared_missing",
            "message": (
                "Install cloudflared once so the website can reach this computer: "
                "brew install cloudflared"
            ),
            "install_hint": "brew install cloudflared",
        }

    named = _try_named_cloudflared_tunnel(LISTEN_PORT)
    if named:
        return {
            "ok": True,
            "public_base_url": named,
            "local_base_url": f"http://{LISTEN_HOST}:{LISTEN_PORT}",
            "tunnel_mode": "named",
            "stable": True,
            "message": (
                f"Durable tunnel online at {named} — same hostname after reboot."
            ),
        }

    cfg = load_durable_tunnel()
    if str(cfg.get("token") or "").strip():
        return {
            "ok": False,
            "error": "named_tunnel_failed",
            "tunnel_mode": "named",
            "stable": True,
            "public_base_url": str(cfg.get("public_base_url") or "") or None,
            "message": (
                "Durable tunnel is configured but failed to start. "
                "Check that the Cloudflare hostname points at "
                f"http://127.0.0.1:{LISTEN_PORT} and the token is valid."
            ),
        }

    url = _try_cloudflared_tunnel(
        LISTEN_PORT, state_key="companion_tunnel_base"
    )
    if not url:
        return {
            "ok": False,
            "error": "tunnel_failed",
            "tunnel_mode": "quick",
            "stable": False,
            "message": (
                "Could not open a public tunnel. Install cloudflared, or set up a "
                "durable named tunnel under Settings → Open source (recommended)."
            ),
        }
    return {
        "ok": True,
        "public_base_url": url,
        "local_base_url": f"http://{LISTEN_HOST}:{LISTEN_PORT}",
        "tunnel_mode": "quick",
        "stable": False,
        "message": (
            "Temporary tunnel ready. For a hostname that survives reboot, "
            "configure a durable Cloudflare tunnel once under Settings → Open source."
        ),
    }


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
    tunnel_base = ""
    if mode == "tunnel":
        tun = _try_cloudflared_tunnel(GATEWAY_PORT)
        if tun:
            tunnel_base = tun.rstrip("/")
            base_url = tunnel_base + "/v1"
            mode = "tunnel"
        else:
            # Fall back to loopback; cloud messenger will report needs if unreachable
            mode = "loopback"

    state: dict[str, Any] = {
        "runtime": runtime_id,
        "model": model_id,
        "base_url": base_url,
        "token": token,
        "gateway_mode": mode,
        "upstream": upstream,
        "gateway_port": GATEWAY_PORT,
        "updated_at": time.time(),
    }
    if tunnel_base:
        state["tunnel_base"] = tunnel_base
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
    global _gateway_proc, _tunnel_proc, _companion_tunnel_proc
    with _proc_lock:
        for proc in (_companion_tunnel_proc, _tunnel_proc, _gateway_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        _companion_tunnel_proc = None
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

    def _cors_headers(self) -> None:
        # Browser on Flyleaf (levin.fly.dev) must call this loopback companion.
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type",
        )
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_bytes(
        self, code: int, body: bytes, content_type: str = "application/json"
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        self._send_bytes(code, json.dumps(payload).encode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

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
  <p>On <strong>levin.fly.dev</strong>, click <strong>Start local model</strong> — this computer opens the tunnel (durable hostname if configured).</p>
  <ol>
    <li>One-time: Settings → Open source → Durable tunnel (Cloudflare token + hostname)</li>
    <li>Then one click: <strong>Start local model</strong> on Flyleaf</li>
    <li>Optional: <code>cat {token_path}</code> for manual companion link</li>
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

    def _is_loopback_client(self) -> bool:
        host = str(self.client_address[0] if self.client_address else "")
        return host in {"127.0.0.1", "::1", "localhost"} or host.startswith(
            "::ffff:127."
        )

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
        # Loopback-only: website can one-click link without pasting the token.
        # Token never leaves this machine except via the user's own browser to Flyleaf.
        if path == "/browser-link-info":
            if not self._is_loopback_client():
                self._send(403, {"ok": False, "error": "loopback_only"})
                return
            self._send(
                200,
                {
                    "ok": True,
                    "base_url": f"http://{LISTEN_HOST}:{LISTEN_PORT}",
                    "token": ensure_companion_token(),
                    "ollama": ollama_available(),
                    "ollama_installed": ollama_installed(),
                    "tunnel": durable_tunnel_public(),
                },
            )
            return
        # Loopback: read durable tunnel status without pasting the companion token.
        if path == "/tunnel/config":
            if not self._is_loopback_client():
                self._send(403, {"ok": False, "error": "loopback_only"})
                return
            self._send(200, {"ok": True, **durable_tunnel_public()})
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
        # Loopback durable-tunnel setup from the Flyleaf page (one-time).
        if path == "/tunnel/config" and self._is_loopback_client():
            body = self._read_json()
            try:
                public = save_durable_tunnel(
                    token=str(body.get("token") or ""),
                    public_base_url=str(body.get("public_base_url") or ""),
                )
            except ValueError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            # Boot it immediately so one-click Start can reuse the same name.
            started = _try_named_cloudflared_tunnel(LISTEN_PORT)
            self._send(
                200,
                {
                    "ok": True,
                    **public,
                    "booted": bool(started),
                    "message": (
                        f"Durable tunnel saved"
                        + (f" and online at {started}" if started else ". Click Start local model to bring it online.")
                    ),
                },
            )
            return
        if not self._check_auth():
            return
        body = self._read_json()
        if path == "/prepare-cloud-link":
            result = prepare_cloud_link()
            self._send(200 if result.get("ok") else 400, result)
            return
        if path == "/tunnel/config":
            try:
                public = save_durable_tunnel(
                    token=str(body.get("token") or ""),
                    public_base_url=str(body.get("public_base_url") or ""),
                )
            except ValueError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            self._send(200, {"ok": True, **public})
            return
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

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/tunnel/config" and self._is_loopback_client():
            self._send(200, {"ok": True, **clear_durable_tunnel()})
            return
        if not self._check_auth():
            return
        if path == "/tunnel/config":
            self._send(200, {"ok": True, **clear_durable_tunnel()})
            return
        self._send(404, {"ok": False, "error": "not_found"})


def main() -> None:
    import signal

    # Survive parent-shell exit when started from agent/QA wrappers (`python … &`).
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
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
    # Bring durable tunnel up on boot so the hostname is ready before the first click.
    if load_durable_tunnel().get("token"):
        threading.Thread(
            target=lambda: _try_named_cloudflared_tunnel(LISTEN_PORT),
            name="durable-tunnel-boot",
            daemon=True,
        ).start()
        print("Durable tunnel: starting in background (stable hostname).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    except Exception:
        logger.exception("companion crashed")
        raise
    finally:
        pipeline_stop()
        server.server_close()


if __name__ == "__main__":
    main()
