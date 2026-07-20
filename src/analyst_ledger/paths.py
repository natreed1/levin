"""Paths and environment configuration for the local ledger."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional


def repo_root() -> Path:
    """Project root (parent of src/)."""
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    raw = os.environ.get("ANALYST_LEDGER_DATA", "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        path = repo_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def events_dir() -> Path:
    path = data_dir() / "events"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifacts_dir() -> Path:
    path = data_dir() / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sqlite_path() -> Path:
    raw = os.environ.get("ANALYST_LEDGER_DB", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return data_dir() / "ledger.sqlite3"


def state_path() -> Path:
    return data_dir() / "active_session.json"


def inbox_dir() -> Path:
    raw = os.environ.get("ANALYST_INBOX", "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        path = Path.home() / "AnalystInbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sft_export_dir() -> Path:
    path = data_dir() / "sft"
    path.mkdir(parents=True, exist_ok=True)
    return path


def rituals_dir() -> Path:
    path = data_dir() / "rituals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ritual_specs_dir() -> Path:
    path = rituals_dir() / "specs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ritual_builds_dir() -> Path:
    path = rituals_dir() / "builds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def claude_skills_dir() -> Optional[Path]:
    """Optional Claude skills install directory (ANALYST_CLAUDE_SKILLS_DIR)."""
    raw = os.environ.get("ANALYST_CLAUDE_SKILLS_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def obsidian_vault() -> Optional[Path]:
    raw = os.environ.get("ANALYST_OBSIDIAN_VAULT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def apple_notes_export_dir() -> Path:
    raw = os.environ.get("ANALYST_APPLE_NOTES_EXPORT", "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        path = data_dir() / "apple_notes_export"
    path.mkdir(parents=True, exist_ok=True)
    return path


def gdocs_export_dir() -> Path:
    raw = os.environ.get("ANALYST_GDOCS_EXPORT", "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        path = Path.home() / "AnalystGDocs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_search_roots() -> "List[Path]":
    """Folders the local file finder may search (ANALYST_FILE_SEARCH_ROOTS).

    Separated by os.pathsep (';' on Windows). Feature is inert when unset:
    returns [] and nothing is ever created or scanned.
    """
    raw = os.environ.get("ANALYST_FILE_SEARCH_ROOTS", "").strip()
    if not raw:
        return []
    roots: List[Path] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        path = Path(chunk).expanduser()
        try:
            path = path.resolve()
        except OSError:
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def sync_state_dir() -> Path:
    path = data_dir() / "sync_state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def arena_dir() -> Path:
    """Disposable dual-run arena trials (isolated from workflow chats)."""
    path = data_dir() / "arena"
    path.mkdir(parents=True, exist_ok=True)
    return path


def arena_trials_dir() -> Path:
    path = arena_dir() / "trials"
    path.mkdir(parents=True, exist_ok=True)
    return path
