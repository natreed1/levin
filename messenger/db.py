"""SQLite persistence for messenger messages."""

from __future__ import annotations

import os
import sqlite3
import threading
import hmac
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_db_path() -> Path:
    raw = os.environ.get("MESSENGER_DB_PATH", "").strip()
    if raw:
        return Path(raw)
    data_dir = Path(os.environ.get("MESSENGER_DATA_DIR", "")).expanduser()
    if not data_dir.parts:
        data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "messages.sqlite3"


class MessageStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        author TEXT NOT NULL,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                if "room_id" not in columns:
                    conn.execute(
                        "ALTER TABLE messages ADD COLUMN room_id TEXT NOT NULL "
                        "DEFAULT 'legacy'"
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS rooms (
                        room_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        token_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_room_id "
                    "ON messages(room_id, id)"
                )
                conn.commit()
            finally:
                conn.close()

    def create_room(self, room_id: str, title: str, token_hash: str) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO rooms (room_id, title, token_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (room_id, title, token_hash, created_at),
                )
                conn.commit()
            finally:
                conn.close()
        return {"room_id": room_id, "title": title, "created_at": created_at}

    def room(self, room_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT room_id, title, token_hash, created_at FROM rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
            finally:
                conn.close()
        return dict(row) if row else None

    def room_token_ok(self, room_id: str, token_hash: str) -> bool:
        room = self.room(room_id)
        return bool(room and hmac.compare_digest(str(room["token_hash"]), token_hash))

    def list_messages(
        self, limit: int = 200, room_id: str = "legacy"
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT id, author, body, created_at
                    FROM messages
                    WHERE room_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (room_id, limit),
                ).fetchall()
            finally:
                conn.close()
        out = [dict(r) for r in reversed(rows)]
        return out

    def add_message(
        self, author: str, body: str, room_id: str = "legacy"
    ) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO messages (author, body, created_at, room_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (author, body, created_at, room_id),
                )
                conn.commit()
                msg_id = int(cur.lastrowid)
            finally:
                conn.close()
        return {
            "id": msg_id,
            "author": author,
            "body": body,
            "created_at": created_at,
        }

    def clear_messages(self, room_id: str = "legacy") -> int:
        """Delete every message in the room. Returns the number of rows removed."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE room_id = ?",
                    (room_id,),
                )
                count = int(cur.fetchone()["n"])
                conn.execute("DELETE FROM messages WHERE room_id = ?", (room_id,))
                conn.commit()
            finally:
                conn.close()
        return count
