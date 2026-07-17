"""Redacted synthesis jobs with egress audit logging."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from .ledger import Ledger
from .redact import build_synthesis_prompt
from .schema import Event, Sensitivity, Surface


def run_synthesis(
    ledger: Ledger,
    session_id: str,
    instruction: str,
    max_sensitivity: Sensitivity = Sensitivity.INTERNAL,
    dry_run: bool = False,
    destination: str = "anthropic",
) -> Dict[str, Any]:
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


def _call_anthropic(prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Use --dry-run, --destination local_stub, "
            "or set the key (prefer ZDR-enabled commercial org)."
        )
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. pip install 'analyst-ledger[anthropic]' "
            "or use --destination local_stub"
        ) from exc

    model = os.environ.get("ANALYST_CLAUDE_MODEL", "claude-sonnet-5").strip()
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = []
    for block in message.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


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
