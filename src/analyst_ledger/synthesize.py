"""Redacted synthesis jobs with egress audit logging."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional

from .ledger import Ledger
from .redact import build_synthesis_prompt
from .schema import Event, Sensitivity, Surface, sensitivity_allows_egress

# Destinations whose model runs on this machine; everything else (including
# unknown strings, which run_synthesis routes to Anthropic) counts as external.
LOCAL_DESTINATIONS = {"qwen", "local_stub", "ollama", "lmstudio"}

# Per-request / per-job override so each Workflow user can point at their own
# Claude / GPT / Ollama / OpenRouter endpoint instead of a shared operator machine.
_QWEN_ENDPOINT: ContextVar[Optional[Dict[str, str]]] = ContextVar(
    "qwen_endpoint", default=None
)


def assert_destination_allowed(destination: str, max_sensitivity: Sensitivity) -> None:
    """Hard gate at the model-call boundary: external models (Anthropic/Bedrock)
    accept an egress ceiling of `internal` at most; local destinations may go up
    to `confidential`; `restricted` never goes to any model."""
    dest = (destination or "").strip().lower()
    is_local = dest in LOCAL_DESTINATIONS
    # Endpoint overrides may mark is_local explicitly via ContextVar metadata.
    override = _QWEN_ENDPOINT.get() or {}
    if str(override.get("is_local") or "") in {"1", "true", "yes"}:
        is_local = True
    if str(override.get("destination") or "").strip().lower() in LOCAL_DESTINATIONS:
        is_local = True
    ceiling = Sensitivity.CONFIDENTIAL if is_local else Sensitivity.INTERNAL
    if not sensitivity_allows_egress(max_sensitivity, ceiling):
        raise RuntimeError(
            f"Sensitivity ceiling '{max_sensitivity.value}' is not allowed for "
            f"destination '{destination}'. External models take 'internal' or below; "
            f"'confidential' must stay on a local destination ({', '.join(sorted(LOCAL_DESTINATIONS))}); "
            f"'restricted' content never goes to any model."
        )


@contextmanager
def use_llm_endpoint(endpoint: Optional[Dict[str, str]]) -> Iterator[None]:
    """Temporarily route chat calls to a user's linked provider."""
    if not endpoint:
        yield
        return
    token = _QWEN_ENDPOINT.set(endpoint)
    try:
        yield
    finally:
        _QWEN_ENDPOINT.reset(token)


# Backward-compatible alias used by specialist / messenger code.
use_qwen_endpoint = use_llm_endpoint


def call_chat_messages(
    messages: List[Dict[str, str]],
    *,
    max_tokens: int = 2048,
    system: Optional[str] = None,
    temperature: float = 0.0,
) -> str:
    """Dispatch to Anthropic or OpenAI-compatible based on active endpoint override."""
    override = _QWEN_ENDPOINT.get() or {}
    kind = (override.get("kind") or "").strip().lower()
    provider = (override.get("provider") or "").strip().lower()
    if kind == "anthropic" or provider == "anthropic":
        return _call_anthropic_messages(
            messages, max_tokens=max_tokens, system=system
        )
    return _call_openai_compatible_messages(
        messages,
        max_tokens=max_tokens,
        system=system,
        temperature=temperature,
    )

def run_synthesis(
    ledger: Ledger,
    session_id: str,
    instruction: str,
    max_sensitivity: Sensitivity = Sensitivity.INTERNAL,
    dry_run: bool = False,
    destination: str = "anthropic",
) -> Dict[str, Any]:
    assert_destination_allowed(destination, max_sensitivity)
    context = ledger.session_context_for_synthesis(session_id, max_sensitivity=max_sensitivity)
    prompt = build_synthesis_prompt(context, instruction)

    ledger.append_event(
        Event(
            type="synthesis_request",
            surface=Surface.SYNTHESIS.value,
            session_id=session_id,
            sensitivity=max_sensitivity.value,
            payload={
                "instruction": instruction,
                "max_sensitivity": max_sensitivity.value,
                "destination": destination,
                "dry_run": dry_run,
                "event_count": len(context.get("events") or []),
            },
        )
    )

    if dry_run:
        audit_id = ledger.record_egress(
            destination=destination,
            prompt=prompt,
            max_sensitivity=max_sensitivity.value,
            status="dry_run",
            session_id=session_id,
            detail={"reason": "dry_run"},
        )
        return {
            "status": "dry_run",
            "session_id": session_id,
            "audit_id": audit_id,
            "prompt_chars": len(prompt),
            "prompt_preview": prompt[:1200],
            "event_count": len(context.get("events") or []),
        }

    try:
        if destination == "local_stub":
            output = _stub_completion(context, instruction)
            dest_label = "local_stub"
        elif destination == "bedrock":
            output = _call_bedrock(prompt)
            dest_label = "bedrock"
        elif destination == "qwen":
            output = _call_openai_compatible_messages(
                [{"role": "user", "content": prompt}]
            )
            dest_label = "qwen"
        else:
            output = _call_anthropic(prompt)
            dest_label = "anthropic"
        status = "ok"
        error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001 — surface to audit + caller
        output = ""
        status = "error"
        error = str(exc)
        dest_label = destination

    audit_id = ledger.record_egress(
        destination=dest_label,
        prompt=prompt,
        max_sensitivity=max_sensitivity.value,
        status=status,
        session_id=session_id,
        detail={"error": error} if error else {"output_chars": len(output)},
    )

    result_event = ledger.append_event(
        Event(
            type="synthesis_result",
            surface=Surface.SYNTHESIS.value,
            session_id=session_id,
            sensitivity=max_sensitivity.value,
            payload={
                "audit_id": audit_id,
                "status": status,
                "output": output if status == "ok" else None,
                "error": error,
                "destination": dest_label,
            },
        )
    )

    # Persist successful draft under artifacts for feedback / SFT
    if status == "ok" and output:
        from .paths import artifacts_dir

        out_dir = artifacts_dir() / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"synthesis_{result_event.event_id}.md"
        out_path.write_text(output, encoding="utf-8")
        ledger.attach_artifact(out_path, session_id=session_id, sensitivity=max_sensitivity.value)

    return {
        "status": status,
        "session_id": session_id,
        "audit_id": audit_id,
        "event_id": result_event.event_id,
        "output": output if status == "ok" else None,
        "error": error,
        "destination": dest_label,
    }


def _stub_completion(context: Dict[str, Any], instruction: str) -> str:
    session = context.get("session") or {}
    notes = [
        e["payload"].get("text", "")
        for e in (context.get("events") or [])
        if e.get("type") == "note"
    ]
    bullets = "\n".join(f"- {n}" for n in notes if n) or "- (no notes captured)"
    return (
        f"# Research memo (local stub)\n\n"
        f"**Session:** {session.get('title', '')}\n\n"
        f"**Instruction:** {instruction}\n\n"
        f"## Summary\nStub draft generated without calling an external model.\n\n"
        f"## What was examined\n{bullets}\n\n"
        f"## Open questions\n- (add after review)\n\n"
        f"## Suggested next checks\n- Re-read notes and attach chart export if missing\n"
    )


def _call_anthropic_messages(
    messages: List[Dict[str, str]],
    *,
    max_tokens: int = 2048,
    system: Optional[str] = None,
) -> str:
    """Call Claude with an explicit multi-turn message list."""
    override = _QWEN_ENDPOINT.get() or {}
    api_key = (
        (override.get("api_key") or "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "No Anthropic API key. Add Claude under the Model tab, "
            "or set ANTHROPIC_API_KEY."
        )
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. pip install anthropic "
            "or use an OpenAI-compatible provider under Model."
        ) from exc

    # Per-user Model-tab override, else ANALYST_CLAUDE_MODEL.
    # (claude-sonnet-4-20250514 is retired.)
    model = (
        (override.get("model") or "").strip()
        or os.environ.get("ANALYST_CLAUDE_MODEL", "claude-sonnet-5").strip()
    )
    client = anthropic.Anthropic(api_key=api_key)
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max(1, int(max_tokens)),
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    message = client.messages.create(
        **kwargs,
    )
    parts = []
    for block in message.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _call_anthropic(prompt: str, max_tokens: int = 2048) -> str:
    return _call_anthropic_messages(
        [{"role": "user", "content": prompt}], max_tokens=max_tokens
    )


def _call_openai_compatible_messages(
    messages: List[Dict[str, str]],
    *,
    max_tokens: int = 2048,
    system: Optional[str] = None,
    temperature: float = 0.0,
) -> str:
    """
    Call an OpenAI-compatible chat endpoint (Ollama, vLLM, MLX server, etc.).

    Resolution order:
      1. use_qwen_endpoint(...) context (per-user Model link)
      2. ANALYST_QWEN_* / OPENAI_* env (local self-host / operator override)
      3. default http://127.0.0.1:11434/v1 + qwen3:8b
    """
    override = _QWEN_ENDPOINT.get() or {}
    base_url = (
        (override.get("base_url") or "").strip()
        or os.environ.get("ANALYST_QWEN_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "http://127.0.0.1:11434/v1"
    ).rstrip("/")
    model = (
        (override.get("model") or "").strip()
        or os.environ.get("ANALYST_QWEN_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "qwen3:8b"
    ).strip()
    if model.lower() in {"qwen2.5:7b", "qwen2.5-7b"}:
        model = "qwen3:8b"
    api_key = (
        (override.get("api_key") or "").strip()
        or os.environ.get("ANALYST_QWEN_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "local"
    ).strip()
    chat_messages: List[Dict[str, str]] = []
    if system:
        chat_messages.append({"role": "system", "content": system})
    chat_messages.extend(messages)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": chat_messages,
        "max_tokens": max(1, int(max_tokens)),
        "temperature": float(temperature),
    }
    # Qwen3 defaults to a reasoning mode in Ollama's OpenAI-compatible API.
    # Short chat calls can otherwise spend the whole token budget reasoning and
    # return an empty ``content`` field.
    if model.lower().startswith("qwen3"):
        payload["reasoning_effort"] = "none"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # ngrok free tier serves an HTML interstitial to browsers; API clients need this.
    if "ngrok" in base_url.lower():
        headers["ngrok-skip-browser-warning"] = "1"
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Local model HTTP {exc.code}: {err}") from exc
    except urllib.error.URLError as exc:
        hint = (
            "Click Start local model (or Settings → Open source) so Companion "
            "can open a tunnel, then try again."
        )
        raise RuntimeError(
            f"Local model unreachable at {base_url}. {hint}"
        ) from exc
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("Local model returned no choices.")
    message = choices[0].get("message") or {}
    return str(message.get("content") or "").strip()


def _call_bedrock(prompt: str) -> str:
    """Invoke Claude on AWS Bedrock (firm cloud DPA path)."""
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "boto3 required for Bedrock. pip install boto3 or use --destination anthropic"
        ) from exc

    model_id = os.environ.get(
        "ANALYST_BEDROCK_MODEL",
        "anthropic.claude-sonnet-4-20250514-v1:0",
    ).strip()
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    client = boto3.client("bedrock-runtime", region_name=region)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )
    payload = json.loads(resp["body"].read())
    parts = []
    for block in payload.get("content") or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()
