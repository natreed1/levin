#!/usr/bin/env python3
"""
Append a ledger event after shell commands (Cursor afterShellExecution hook).

Opt-in via ``ANALYST_CURSOR_HOOK=1``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analyst_ledger.ledger import Ledger  # noqa: E402
from analyst_ledger.schema import Event, Sensitivity, Surface  # noqa: E402

MAX_CMD = 500


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    if not _truthy("ANALYST_CURSOR_HOOK"):
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_parse_error": "invalid_json"}

    command = str(payload.get("command") or payload.get("cmd") or "")[:MAX_CMD]
    exit_code = payload.get("exit_code", payload.get("exitCode"))

    os.environ.setdefault("ANALYST_LEDGER_DATA", str(REPO_ROOT / "data"))
    ledger = Ledger()
    sid = ledger.get_active_session_id()

    ledger.append_event(
        Event(
            type="shell",
            surface=Surface.CURSOR.value,
            session_id=sid,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={
                "hook": "afterShellExecution",
                "command": command,
                "exit_code": exit_code,
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
