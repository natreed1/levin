#!/usr/bin/env python3
"""
Append a ledger event when Cursor fires the workspace ``stop`` hook.

Opt-in via env ``ANALYST_CURSOR_HOOK=1`` so clones do not silently grow ledgers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow running without install by putting src on path
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analyst_ledger.ledger import Ledger  # noqa: E402
from analyst_ledger.schema import Event, Sensitivity, Surface  # noqa: E402

MAX_EMBED = 4000


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    if not _truthy("ANALYST_CURSOR_HOOK"):
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_parse_error": "invalid_json", "_snippet": raw[:MAX_EMBED]}

    if raw and len(raw) > MAX_EMBED:
        payload = dict(payload)
        payload["_truncated_stdio"] = True
        payload["_stdio_len"] = len(raw)

    os.environ.setdefault("ANALYST_LEDGER_DATA", str(REPO_ROOT / "data"))
    ledger = Ledger()
    sid = ledger.get_active_session_id()
    if not sid and _truthy("ANALYST_CURSOR_HOOK_AUTO_SESSION"):
        session = ledger.start_session(
            title="Cursor agent session",
            surface=Surface.CURSOR.value,
            sensitivity=Sensitivity.INTERNAL.value,
        )
        sid = session.session_id

    ledger.append_event(
        Event(
            type="agent_stop",
            surface=Surface.CURSOR.value,
            session_id=sid,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={"hook": "stop", "cursor": payload},
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
