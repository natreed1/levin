"""Apple Notes sync via osascript export → local markdown → ledger ingest.

Requires macOS + Notes.app access (Automation permission on first run).
Only exports notes from a configured folder (default: AnalystLedger).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from .ledger import Ledger
from .notes_ingest import (
    content_fingerprint,
    file_fingerprint,
    ingest_note_text,
    load_seen,
    save_seen,
)
from .paths import apple_notes_export_dir
from .schema import Surface


def apple_notes_folder_name() -> str:
    return os.environ.get("ANALYST_APPLE_NOTES_FOLDER", "AnalystLedger").strip() or "AnalystLedger"


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]+", "_", name).strip() or "note"
    return cleaned[:80]


def export_apple_notes(
    folder: Optional[str] = None,
    export_dir: Optional[Path] = None,
) -> List[Path]:
    """
    Export notes from an Apple Notes folder to markdown files.

    Returns list of written/updated paths.
    """
    folder = folder or apple_notes_folder_name()
    export_dir = Path(export_dir) if export_dir else apple_notes_export_dir()
    export_dir.mkdir(parents=True, exist_ok=True)

    # AppleScript: dump id|title|body for notes in folder
    script = f'''
    set AppleScript's text item delimiters to ""
    set outText to ""
    tell application "Notes"
      set theFolder to missing value
      repeat with f in folders
        if name of f is "{folder}" then
          set theFolder to f
          exit repeat
        end if
      end repeat
      if theFolder is missing value then
        return "FOLDER_NOT_FOUND"
      end if
      repeat with n in notes of theFolder
        set nid to id of n as string
        set ntitle to name of n as string
        set nbody to plaintext of n as string
        set outText to outText & "<<<NOTE>>>\\n" & nid & "\\n" & ntitle & "\\n" & nbody & "\\n<<<END>>>\\n"
      end repeat
    end tell
    return outText
    '''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("osascript not found — Apple Notes sync requires macOS") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Apple Notes export timed out") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Apple Notes export failed: {err or proc.returncode}")

    raw = proc.stdout or ""
    if "FOLDER_NOT_FOUND" in raw.strip():
        raise RuntimeError(
            f'Apple Notes folder "{folder}" not found. '
            f'Create a folder named "{folder}" in Notes and put research notes there.'
        )

    written: List[Path] = []
    blocks = raw.split("<<<NOTE>>>")
    for block in blocks:
        block = block.strip()
        if not block or "<<<END>>>" not in block:
            continue
        body_part, _ = block.split("<<<END>>>", 1)
        lines = body_part.split("\n", 2)
        if len(lines) < 3:
            continue
        note_id, title, body = lines[0].strip(), lines[1].strip(), lines[2]
        # Prefix with ledger opt-in so ingest accepts by default from this folder
        md = (
            f"---\nledger: true\nsource: apple_notes\nnote_id: {note_id}\n"
            f'title: "{title.replace(chr(34), "")}"\n---\n\n'
            f"# {title}\n\n{body.strip()}\n"
        )
        fname = _sanitize_filename(title) + ".md"
        # Include short id to avoid collisions
        short = re.sub(r"\W+", "", note_id)[-12:] or "note"
        path = export_dir / f"{short}_{fname}"
        path.write_text(md, encoding="utf-8")
        written.append(path)
    return written


def scan_apple_notes(
    ledger: Optional[Ledger] = None,
    folder: Optional[str] = None,
    export_only: bool = False,
    skip_export: bool = False,
) -> int:
    """
    Export from Notes.app (unless skip_export) then ingest new/changed markdown.
    """
    ledger = ledger or Ledger()
    export_dir = apple_notes_export_dir()
    if not skip_export:
        try:
            paths = export_apple_notes(folder=folder, export_dir=export_dir)
            print(f"apple_notes: exported {len(paths)} note(s) from folder")
        except RuntimeError as exc:
            # Allow ingesting previously exported files if Notes unavailable in CI
            print(f"apple_notes: export skipped ({exc})")
            if export_only:
                raise
    if export_only:
        return 0

    seen = load_seen("apple_notes")
    count = 0
    for path in sorted(export_dir.glob("*.md")):
        fp = file_fingerprint(path)
        if fp in seen:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Dedup by content too across renames
        cfp = content_fingerprint(text, "apple_notes")
        if cfp in seen:
            seen.add(fp)
            continue
        result = ingest_note_text(
            ledger,
            title=path.stem,
            text=text,
            surface=Surface.APPLE_NOTES.value,
            source="apple_notes",
            source_path=str(path),
            source_id=path.name,
            require_opt_in=True,
            attach_file=path,
        )
        seen.add(fp)
        seen.add(cfp)
        if result:
            count += 1
            print(f"apple_notes: {path.name} -> {result['session_id']}")
    save_seen("apple_notes", seen)
    return count


def watch_apple_notes(
    once: bool = False,
    poll_seconds: float = 30.0,
    folder: Optional[str] = None,
) -> None:
    print(f"Watching Apple Notes folder: {folder or apple_notes_folder_name()}")
    if once:
        n = scan_apple_notes(folder=folder)
        print(f"done ({n} new note(s))")
        return
    while True:
        scan_apple_notes(folder=folder)
        time.sleep(poll_seconds)
