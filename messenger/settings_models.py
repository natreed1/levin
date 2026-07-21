"""Settings → Models orchestration: companion proxy, establish, enable."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from messenger.companion import registry as companion_registry
from messenger.model_link import DEFAULT_PULL_MODEL, registry as model_registry

logger = logging.getLogger("messenger.settings_models")


def _companion_request(
    user_id: str,
    path: str,
    *,
    method: str = "POST",
    body: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    entry = companion_registry.get(user_id)
    if not entry:
        return {
            "ok": False,
            "error": "needs_companion",
            "message": (
                "Install Local Companion on this computer, then link it under Settings. "
                "Run: python -m messenger.companion_app"
            ),
        }
    base = str(entry["base_url"]).rstrip("/")
    url = f"{base}{path}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if entry.get("token"):
        headers["Authorization"] = f"Bearer {entry['token']}"
    data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
    if method == "GET":
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"ok": False, "error": str(exc)}
        if isinstance(err_body, dict):
            err_body.setdefault("ok", False)
            return err_body
        return {"ok": False, "error": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "error": "companion_unreachable",
            "message": "Local Companion is not reachable. Open it on this computer, then retry.",
            "detail": str(exc),
        }


def companion_health(user_id: str) -> dict[str, Any]:
    probe = companion_registry.probe(user_id, timeout=2.0)
    return {
        "linked": bool(probe.get("linked")),
        "reachable": bool(probe.get("reachable")),
        "base_url": probe.get("base_url") or "",
        "error": probe.get("error"),
        "install_hint": "python -m messenger.companion_app",
    }


def discover(user_id: str) -> dict[str, Any]:
    result = _companion_request(user_id, "/local-model/discover", method="POST", body={})
    if not result.get("ok") and result.get("error") in {
        "needs_companion",
        "companion_unreachable",
    }:
        return result
    if "candidates" not in result and result.get("ok") is not False:
        # health-only failure shape
        pass
    result.setdefault("ok", True)
    return result


def pull_model(user_id: str, model: str = "") -> dict[str, Any]:
    return _companion_request(
        user_id,
        "/local-model/pull",
        method="POST",
        body={"model": model or DEFAULT_PULL_MODEL},
        timeout=10.0,
    )


def pull_job_status(user_id: str, job_id: str) -> dict[str, Any]:
    return _companion_request(
        user_id,
        f"/local-model/pull/{job_id}",
        method="GET",
        timeout=10.0,
    )


def _prefer_tunnel() -> bool:
    # Cloud / remote messenger should ask companion for a tunnel when possible.
    fly = os.environ.get("FLY_APP_NAME") or os.environ.get("FLY_ALLOC_ID")
    return bool(fly)


def establish_pipeline(user_id: str, profile_id: str) -> dict[str, Any]:
    reg = model_registry()
    profile = reg.get_profile(user_id, profile_id)
    if not profile:
        return {"ok": False, "error": "profile_not_found"}
    if profile.get("category") != "open_source":
        return {"ok": False, "error": "not_open_source"}

    prefer_tunnel = _prefer_tunnel()
    result = _companion_request(
        user_id,
        "/local-model/pipeline/start",
        method="POST",
        body={
            "runtime": profile.get("runtime") or "ollama",
            "model": profile.get("model") or DEFAULT_PULL_MODEL,
            "source": profile.get("source") or {},
            "gateway_mode": "tunnel" if prefer_tunnel else "auto",
            "prefer_tunnel": prefer_tunnel,
        },
        timeout=120.0,
    )
    if not result.get("ok"):
        return result

    base_url = str(result.get("base_url") or "")
    token = str(result.get("token") or "")
    try:
        public = reg.set_pipeline_route(
            user_id,
            profile_id,
            base_url=base_url,
            api_key=token,
            gateway_mode=str(result.get("gateway_mode") or "loopback"),
            setup_complete=True,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Probe from cloud/server side
    probe = reg.probe_profile(user_id, profile_id, timeout=12.0)
    return {
        "ok": True,
        "profile": public,
        "reachable": bool(probe.get("reachable")),
        "message": (
            "Connected and saved. You can turn it on with one click."
            if probe.get("reachable")
            else "Saved. Route not reachable from this server yet — turn On when Companion is running."
        ),
        "probe": probe,
    }


def rebuild_pipeline(user_id: str, profile_id: str) -> dict[str, Any]:
    return establish_pipeline(user_id, profile_id)


def enable_profile(user_id: str, profile_id: str) -> dict[str, Any]:
    reg = model_registry()
    profile = reg.get_profile(user_id, profile_id)
    if not profile:
        return {"ok": False, "error": "profile_not_found"}

    if profile.get("category") == "frontier":
        public = reg.activate(user_id, profile_id)
        probe = reg.probe_profile(user_id, profile_id)
        return {
            "ok": True,
            "profile": public,
            "reachable": bool(probe.get("reachable")),
            "message": probe.get("message"),
        }

    if not profile.get("setup_complete") or not profile.get("pipeline_route"):
        return {
            "ok": False,
            "error": "setup_incomplete",
            "message": "Finish Connect & save before turning this on.",
        }

    # Probe saved route first
    probe = reg.probe_profile(user_id, profile_id, timeout=8.0)
    if not probe.get("reachable"):
        recon = _companion_request(
            user_id,
            "/local-model/pipeline/reconnect",
            method="POST",
            body={"prefer_tunnel": _prefer_tunnel()},
            timeout=90.0,
        )
        if recon.get("ok"):
            try:
                reg.set_pipeline_route(
                    user_id,
                    profile_id,
                    base_url=str(recon.get("base_url") or ""),
                    api_key=str(recon.get("token") or ""),
                    gateway_mode=str(recon.get("gateway_mode") or "loopback"),
                    setup_complete=True,
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            probe = reg.probe_profile(user_id, profile_id, timeout=8.0)
        elif recon.get("error") in {"needs_companion", "companion_unreachable"}:
            return {
                "ok": False,
                "error": recon.get("error"),
                "message": recon.get("message")
                or "Open Local Companion, then turn On again.",
            }

    if not probe.get("reachable"):
        return {
            "ok": False,
            "error": "unreachable",
            "message": probe.get("message")
            or "Model route unreachable. Open Local Companion and retry.",
            "probe": probe,
        }

    try:
        public = reg.enable_open_source(user_id, profile_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "profile": public,
        "reachable": True,
        "message": "On — specialists will use this local model.",
    }


def disable_profile(user_id: str, profile_id: str) -> dict[str, Any]:
    reg = model_registry()
    try:
        public = reg.disable_open_source(user_id, profile_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "profile": public, "message": "Off."}
