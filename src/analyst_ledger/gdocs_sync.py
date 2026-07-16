"""Google Docs integration via a local export / Drive-synced folder.

There is no silent access to your Google account. Put exports (or Drive Desktop
sync of a dedicated folder) into ANALYST_GDOCS_EXPORT (default ~/AnalystGDocs).

Supported files: .md .txt .docx (Google Doc → File → Download → Plain text /
Markdown / Word), plus optional .gdoc sidecar ignore.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .ledger import Ledger
from .notes_ingest import (
    file_fingerprint,
    ingest_note_text,
    load_seen,
    read_note_file,
    save_seen,
)
from .paths import gdocs_export_dir
from .schema import Surface

SUPPORTED = {".md", ".markdown", ".txt", ".text", ".docx"}


def scan_gdocs(
    ledger: Optional[Ledger] = None,
    export_dir: Optional[Path] = None,
    require_opt_in: bool = False,
) -> int:
    """
    Ingest new/changed files from the Google Docs export folder.

    By default require_opt_in=False for this folder (everything you put here
    is intentional). Add ``ledger: false`` in frontmatter to skip a file.
    """
    ledger = ledger or Ledger()
    root = Path(export_dir) if export_dir else gdocs_export_dir()
    root.mkdir(parents=True, exist_ok=True)

    seen = load_seen("gdocs")
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        fp = file_fingerprint(path)
        if fp in seen:
            continue
        try:
            title, text = read_note_file(path)
        except (ValueError, OSError, KeyError) as exc:
            print(f"gdocs: skip {path.name} ({exc})")
            seen.add(fp)
            continue

        # Allow explicit opt-out
        if "ledger: false" in text[:500].lower() or "ledger:false" in text[:500].lower():
            seen.add(fp)
            continue

        # Auto-stamp opt-in if no frontmatter so ingest_note_text is consistent
        if require_opt_in:
            stamped = text
        else:
            if not text.lstrip().startswith("---"):
                stamped = (
                    f"---\nledger: true\nsource: gdocs\n"
                    f'title: "{title}"\n---\n\n{text}'
                )
            else:
                stamped = text

        result = ingest_note_text(
            ledger,
            title=title,
            text=stamped,
            surface=Surface.GDOCS.value,
            source="gdocs",
            source_path=str(path.resolve()),
            source_id=path.name,
            require_opt_in=require_opt_in,
            attach_file=path,
        )
        seen.add(fp)
        if result:
            count += 1
            print(f"gdocs: {path.name} -> {result['session_id']}")
    save_seen("gdocs", seen)
    return count


def watch_gdocs(
    once: bool = False,
    poll_seconds: float = 5.0,
    export_dir: Optional[Path] = None,
) -> None:
    root = export_dir or gdocs_export_dir()
    print(f"Watching Google Docs export folder: {root}")
    print("Drop .md / .txt / .docx exports here (Drive Desktop sync works too).")
    if once:
        n = scan_gdocs(export_dir=export_dir)
        print(f"done ({n} new file(s))")
        return
    while True:
        scan_gdocs(export_dir=export_dir)
        time.sleep(poll_seconds)
