"""Per-user LLM provider link (Claude, GPT, local Ollama, OpenRouter, custom).

Each Workflow account stores their own credentials. Specialists / @mentions
use the room owner's active provider — never a shared operator machine.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("messenger.model_link")

PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Claude (Anthropic)",
        "kind": "anthropic",
        "default_model": "claude-sonnet-4-20250514",
        "models": [
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-haiku-4-5-20251001",
        ],
        "needs_base_url": False,
        "default_base_url": "",
        "hint": "Paste your Anthropic API key (sk-ant-…). Billing is on your Anthropic account.",
    },
    "openai": {
        "label": "GPT (OpenAI)",
        "kind": "openai_compatible",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o4-mini"],
        "needs_base_url": False,
        "default_base_url": "https://api.openai.com/v1",
        "hint": "Paste your OpenAI API key (sk-…). Billing is on your OpenAI account.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "kind": "openai_compatible",
        "default_model": "anthropic/claude-sonnet-4",
        "models": [
            "anthropic/claude-sonnet-4",
            "openai/gpt-4o",
            "google/gemini-2.5-flash",
        ],
        "needs_base_url": False,
        "default_base_url": "https://openrouter.ai/api/v1",
        "hint": "One key for many models. Paste your OpenRouter key.",
    },
    "ollama": {
        "label": "Local Ollama (tunnel)",
        "kind": "openai_compatible",
        "default_model": "qwen3:8b",
        "models": ["qwen3:8b", "qwen2.5:7b"],
        "needs_base_url": True,
        "default_base_url": "",
        "hint": "Run ./scripts/secure_qwen_tunnel.sh on your computer, then paste the HTTPS /v1 URL + gateway token.",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "kind": "openai_compatible",
        "default_model": "gpt-4o",
        "models": [],
        "needs_base_url": True,
        "default_base_url": "",
        "hint": "Any OpenAI-compatible /v1 endpoint (Groq, Together, vLLM, Azure-compatible, …).",
    },
}


def providers_public() -> list[dict[str, Any]]:
    out = []
    for pid, meta in PROVIDERS.items():
        out.append(
            {
                "id": pid,
                "label": meta["label"],
                "default_model": meta["default_model"],
                "models": list(meta.get("models") or []),
                "needs_base_url": bool(meta.get("needs_base_url")),
                "default_base_url": meta.get("default_base_url") or "",
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


class ModelLinkRegistry:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _state_path()
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def register(
        self,
        user_id: str,
        *,
        provider: str = "ollama",
        api_key: str,
        model: str = "",
        base_url: str = "",
    ) -> dict[str, Any]:
        uid = str(user_id).strip()
        if not uid:
            raise ValueError("user_id required")
        provider_id = str(provider or "ollama").strip().lower()
        # Backward compat: old clients sent only base_url/key → treat as ollama
        if provider_id not in PROVIDERS:
            raise ValueError(
                f"Unknown provider '{provider_id}'. "
                f"Expected: {', '.join(PROVIDERS)}"
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

        entry = {
            "user_id": uid,
            "provider": provider_id,
            "kind": meta["kind"],
            "base_url": url,
            "api_key": key,
            "model": model_id,
            "linked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with self._lock:
            data = self._load()
            data[uid] = entry
            self._save(data)
        return self.public(entry)

    def get(self, user_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            entry = self._load().get(str(user_id))
        if not isinstance(entry, dict):
            return None
        # Migrate legacy ollama-only rows
        if not entry.get("provider"):
            entry = {
                **entry,
                "provider": "ollama",
                "kind": "openai_compatible",
            }
        return entry

    def unlink(self, user_id: str) -> bool:
        with self._lock:
            data = self._load()
            existed = str(user_id) in data
            data.pop(str(user_id), None)
            self._save(data)
            return existed

    @staticmethod
    def public(entry: dict[str, Any]) -> dict[str, Any]:
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
            "api_key_suffix": ("…" + key[-4:]) if len(key) >= 4 else "",
        }

    def endpoint_for_call(self, user_id: str) -> Optional[dict[str, str]]:
        """Shape expected by analyst_ledger.synthesize.use_llm_endpoint."""
        entry = self.get(user_id)
        if not entry:
            return None
        provider = str(entry.get("provider") or "ollama")
        meta = PROVIDERS.get(provider) or {}
        return {
            "provider": provider,
            "kind": str(entry.get("kind") or meta.get("kind") or "openai_compatible"),
            "base_url": str(entry.get("base_url") or meta.get("default_base_url") or ""),
            "api_key": str(entry["api_key"]),
            "model": str(
                entry.get("model") or meta.get("default_model") or "qwen3:8b"
            ),
        }

    def probe(self, user_id: str, timeout: float = 12.0) -> dict[str, Any]:
        entry = self.get(user_id)
        if not entry:
            return {
                "ok": True,
                "linked": False,
                "reachable": False,
                "message": "No model linked. Add Claude, GPT, or a local tunnel under Model.",
                "providers": providers_public(),
            }
        public = self.public(entry)
        provider = public["provider"]
        kind = public["kind"]
        try:
            if kind == "anthropic":
                models = self._probe_anthropic(entry["api_key"], timeout=timeout)
            else:
                models = self._probe_openai_compatible(
                    base_url=public["base_url"]
                    or PROVIDERS.get(provider, {}).get("default_base_url")
                    or "",
                    api_key=entry["api_key"],
                    timeout=timeout,
                )
            return {
                "ok": True,
                "linked": True,
                "reachable": True,
                "model_link": public,
                "models": models[:16],
                "message": f"{public['provider_label']} reachable.",
                "providers": providers_public(),
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("model probe failed for %s: %s", user_id, exc)
            hint = (
                "Is your tunnel running?"
                if provider == "ollama"
                else "Check the API key and model id."
            )
            return {
                "ok": True,
                "linked": True,
                "reachable": False,
                "model_link": public,
                "message": f"Linked, but unreachable right now. {hint}",
                "error": str(exc),
                "providers": providers_public(),
            }

    @staticmethod
    def _probe_openai_compatible(
        *, base_url: str, api_key: str, timeout: float
    ) -> list[str]:
        url = str(base_url or "").rstrip("/")
        if not url:
            raise RuntimeError("missing base_url")
        req = urllib.request.Request(
            f"{url}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "ngrok-skip-browser-warning": "1",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        return [m.get("id") for m in (body.get("data") or []) if isinstance(m, dict)]

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
            # Some keys / orgs can't list models — fall through to a tiny ping.
            pass
        # Minimal authenticated ping via Messages API
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
        return ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]


_REGISTRY = ModelLinkRegistry()


def registry() -> ModelLinkRegistry:
    return _REGISTRY
