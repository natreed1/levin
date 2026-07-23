"""Per-user model profiles (frontier + open-source) with durable pipeline routes.

Each Workflow account stores their own credentials. Specialists / @mentions
use the room owner's active profile (or a per-room ``model_profile_id``
override) — never a shared operator machine.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from messenger.secrets_crypto import (
    decrypt_secret,
    encrypt_secret,
    secret_suffix,
)

logger = logging.getLogger("messenger.model_link")

FRONTIER_PROVIDERS = ("anthropic", "openai", "openrouter")
OPEN_SOURCE_PROVIDERS = ("ollama", "custom")

PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Claude (Anthropic)",
        "category": "frontier",
        "kind": "anthropic",
        "default_model": "claude-sonnet-5",
        "models": [
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-haiku-4-5-20251001",
        ],
        "needs_base_url": False,
        "default_base_url": "",
        "is_local": False,
        "hint": "Paste your Anthropic API key (sk-ant-…). Billing is on your Anthropic account.",
    },
    "openai": {
        "label": "GPT (OpenAI)",
        "category": "frontier",
        "kind": "openai_compatible",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o4-mini"],
        "needs_base_url": False,
        "default_base_url": "https://api.openai.com/v1",
        "is_local": False,
        "hint": "Paste your OpenAI API key (sk-…). Billing is on your OpenAI account.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "category": "frontier",
        "kind": "openai_compatible",
        "default_model": "anthropic/claude-sonnet-4",
        "models": [
            "anthropic/claude-sonnet-4",
            "openai/gpt-4o",
            "google/gemini-2.5-flash",
        ],
        "needs_base_url": False,
        "default_base_url": "https://openrouter.ai/api/v1",
        "is_local": False,
        "hint": "One key for many models. Paste your OpenRouter key.",
    },
    "ollama": {
        "label": "Local open source",
        "category": "open_source",
        "kind": "openai_compatible",
        "default_model": "qwen3:8b",
        "models": ["qwen3:8b", "qwen2.5:7b"],
        "needs_base_url": False,
        "default_base_url": "",
        "is_local": True,
        "hint": "Find models on this computer via Local Companion, then Connect & save once.",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "category": "open_source",
        "kind": "openai_compatible",
        "default_model": "gpt-4o",
        "models": [],
        "needs_base_url": True,
        "default_base_url": "",
        "is_local": False,
        "hint": "Any OpenAI-compatible /v1 endpoint (Groq, Together, vLLM, …).",
    },
}

DEFAULT_PULL_MODEL = "qwen3:8b"


def providers_public(*, category: Optional[str] = None) -> list[dict[str, Any]]:
    out = []
    for pid, meta in PROVIDERS.items():
        if category and meta.get("category") != category:
            continue
        out.append(
            {
                "id": pid,
                "label": meta["label"],
                "category": meta.get("category") or "frontier",
                "default_model": meta["default_model"],
                "models": list(meta.get("models") or []),
                "needs_base_url": bool(meta.get("needs_base_url")),
                "default_base_url": meta.get("default_base_url") or "",
                "is_local": bool(meta.get("is_local")),
                "hint": meta.get("hint") or "",
            }
        )
    return out


def _state_path() -> Path:
    import os

    raw = os.environ.get("MESSENGER_DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "model_links.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_base_url(url: str, *, required: bool) -> str:
    url = str(url or "").strip().rstrip("/")
    if not url:
        if required:
            raise ValueError("base_url required for this provider")
        return ""
    if not (
        url.startswith("https://")
        or url.startswith("http://127.0.0.1")
        or url.startswith("http://localhost")
    ):
        raise ValueError("base_url must be https://… (or http://127.0.0.1 for local)")
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url


def _new_id() -> str:
    return "prof_" + uuid.uuid4().hex[:12]


def profile_is_local(profile: dict[str, Any]) -> bool:
    provider = str(profile.get("provider") or "")
    meta = PROVIDERS.get(provider) or {}
    if meta.get("is_local"):
        return True
    if profile.get("category") == "open_source" and provider == "ollama":
        return True
    runtime = str(profile.get("runtime") or "")
    if runtime in {"ollama", "lmstudio"}:
        return True
    route = profile.get("pipeline_route") or {}
    base = str(route.get("base_url") or profile.get("base_url") or "")
    if "127.0.0.1" in base or "localhost" in base:
        return True
    return False


def destination_for_profile(profile: Optional[dict[str, Any]]) -> str:
    """Map a profile to synthesize destination ids for sensitivity gating."""
    if not profile:
        return "anthropic"
    if profile_is_local(profile):
        return "qwen"
    provider = str(profile.get("provider") or "anthropic")
    if provider == "anthropic":
        return "anthropic"
    if provider in {"openai", "openrouter", "custom"}:
        return "openai"
    return provider


class ModelLinkRegistry:
    """Multi-profile store with legacy single-link compatibility."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _state_path()
        self._lock = threading.Lock()

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_raw(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _empty_user(self, user_id: str) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "active_profile_id": None,
            "profiles": [],
        }

    def _migrate_entry(self, user_id: str, entry: Any) -> dict[str, Any]:
        """Normalize legacy flat link or already-migrated user blob."""
        if not isinstance(entry, dict):
            return self._empty_user(user_id)
        if "profiles" in entry and isinstance(entry.get("profiles"), list):
            entry.setdefault("user_id", user_id)
            entry.setdefault("active_profile_id", None)
            # Decrypt-migrate plaintext keys on profiles
            for p in entry["profiles"]:
                if not isinstance(p, dict):
                    continue
                if p.get("api_key") and not p.get("api_key_enc"):
                    p["api_key_enc"] = encrypt_secret(str(p.pop("api_key")))
                route = p.get("pipeline_route")
                if isinstance(route, dict) and route.get("api_key") and not route.get(
                    "api_key_enc"
                ):
                    route["api_key_enc"] = encrypt_secret(str(route.pop("api_key")))
            return entry

        # Legacy single-link row
        provider = str(entry.get("provider") or "ollama").strip().lower()
        if provider not in PROVIDERS:
            provider = "ollama"
        meta = PROVIDERS[provider]
        raw_key = str(entry.get("api_key") or "")
        profile = {
            "id": _new_id(),
            "category": meta.get("category") or "frontier",
            "provider": provider,
            "kind": entry.get("kind") or meta["kind"],
            "label": meta.get("label") or provider,
            "model": entry.get("model") or meta["default_model"],
            "base_url": entry.get("base_url") or meta.get("default_base_url") or "",
            "api_key_enc": encrypt_secret(raw_key) if raw_key else "",
            "runtime": "ollama" if provider == "ollama" else "",
            "source": {"method": "legacy"},
            "pipeline_route": None,
            "setup_complete": bool(raw_key),
            "enabled": True,
            "auto_connect": provider == "ollama",
            "created_at": entry.get("linked_at") or _now(),
            "last_ok_at": entry.get("linked_at"),
        }
        if provider == "ollama" and profile["base_url"] and raw_key:
            profile["pipeline_route"] = {
                "base_url": profile["base_url"],
                "api_key_enc": encrypt_secret(raw_key),
                "gateway_mode": "legacy",
                "established_at": profile["created_at"],
                "last_ok_at": profile["created_at"],
            }
            profile["setup_complete"] = True
        return {
            "user_id": user_id,
            "active_profile_id": profile["id"],
            "profiles": [profile],
        }

    def _get_user_unlocked(self, user_id: str) -> dict[str, Any]:
        uid = str(user_id)
        data = self._load_raw()
        entry = data.get(uid)
        migrated = self._migrate_entry(uid, entry)
        # Persist migration if shape changed
        if entry != migrated:
            data[uid] = migrated
            self._save_raw(data)
        return migrated

    def _put_user_unlocked(self, user_id: str, blob: dict[str, Any]) -> None:
        data = self._load_raw()
        data[str(user_id)] = blob
        self._save_raw(data)

    def list_profiles(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            blob = self._get_user_unlocked(user_id)
        active = blob.get("active_profile_id")
        return {
            "ok": True,
            "active_profile_id": active,
            "profiles": [self.public_profile(p) for p in blob.get("profiles") or []],
            "providers": {
                "frontier": providers_public(category="frontier"),
                "open_source": providers_public(category="open_source"),
            },
        }

    def get_profile(self, user_id: str, profile_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            blob = self._get_user_unlocked(user_id)
        for p in blob.get("profiles") or []:
            if isinstance(p, dict) and p.get("id") == profile_id:
                return p
        return None

    def active_profile(self, user_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            blob = self._get_user_unlocked(user_id)
        active_id = blob.get("active_profile_id")
        for p in blob.get("profiles") or []:
            if isinstance(p, dict) and p.get("id") == active_id:
                if p.get("category") == "open_source" and not p.get("enabled", True):
                    return None
                return p
        # Fallback: first enabled / frontier
        for p in blob.get("profiles") or []:
            if not isinstance(p, dict):
                continue
            if p.get("category") == "open_source" and not p.get("enabled"):
                continue
            if p.get("setup_complete", True):
                return p
        return None

    def add_frontier(
        self,
        user_id: str,
        *,
        provider: str,
        api_key: str,
        model: str = "",
        base_url: str = "",
        label: str = "",
        activate: bool = True,
    ) -> dict[str, Any]:
        uid = str(user_id).strip()
        if not uid:
            raise ValueError("user_id required")
        provider_id = str(provider or "").strip().lower()
        if provider_id not in FRONTIER_PROVIDERS:
            raise ValueError(
                f"Frontier provider must be one of: {', '.join(FRONTIER_PROVIDERS)}"
            )
        meta = PROVIDERS[provider_id]
        key = str(api_key or "").strip()
        if len(key) < 8:
            raise ValueError("api_key is too short")
        model_id = (str(model or "").strip() or str(meta["default_model"])).strip()
        needs_url = bool(meta.get("needs_base_url"))
        url = _normalize_base_url(
            base_url or (meta.get("default_base_url") or ""),
            required=needs_url,
        )
        if not needs_url and not url:
            url = str(meta.get("default_base_url") or "")

        profile = {
            "id": _new_id(),
            "category": "frontier",
            "provider": provider_id,
            "kind": meta["kind"],
            "label": (label or meta.get("label") or provider_id).strip(),
            "model": model_id,
            "base_url": url,
            "api_key_enc": encrypt_secret(key),
            "runtime": "",
            "source": {"method": "frontier_form"},
            "pipeline_route": None,
            "setup_complete": True,
            "enabled": True,
            "auto_connect": False,
            "created_at": _now(),
            "last_ok_at": None,
        }
        with self._lock:
            blob = self._get_user_unlocked(uid)
            blob["profiles"].append(profile)
            if activate or not blob.get("active_profile_id"):
                blob["active_profile_id"] = profile["id"]
                # Disable open-source enabled flags when frontier takes over
                for p in blob["profiles"]:
                    if p.get("category") == "open_source" and p.get("id") != profile["id"]:
                        p["enabled"] = False
            self._put_user_unlocked(uid, blob)
        return self.public_profile(profile)

    def add_open_source_draft(
        self,
        user_id: str,
        *,
        candidate_id: str,
        runtime: str = "ollama",
        model: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        uid = str(user_id).strip()
        if not uid:
            raise ValueError("user_id required")
        model_id = (str(model or "").strip() or DEFAULT_PULL_MODEL).strip()
        runtime_id = (str(runtime or "ollama").strip() or "ollama").lower()
        provider = "ollama" if runtime_id in {"ollama", "lmstudio"} else "custom"
        meta = PROVIDERS[provider]
        profile = {
            "id": _new_id(),
            "category": "open_source",
            "provider": provider,
            "kind": meta["kind"],
            "label": (label or model_id).strip(),
            "model": model_id,
            "base_url": "",
            "api_key_enc": "",
            "runtime": runtime_id,
            "source": {
                "method": "search",
                "candidate_id": str(candidate_id or f"{runtime_id}:{model_id}"),
            },
            "pipeline_route": None,
            "setup_complete": False,
            "enabled": False,
            "auto_connect": True,
            "created_at": _now(),
            "last_ok_at": None,
        }
        with self._lock:
            blob = self._get_user_unlocked(uid)
            blob["profiles"].append(profile)
            self._put_user_unlocked(uid, blob)
        return self.public_profile(profile)

    def update_profile(
        self,
        user_id: str,
        profile_id: str,
        *,
        label: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            blob = self._get_user_unlocked(user_id)
            target = None
            for p in blob.get("profiles") or []:
                if p.get("id") == profile_id:
                    target = p
                    break
            if not target:
                raise ValueError("profile not found")
            if label is not None:
                target["label"] = str(label).strip() or target.get("label")
            if model is not None and str(model).strip():
                target["model"] = str(model).strip()
            if api_key is not None and str(api_key).strip():
                target["api_key_enc"] = encrypt_secret(str(api_key).strip())
            if base_url is not None:
                meta = PROVIDERS.get(str(target.get("provider") or ""), {})
                target["base_url"] = _normalize_base_url(
                    base_url,
                    required=bool(meta.get("needs_base_url")),
                )
            self._put_user_unlocked(user_id, blob)
            return self.public_profile(target)

    def delete_profile(self, user_id: str, profile_id: str) -> bool:
        with self._lock:
            blob = self._get_user_unlocked(user_id)
            before = len(blob.get("profiles") or [])
            blob["profiles"] = [
                p for p in (blob.get("profiles") or []) if p.get("id") != profile_id
            ]
            if blob.get("active_profile_id") == profile_id:
                blob["active_profile_id"] = (
                    blob["profiles"][0]["id"] if blob["profiles"] else None
                )
            self._put_user_unlocked(user_id, blob)
            return len(blob["profiles"]) < before

    def set_pipeline_route(
        self,
        user_id: str,
        profile_id: str,
        *,
        base_url: str,
        api_key: str,
        gateway_mode: str = "tunnel",
        setup_complete: bool = True,
    ) -> dict[str, Any]:
        url = _normalize_base_url(base_url, required=True)
        key = str(api_key or "").strip()
        if len(key) < 8:
            raise ValueError("pipeline token is too short")
        with self._lock:
            blob = self._get_user_unlocked(user_id)
            target = None
            for p in blob.get("profiles") or []:
                if p.get("id") == profile_id:
                    target = p
                    break
            if not target:
                raise ValueError("profile not found")
            now = _now()
            target["pipeline_route"] = {
                "base_url": url,
                "api_key_enc": encrypt_secret(key),
                "gateway_mode": gateway_mode,
                "established_at": (
                    (target.get("pipeline_route") or {}).get("established_at") or now
                ),
                "last_ok_at": now,
            }
            target["base_url"] = url
            target["api_key_enc"] = encrypt_secret(key)
            target["setup_complete"] = bool(setup_complete)
            target["last_ok_at"] = now
            self._put_user_unlocked(user_id, blob)
            return self.public_profile(target)

    def activate(self, user_id: str, profile_id: str) -> dict[str, Any]:
        with self._lock:
            blob = self._get_user_unlocked(user_id)
            target = None
            for p in blob.get("profiles") or []:
                if p.get("id") == profile_id:
                    target = p
                    break
            if not target:
                raise ValueError("profile not found")
            if target.get("category") == "open_source" and not target.get(
                "setup_complete"
            ):
                raise ValueError("Finish setup before activating this model")
            blob["active_profile_id"] = profile_id
            for p in blob["profiles"]:
                if p.get("category") == "open_source":
                    p["enabled"] = p.get("id") == profile_id
                elif p.get("id") == profile_id:
                    p["enabled"] = True
            self._put_user_unlocked(user_id, blob)
            return self.public_profile(target)

    def enable_open_source(self, user_id: str, profile_id: str) -> dict[str, Any]:
        """One-click On for a setup-complete open-source profile."""
        with self._lock:
            blob = self._get_user_unlocked(user_id)
            target = None
            for p in blob.get("profiles") or []:
                if p.get("id") == profile_id:
                    target = p
                    break
            if not target:
                raise ValueError("profile not found")
            if target.get("category") != "open_source":
                raise ValueError("enable is for open-source profiles")
            if not target.get("setup_complete") or not target.get("pipeline_route"):
                raise ValueError("Finish Connect & save before turning this on")
            for p in blob["profiles"]:
                if p.get("category") == "open_source":
                    p["enabled"] = p.get("id") == profile_id
            target["enabled"] = True
            blob["active_profile_id"] = profile_id
            target["last_ok_at"] = _now()
            route = target.get("pipeline_route") or {}
            route["last_ok_at"] = target["last_ok_at"]
            target["pipeline_route"] = route
            self._put_user_unlocked(user_id, blob)
            return self.public_profile(target)

    def disable_open_source(self, user_id: str, profile_id: str) -> dict[str, Any]:
        with self._lock:
            blob = self._get_user_unlocked(user_id)
            target = None
            for p in blob.get("profiles") or []:
                if p.get("id") == profile_id:
                    target = p
                    break
            if not target:
                raise ValueError("profile not found")
            target["enabled"] = False
            if blob.get("active_profile_id") == profile_id:
                # Fall back to a frontier profile if any
                fallback = None
                for p in blob["profiles"]:
                    if p.get("category") == "frontier" and p.get("setup_complete"):
                        fallback = p["id"]
                        break
                blob["active_profile_id"] = fallback
            self._put_user_unlocked(user_id, blob)
            return self.public_profile(target)

    def _api_key_for(self, profile: dict[str, Any]) -> str:
        route = profile.get("pipeline_route")
        if isinstance(route, dict) and route.get("api_key_enc"):
            return decrypt_secret(str(route["api_key_enc"]))
        if profile.get("api_key_enc"):
            return decrypt_secret(str(profile["api_key_enc"]))
        if profile.get("api_key"):
            return str(profile["api_key"])
        return ""

    def _base_url_for(self, profile: dict[str, Any]) -> str:
        route = profile.get("pipeline_route")
        if isinstance(route, dict) and route.get("base_url"):
            return str(route["base_url"])
        meta = PROVIDERS.get(str(profile.get("provider") or ""), {})
        return str(profile.get("base_url") or meta.get("default_base_url") or "")

    def public_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        key = self._api_key_for(profile)
        provider = str(profile.get("provider") or "ollama")
        meta = PROVIDERS.get(provider) or {}
        route = profile.get("pipeline_route") if isinstance(profile.get("pipeline_route"), dict) else None
        return {
            "id": profile.get("id"),
            "category": profile.get("category") or meta.get("category") or "frontier",
            "provider": provider,
            "provider_label": meta.get("label") or provider,
            "kind": profile.get("kind") or meta.get("kind") or "openai_compatible",
            "label": profile.get("label") or meta.get("label") or provider,
            "model": profile.get("model") or meta.get("default_model") or "",
            "base_url": self._base_url_for(profile),
            "runtime": profile.get("runtime") or "",
            "source": profile.get("source") or {},
            "setup_complete": bool(profile.get("setup_complete")),
            "enabled": bool(profile.get("enabled")),
            "auto_connect": bool(profile.get("auto_connect")),
            "is_local": profile_is_local(profile),
            "destination": destination_for_profile(profile),
            "created_at": profile.get("created_at"),
            "last_ok_at": profile.get("last_ok_at"),
            "api_key_set": bool(key),
            "api_key_suffix": secret_suffix(key),
            "pipeline_route": (
                {
                    "base_url": route.get("base_url") or "",
                    "gateway_mode": route.get("gateway_mode") or "",
                    "established_at": route.get("established_at"),
                    "last_ok_at": route.get("last_ok_at"),
                    "api_key_set": bool(route.get("api_key_enc") or route.get("api_key")),
                }
                if route
                else None
            ),
        }

    # --- Legacy single-link API (aliases for old /api/model/*) ----------------

    def register(
        self,
        user_id: str,
        *,
        provider: str = "ollama",
        api_key: str,
        model: str = "",
        base_url: str = "",
    ) -> dict[str, Any]:
        provider_id = str(provider or "ollama").strip().lower()
        if provider_id in FRONTIER_PROVIDERS:
            return self.add_frontier(
                user_id,
                provider=provider_id,
                api_key=api_key,
                model=model,
                base_url=base_url,
                activate=True,
            )
        # Treat as open-source with immediate route (legacy paste flow)
        public = self.add_open_source_draft(
            user_id,
            candidate_id=f"legacy:{model or 'custom'}",
            runtime="ollama" if provider_id == "ollama" else "custom",
            model=model or PROVIDERS.get(provider_id, {}).get("default_model", ""),
            label=PROVIDERS.get(provider_id, {}).get("label") or provider_id,
        )
        return self.set_pipeline_route(
            user_id,
            public["id"],
            base_url=base_url
            or PROVIDERS.get(provider_id, {}).get("default_base_url")
            or "",
            api_key=api_key,
            gateway_mode="legacy",
            setup_complete=True,
        )

    def get(self, user_id: str) -> Optional[dict[str, Any]]:
        """Legacy: return active profile in flat-ish shape (with decrypted key)."""
        profile = self.active_profile(user_id)
        if not profile:
            return None
        return {
            "user_id": user_id,
            "provider": profile.get("provider"),
            "kind": profile.get("kind"),
            "base_url": self._base_url_for(profile),
            "api_key": self._api_key_for(profile),
            "model": profile.get("model"),
            "linked_at": profile.get("created_at"),
            "profile_id": profile.get("id"),
            "category": profile.get("category"),
            "is_local": profile_is_local(profile),
        }

    def unlink(self, user_id: str) -> bool:
        """Legacy: clear all profiles for user."""
        with self._lock:
            data = self._load_raw()
            existed = str(user_id) in data
            data.pop(str(user_id), None)
            self._save_raw(data)
            return existed

    @staticmethod
    def public(entry: dict[str, Any]) -> dict[str, Any]:
        """Legacy public shape for /api/model/status."""
        key = str(entry.get("api_key") or "")
        provider = str(entry.get("provider") or "ollama")
        meta = PROVIDERS.get(provider) or {}
        return {
            "user_id": entry.get("user_id"),
            "provider": provider,
            "provider_label": meta.get("label") or provider,
            "kind": entry.get("kind") or meta.get("kind") or "openai_compatible",
            "base_url": entry.get("base_url") or "",
            "model": entry.get("model") or meta.get("default_model") or "",
            "linked_at": entry.get("linked_at"),
            "api_key_set": bool(key),
            "api_key_suffix": secret_suffix(key),
            "profile_id": entry.get("profile_id"),
            "is_local": bool(entry.get("is_local")),
        }

    def endpoint_for_call(
        self, user_id: str, profile_id: Optional[str] = None
    ) -> Optional[dict[str, str]]:
        """Shape expected by analyst_ledger.synthesize.use_llm_endpoint.

        When ``profile_id`` is set (e.g. per-room model toggle), use that profile
        instead of the account active model.
        """
        profile = (
            self.get_profile(user_id, profile_id)
            if profile_id
            else self.active_profile(user_id)
        )
        if not profile:
            return None
        provider = str(profile.get("provider") or "ollama")
        meta = PROVIDERS.get(provider) or {}
        key = self._api_key_for(profile)
        if not key:
            return None
        return {
            "provider": provider,
            "kind": str(profile.get("kind") or meta.get("kind") or "openai_compatible"),
            "base_url": self._base_url_for(profile),
            "api_key": key,
            "model": str(
                profile.get("model") or meta.get("default_model") or "qwen3:8b"
            ),
            "is_local": "1" if profile_is_local(profile) else "0",
            "destination": destination_for_profile(profile),
        }

    def probe_profile(
        self, user_id: str, profile_id: Optional[str] = None, timeout: float = 12.0
    ) -> dict[str, Any]:
        profile = (
            self.get_profile(user_id, profile_id)
            if profile_id
            else self.active_profile(user_id)
        )
        if not profile:
            return {
                "ok": True,
                "linked": False,
                "reachable": False,
                "message": "No model linked. Add Claude, GPT, or a local model under Settings.",
                "providers": providers_public(),
                "profiles": self.list_profiles(user_id)["profiles"],
            }
        public = self.public_profile(profile)
        key = self._api_key_for(profile)
        kind = public["kind"]
        provider = public["provider"]
        try:
            if kind == "anthropic":
                models = self._probe_anthropic(key, timeout=timeout)
            else:
                models = self._probe_openai_compatible(
                    base_url=public["base_url"]
                    or PROVIDERS.get(provider, {}).get("default_base_url")
                    or "",
                    api_key=key,
                    timeout=timeout,
                )
            return {
                "ok": True,
                "linked": True,
                "reachable": True,
                "model_link": self.public(
                    {
                        "user_id": user_id,
                        "provider": provider,
                        "kind": kind,
                        "base_url": public["base_url"],
                        "api_key": key,
                        "model": public["model"],
                        "linked_at": public.get("created_at"),
                        "profile_id": public["id"],
                        "is_local": public["is_local"],
                    }
                ),
                "profile": public,
                "models": models[:16],
                "message": f"{public['provider_label']} reachable.",
                "providers": providers_public(),
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("model probe failed for %s: %s", user_id, exc)
            hint = (
                "Is your Local Companion running?"
                if profile_is_local(profile)
                else "Check the API key and model id."
            )
            return {
                "ok": True,
                "linked": True,
                "reachable": False,
                "model_link": self.public(
                    {
                        "user_id": user_id,
                        "provider": provider,
                        "kind": kind,
                        "base_url": public["base_url"],
                        "api_key": key,
                        "model": public["model"],
                        "linked_at": public.get("created_at"),
                        "profile_id": public["id"],
                        "is_local": public["is_local"],
                    }
                ),
                "profile": public,
                "message": f"Linked, but unreachable right now. {hint}",
                "error": str(exc),
                "providers": providers_public(),
            }

    def probe(self, user_id: str, timeout: float = 12.0) -> dict[str, Any]:
        return self.probe_profile(user_id, timeout=timeout)

    @staticmethod
    def _probe_openai_compatible(
        *, base_url: str, api_key: str, timeout: float
    ) -> list[str]:
        url = str(base_url or "").rstrip("/")
        if not url:
            raise RuntimeError("missing base_url")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "ngrok-skip-browser-warning": "1",
            "Accept": "application/json",
        }
        models_url = f"{url}/models"
        try:
            req = urllib.request.Request(models_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
            return [m.get("id") for m in (body.get("data") or []) if isinstance(m, dict)]
        except Exception as primary:
            # Some trycloudflare quick tunnels publish AAAA-only; macOS often has
            # no IPv6 route. Probe via Cloudflare anycast IPv4 + SNI/Host.
            from urllib.parse import urlparse
            import http.client
            import ssl

            parsed = urlparse(models_url)
            host = parsed.hostname or ""
            if "trycloudflare.com" not in host:
                raise primary
            path = parsed.path or "/models"
            hdrs = dict(headers)
            hdrs["Host"] = host
            ctx = ssl.create_default_context()
            last_exc: Exception = primary
            for ip in ("104.16.230.132", "104.16.231.132", "104.19.140.88"):
                try:
                    sock = socket.create_connection((ip, 443), timeout=timeout)
                    ssock = ctx.wrap_socket(sock, server_hostname=host)
                    conn = http.client.HTTPSConnection(ip, 443, timeout=timeout)
                    conn.sock = ssock
                    conn.request("GET", path, headers=hdrs)
                    resp = conn.getresponse()
                    raw = resp.read().decode("utf-8") or "{}"
                    status = resp.status
                    conn.close()
                    if status >= 400:
                        last_exc = RuntimeError(f"HTTP {status}")
                        continue
                    body = json.loads(raw)
                    return [
                        m.get("id")
                        for m in (body.get("data") or [])
                        if isinstance(m, dict)
                    ]
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    continue
            raise last_exc

    @staticmethod
    def _probe_anthropic(api_key: str, *, timeout: float) -> list[str]:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
            ids = [m.get("id") for m in (body.get("data") or []) if isinstance(m, dict)]
            if ids:
                return ids
        except urllib.error.HTTPError:
            pass
        payload = json.dumps(
            {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return ["claude-sonnet-5", "claude-haiku-4-5-20251001"]


_REGISTRY = ModelLinkRegistry()


def registry() -> ModelLinkRegistry:
    return _REGISTRY
