"""Governed Claude requests and declarative workflow validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .ledger import Ledger
from .models import normalize_agent_model
from .schema import Sensitivity
from .synthesize import _call_anthropic_messages, _call_openai_compatible_messages


ALLOWED_RUNNERS = frozenset(
    {"morning_yf_scan", "generic_watchlist_scan", "sec_filings_check", "note_digest"}
)
ALLOWED_ACTIONS = frozenset(
    {"fetch_quote", "fetch_calendar", "fetch_headlines", "sec_filings", "recent_notes"}
)


class WorkflowValidationError(ValueError):
    """Claude returned a workflow outside the governed schema."""


@dataclass(frozen=True)
class ModelResult:
    text: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    audit_id: str


def estimate_tokens(text: str) -> int:
    """Provider-neutral approximation used for local budget enforcement."""
    return max(1, len(text) // 4)


def extract_json(text: str) -> Any:
    """Parse a JSON object/array, tolerating one markdown code fence."""
    raw = (text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.I | re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError(f"Claude returned invalid JSON: {exc.msg}") from exc


def _clean_symbols(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        symbol = re.sub(r"[^A-Z0-9.^=-]", "", str(item).upper().strip())[:16]
        if symbol and symbol not in out:
            out.append(symbol)
    return out[:50]


def validate_workflow_spec(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a Claude proposal and reject executable or unknown actions."""
    if not isinstance(raw, dict):
        raise WorkflowValidationError("workflow must be a JSON object")
    name = str(raw.get("name") or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,120}", name):
        raise WorkflowValidationError(f"invalid workflow name: {name!r}")
    runner = str(raw.get("runner") or "").strip()
    if runner not in ALLOWED_RUNNERS:
        raise WorkflowValidationError(f"runner {runner!r} is not allowlisted")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowValidationError("workflow must include at least one step")
    steps: List[Dict[str, Any]] = []
    for step in raw_steps[:12]:
        if not isinstance(step, dict) or len(step) != 1:
            raise WorkflowValidationError("each step must contain exactly one action")
        action, config = next(iter(step.items()))
        if action not in ALLOWED_ACTIONS:
            raise WorkflowValidationError(f"action {action!r} is not allowlisted")
        if isinstance(config, (str, int, float, bool)) or config is None:
            safe_config: Any = config
        elif isinstance(config, list):
            safe_config = [str(v)[:120] for v in config[:20]]
        elif isinstance(config, dict):
            safe_config = {
                str(k)[:40]: v
                for k, v in list(config.items())[:20]
                if isinstance(v, (str, int, float, bool)) or v is None
            }
        else:
            raise WorkflowValidationError(f"invalid configuration for {action}")
        steps.append({action: safe_config})
    budget = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    max_steps = max(1, min(6, int(budget.get("max_steps") or 6)))
    max_minutes = max(1, min(5, int(budget.get("max_minutes") or 5)))
    max_tokens = max(512, min(16000, int(budget.get("max_tokens") or 8000)))
    return {
        "name": name,
        "version": 1,
        "approved": False,
        "enabled": True,
        "runner": runner,
        "schedule": str(raw.get("schedule") or "0 7 * * 1-5")[:80],
        "watchlist": _clean_symbols(raw.get("watchlist")),
        "description": str(raw.get("description") or "")[:1000],
        "steps": steps,
        "budget": {
            "max_steps": max_steps,
            "max_minutes": max_minutes,
            "max_tokens": max_tokens,
        },
        # Unset until the human picks Claude or Qwen before the first run.
        "model": normalize_agent_model(raw.get("model")),
        "outputs": {"ledger_session": True, "chat_thread": True},
    }


class ClaudeGateway:
    """Audited model client (Claude or Qwen) with an injectable responder for tests."""

    def __init__(
        self,
        ledger: Ledger,
        responder: Optional[Callable[[List[Dict[str, str]], int, Optional[str]], str]] = None,
        *,
        model: Optional[str] = None,
    ) -> None:
        self.ledger = ledger
        self.responder = responder
        self.model = normalize_agent_model(model) or "claude"

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        kind: str,
        session_id: Optional[str] = None,
        max_tokens: int = 2048,
        system: Optional[str] = None,
    ) -> ModelResult:
        prompt = json.dumps(
            {"system": system, "messages": messages}, ensure_ascii=False, separators=(",", ":")
        )
        destination = "qwen" if self.model == "qwen3-8b" else "anthropic"
        try:
            if self.responder:
                output = self.responder(messages, max_tokens, system)
            elif destination == "qwen":
                output = _call_openai_compatible_messages(
                    messages, max_tokens=max_tokens, system=system
                )
            else:
                output = _call_anthropic_messages(
                    messages, max_tokens=max_tokens, system=system
                )
            status = "ok"
            detail: Dict[str, Any] = {
                "kind": kind,
                "model": self.model,
                "output_chars": len(output),
                "estimated_input_tokens": estimate_tokens(prompt),
                "estimated_output_tokens": estimate_tokens(output),
            }
        except Exception as exc:
            status = "error"
            detail = {"kind": kind, "model": self.model, "error": str(exc)}
            self.ledger.record_egress(
                destination=destination,
                prompt=prompt,
                max_sensitivity=Sensitivity.INTERNAL.value,
                status=status,
                session_id=session_id,
                detail=detail,
            )
            raise
        audit_id = self.ledger.record_egress(
            destination=destination,
            prompt=prompt,
            max_sensitivity=Sensitivity.INTERNAL.value,
            status=status,
            session_id=session_id,
            detail=detail,
        )
        return ModelResult(
            text=output,
            estimated_input_tokens=detail["estimated_input_tokens"],
            estimated_output_tokens=detail["estimated_output_tokens"],
            audit_id=audit_id,
        )

    def complete_json(self, messages: List[Dict[str, str]], **kwargs: Any) -> Any:
        return extract_json(self.complete(messages, **kwargs).text)
