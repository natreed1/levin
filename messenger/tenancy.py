"""Per-user ledger scoping for the unified workflow messenger."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from analyst_ledger.ledger import Ledger
from analyst_ledger.paths import use_data_dir

_USER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}$")


def users_root() -> Path:
    """Root directory holding one ledger data dir per user id."""
    raw = os.environ.get("MESSENGER_USERS_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        data_dir = Path(os.environ.get("MESSENGER_DATA_DIR", "")).expanduser()
        if not data_dir.parts:
            data_dir = Path(__file__).resolve().parent / "data"
        path = data_dir / "users"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_data_dir(user_id: str) -> Path:
    uid = str(user_id or "").strip()
    if not uid or not _USER_ID_RE.match(uid):
        raise ValueError(f"invalid user_id: {user_id!r}")
    path = users_root() / uid
    path.mkdir(parents=True, exist_ok=True)
    (path / "events").mkdir(parents=True, exist_ok=True)
    (path / "artifacts").mkdir(parents=True, exist_ok=True)
    (path / "rituals" / "specs").mkdir(parents=True, exist_ok=True)
    return path


def user_ledger(user_id: str) -> Ledger:
    """Return a Ledger whose SQLite file lives under the user's data dir.

    Callers that also touch rituals/paths should wrap work in
    :func:`user_context` so ``ritual_specs_dir()`` etc. resolve correctly.
    """
    root = user_data_dir(user_id)
    return Ledger(db_path=root / "ledger.sqlite3")


@contextmanager
def user_context(user_id: str) -> Iterator[Ledger]:
    """Bind paths + return a Ledger for ``user_id`` for the duration of the block."""
    root = user_data_dir(user_id)
    with use_data_dir(root):
        yield Ledger(db_path=root / "ledger.sqlite3")
