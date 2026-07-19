"""Agent model choices for workflow runs (Claude vs local/OS Qwen)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Stable ids persisted on automation specs. Changeable in Edit automation.
AGENT_MODELS: Dict[str, Dict[str, str]] = {
    "claude": {
        "id": "claude",
        "label": "Claude",
        "destination": "anthropic",
        "description": "Anthropic API (or Bedrock via env)",
    },
    "qwen3-8b": {
        "id": "qwen3-8b",
        "label": "Qwen3 8B",
        "destination": "qwen",
        "description": "OpenAI-compatible local/OS endpoint (Ollama, vLLM, MLX, …)",
    },
}


def list_agent_models() -> List[Dict[str, str]]:
    return [dict(AGENT_MODELS[k]) for k in ("claude", "qwen3-8b")]


def normalize_agent_model(value: Any) -> Optional[str]:
    """Return a catalog id, or None when unset / unknown."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    aliases = {
        "claude": "claude",
        "anthropic": "claude",
        "qwen": "qwen3-8b",
        "qwen3": "qwen3-8b",
        "qwen3-8b": "qwen3-8b",
        "qwen 3 8b": "qwen3-8b",
        "qwen3:8b": "qwen3-8b",
        # Migrate existing automation specs to the replacement model.
        "qwen2.5": "qwen3-8b",
        "qwen2.5-7b": "qwen3-8b",
        "qwen 2.5 7b": "qwen3-8b",
        "qwen 7.5": "qwen3-8b",
        "qwen7.5": "qwen3-8b",
    }
    mid = aliases.get(raw) or (raw if raw in AGENT_MODELS else None)
    return mid


def model_destination(model_id: Optional[str]) -> str:
    mid = normalize_agent_model(model_id)
    if not mid:
        raise RuntimeError(
            "Choose an agent model (Claude or Qwen3 8B) before the first run."
        )
    return AGENT_MODELS[mid]["destination"]


def model_label(model_id: Optional[str]) -> str:
    mid = normalize_agent_model(model_id)
    if not mid:
        return "not set"
    return AGENT_MODELS[mid]["label"]
