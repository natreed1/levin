"""Settings → Models orchestration: companion proxy, establish, enable."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from messenger.companion import registry as companion_registry
from messenger.model_link import (
    DEFAULT_PULL_MODEL,
    profile_is_local,
    registry as model_registry,
)

logger = logging.getLogger("messenger.settings_models")


def is_local_route_failure(
    endpoint: Optional[dict[str, Any]], exc: BaseException
) -> bool:
    """True when a chat/completion failure looks like a dead local tunnel/route."""
    if not endpoint or str(endpoint.get("is_local") or "") not in {"1", "true", "True"}:
        return False
    msg = str(exc).lower()
    needles = (
        "unreachable",
        "name or service not known",
        "nodename nor servname",
        "temporary failure in name resolution",
        "connection refused",
        "connection reset",
        "timed out",
        "timeout",
        "network is unreachable",
        "http 502",
        "http 503",
        "http 504",
        "http 530",
    )
    return any(n in msg for n in needles)


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


def _apply_pipeline_result(
    user_id: str, profile_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Persist companion pipeline base_url/token onto the profile and probe."""
    if not result.get("ok"):
        return result
    reg = model_registry()
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
    probe = reg.probe_profile(user_id, profile_id, timeout=12.0)
    endpoint = reg.endpoint_for_call(user_id, profile_id=profile_id)
    return {
        "ok": True,
        "profile": public,
        "reachable": bool(probe.get("reachable")),
        "endpoint": endpoint,
        "probe": probe,
        "message": (
            "Connected and saved. You can turn it on with one click."
            if probe.get("reachable")
            else "Saved. Route not reachable from this server yet — turn On when Companion is running."
        ),
    }


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
    return _apply_pipeline_result(user_id, profile_id, result)


def rebuild_pipeline(user_id: str, profile_id: str) -> dict[str, Any]:
    return establish_pipeline(user_id, profile_id)


def recover_local_route(user_id: str, profile_id: str) -> dict[str, Any]:
    """Re-establish a dead open-source tunnel via Companion, then probe.

    Prefers ``pipeline/reconnect`` (saved companion state); falls back to the
    full Start-local-model ``pipeline/start`` path when reconnect is missing
    or still unreachable from this server.
    """
    reg = model_registry()
    profile = reg.get_profile(user_id, profile_id)
    if not profile:
        return {"ok": False, "error": "profile_not_found"}
    if profile.get("category") != "open_source":
        return {"ok": False, "error": "not_open_source"}

    prefer_tunnel = _prefer_tunnel()
    recon = _companion_request(
        user_id,
        "/local-model/pipeline/reconnect",
        method="POST",
        body={"prefer_tunnel": prefer_tunnel},
        timeout=90.0,
    )
    if recon.get("error") in {"needs_companion", "companion_unreachable"}:
        return {
            "ok": False,
            "error": recon.get("error"),
            "message": recon.get("message")
            or "Open Local Companion, then click Start local model.",
            "recovered": False,
            "reachable": False,
        }

    applied: dict[str, Any]
    if recon.get("ok"):
        applied = _apply_pipeline_result(user_id, profile_id, recon)
        applied["recovered"] = True
        if applied.get("reachable"):
            applied["message"] = "Local model route recovered."
            return applied
        logger.info(
            "reconnect still unreachable for %s/%s; falling back to establish",
            user_id,
            profile_id,
        )
    else:
        logger.info(
            "reconnect failed for %s/%s (%s); falling back to establish",
            user_id,
            profile_id,
            recon.get("error") or recon.get("message"),
        )

    established = establish_pipeline(user_id, profile_id)
    established["recovered"] = bool(established.get("ok"))
    if established.get("ok") and established.get("reachable"):
        established["message"] = "Local model route re-established."
    return established


def ensure_local_route(
    user_id: str,
    profile_id: Optional[str] = None,
    *,
    force_recover: bool = False,
    probe_timeout: float = 8.0,
) -> dict[str, Any]:
    """Probe the active/local open-source route; recover once if dead.

    Used by agent chat after a failed local call and by enable/On.
    """
    reg = model_registry()
    profile = (
        reg.get_profile(user_id, profile_id)
        if profile_id
        else reg.active_profile(user_id)
    )
    if not profile:
        return {
            "ok": False,
            "error": "profile_not_found",
            "reachable": False,
            "recovered": False,
        }
    pid = str(profile.get("id") or "")
    if not profile_is_local(profile):
        endpoint = reg.endpoint_for_call(user_id, profile_id=pid or None)
        return {
            "ok": True,
            "reachable": True,
            "recovered": False,
            "skipped": True,
            "endpoint": endpoint,
            "profile": reg.public_profile(profile),
        }

    if not force_recover:
        probe = reg.probe_profile(user_id, pid, timeout=probe_timeout)
        if probe.get("reachable"):
            return {
                "ok": True,
                "reachable": True,
                "recovered": False,
                "endpoint": reg.endpoint_for_call(user_id, profile_id=pid),
                "profile": probe.get("profile") or reg.public_profile(profile),
                "probe": probe,
            }

    recovered = recover_local_route(user_id, pid)
    return recovered


def annotate_open_source_reachability(
    user_id: str,
    profiles: list[dict[str, Any]],
    *,
    timeout: float = 4.0,
) -> list[dict[str, Any]]:
    """Attach ``reachable`` for setup-complete open-source rows (Settings UI).

    Only probes enabled local profiles so list stays snappy; disabled/incomplete
    rows get ``reachable: null``.
    """
    reg = model_registry()
    out: list[dict[str, Any]] = []
    for public in profiles:
        row = dict(public)
        if row.get("category") != "open_source" or not row.get("setup_complete"):
            row.setdefault("reachable", None)
            out.append(row)
            continue
        if not row.get("enabled"):
            row["reachable"] = None
            out.append(row)
            continue
        pid = str(row.get("id") or "")
        try:
            probe = reg.probe_profile(user_id, pid, timeout=timeout)
            row["reachable"] = bool(probe.get("reachable"))
            if not row["reachable"]:
                row["route_error"] = probe.get("error") or probe.get("message")
        except Exception as exc:  # noqa: BLE001
            logger.info("reachability annotate failed for %s: %s", pid, exc)
            row["reachable"] = False
            row["route_error"] = str(exc)
        out.append(row)
    return out


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

    ensured = ensure_local_route(user_id, profile_id, force_recover=False)
    if ensured.get("error") in {"needs_companion", "companion_unreachable"}:
        return {
            "ok": False,
            "error": ensured.get("error"),
            "message": ensured.get("message")
            or "Open Local Companion, then turn On again.",
        }
    if not ensured.get("reachable"):
        return {
            "ok": False,
            "error": ensured.get("error") or "unreachable",
            "message": ensured.get("message")
            or "Model route unreachable. Open Local Companion and retry.",
            "probe": ensured.get("probe"),
        }

    try:
        public = reg.enable_open_source(user_id, profile_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "profile": public,
        "reachable": True,
        "recovered": bool(ensured.get("recovered")),
        "message": "On — specialists will use this local model.",
    }


def disable_profile(user_id: str, profile_id: str) -> dict[str, Any]:
    reg = model_registry()
    try:
        public = reg.disable_open_source(user_id, profile_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "profile": public, "message": "Off."}
