"""Watch ~/AnalystInbox (or ANALYST_INBOX) for deliberately dropped research files."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Set

from .ledger import Ledger
from .paths import inbox_dir
from .schema import Event, Sensitivity, Surface


SEEN_NAME = ".analyst_inbox_seen.json"


def _load_seen(inbox: Path) -> Set[str]:
    path = inbox / SEEN_NAME
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def _save_seen(inbox: Path, seen: Set[str]) -> None:
    path = inbox / SEEN_NAME
    path.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def scan_once(ledger: Optional[Ledger] = None) -> int:
    ledger = ledger or Ledger()
    inbox = inbox_dir()
    seen = _load_seen(inbox)
    count = 0
    for path in sorted(inbox.iterdir()):
        if path.name.startswith("."):
            continue
        if not path.is_file():
            continue
        key = f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
        if key in seen:
            continue
        sid = ledger.get_active_session_id()
        if not sid:
            session = ledger.start_session(
                title=f"Inbox: {path.name}",
                surface=Surface.INBOX.value,
                sensitivity=Sensitivity.INTERNAL.value,
            )
            sid = session.session_id
        art = ledger.attach_artifact(path, session_id=sid, sensitivity=Sensitivity.INTERNAL.value)
        ledger.append_event(
            Event(
                type="inbox_file",
                surface=Surface.INBOX.value,
                session_id=sid,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "name": path.name,
                    "path": str(path.resolve()),
                    "artifact_id": art.artifact_id,
                },
            )
        )
        seen.add(key)
        count += 1
        print(f"ingested: {path.name} -> session {sid}")
    _save_seen(inbox, seen)
    return count


def watch_inbox(once: bool = False, poll_seconds: float = 2.0) -> None:
    inbox = inbox_dir()
    print(f"Watching inbox: {inbox}")
    if once:
        n = scan_once()
        print(f"done ({n} new file(s))")
        return
    while True:
        scan_once()
        time.sleep(poll_seconds)
