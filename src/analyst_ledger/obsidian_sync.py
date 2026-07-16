"""Obsidian vault sync — opt-in via frontmatter ledger: true or #ledger tag."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional

from .ledger import Ledger
from .notes_ingest import (
    file_fingerprint,
    ingest_note_text,
    load_seen,
    parse_frontmatter,
    save_seen,
    wants_ledger,
)
from .paths import obsidian_vault
from .schema import Surface

SKIP_DIR_NAMES = {".obsidian", ".git", ".trash", "node_modules", ".smart-env"}


def _iter_markdown(vault: Path, subdir: Optional[str] = None) -> List[Path]:
    root = vault / subdir if subdir else vault
    if not root.exists():
        return []
    out: List[Path] = []
    for path in root.rglob("*.md"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name.startswith("."):
            continue
        out.append(path)
    return sorted(out)


def scan_obsidian(
    ledger: Optional[Ledger] = None,
    vault: Optional[Path] = None,
    subdir: Optional[str] = None,
    require_opt_in: bool = True,
) -> int:
    ledger = ledger or Ledger()
    vault = vault or obsidian_vault()
    if vault is None:
        raise RuntimeError(
            "Set ANALYST_OBSIDIAN_VAULT to your vault path, or pass --vault"
        )
    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir():
        raise RuntimeError(f"Obsidian vault not found: {vault}")

    # Optional: only a Research/ subfolder
    env_sub = os.environ.get("ANALYST_OBSIDIAN_SUBDIR", "").strip()
    subdir = subdir if subdir is not None else (env_sub or None)

    seen = load_seen("obsidian")
    count = 0
    for path in _iter_markdown(vault, subdir):
        fp = file_fingerprint(path)
        if fp in seen:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        if require_opt_in and not wants_ledger(meta, body):
            # Remember skip so we don't re-read forever unless file changes
            seen.add(fp)
            continue
        result = ingest_note_text(
            ledger,
            title=path.stem,
            text=text,
            surface=Surface.OBSIDIAN.value,
            source="obsidian",
            source_path=str(path),
            source_id=str(path.relative_to(vault)),
            require_opt_in=False,  # already checked
            attach_file=path,
        )
        seen.add(fp)
        if result:
            count += 1
            print(f"obsidian: {path.relative_to(vault)} -> {result['session_id']}")
    save_seen("obsidian", seen)
    return count


def watch_obsidian(
    once: bool = False,
    poll_seconds: float = 3.0,
    vault: Optional[Path] = None,
    subdir: Optional[str] = None,
    require_opt_in: bool = True,
) -> None:
    v = vault or obsidian_vault()
    print(f"Watching Obsidian vault: {v} (opt-in={'on' if require_opt_in else 'off'})")
    if once:
        n = scan_obsidian(
            vault=vault, subdir=subdir, require_opt_in=require_opt_in
        )
        print(f"done ({n} new note(s))")
        return
    while True:
        scan_obsidian(vault=vault, subdir=subdir, require_opt_in=require_opt_in)
        time.sleep(poll_seconds)
