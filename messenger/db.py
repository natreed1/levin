"""SQLite persistence for messenger messages, rooms, and user accounts."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _default_db_path() -> Path:
    raw = os.environ.get("MESSENGER_DB_PATH", "").strip()
    if raw:
        return Path(raw)
    data_dir = Path(os.environ.get("MESSENGER_DATA_DIR", "")).expanduser()
    if not data_dir.parts:
        data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "messages.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _room_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    raw = data.pop("config_json", None)
    config: dict[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                config = parsed
        except json.JSONDecodeError:
            config = {}
    data["kind"] = str(data.get("kind") or "people")
    data["config"] = config
    return data


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
                room_cols = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(rooms)").fetchall()
                }
                if "owner_user_id" not in room_cols:
                    conn.execute(
                        "ALTER TABLE rooms ADD COLUMN owner_user_id TEXT"
                    )
                # Refresh column set after possible ALTER.
                room_cols = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(rooms)").fetchall()
                }
                if "kind" not in room_cols:
                    conn.execute(
                        "ALTER TABLE rooms ADD COLUMN kind TEXT NOT NULL DEFAULT 'people'"
                    )
                if "config_json" not in room_cols:
                    conn.execute(
                        "ALTER TABLE rooms ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'"
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                user_cols = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(users)").fetchall()
                }
                if "email_verified_at" not in user_cols:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN email_verified_at TEXT"
                    )
                    # Grandfather existing accounts as verified.
                    conn.execute(
                        "UPDATE users SET email_verified_at = created_at "
                        "WHERE email_verified_at IS NULL"
                    )
                if "email_2fa_enabled" not in user_cols:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN email_2fa_enabled "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auth_tokens (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        purpose TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        used_at TEXT
                    )
                    """
                )
                token_cols = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(auth_tokens)"
                    ).fetchall()
                }
                if "code_hash" not in token_cols:
                    conn.execute(
                        "ALTER TABLE auth_tokens ADD COLUMN code_hash TEXT"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_auth_tokens_user "
                    "ON auth_tokens(user_id, purpose)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS room_members (
                        room_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        joined_at TEXT NOT NULL,
                        PRIMARY KEY (room_id, user_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        sid TEXT PRIMARY KEY,
                        user_id TEXT,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_room_id "
                    "ON messages(room_id, id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rooms_owner "
                    "ON rooms(owner_user_id)"
                )
                conn.commit()
            finally:
                conn.close()

    # --- users -----------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        email: str,
        password_hash: str,
        display_name: str,
        *,
        email_verified_at: Optional[str] = None,
    ) -> dict[str, Any]:
        created_at = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO users
                        (user_id, email, password_hash, display_name, created_at,
                         email_verified_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        email,
                        password_hash,
                        display_name,
                        created_at,
                        email_verified_at,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("email_taken") from exc
            finally:
                conn.close()
        return {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "created_at": created_at,
            "email_verified_at": email_verified_at,
        }

    def _user_from_row(self, row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        data["email_verified"] = bool(data.get("email_verified_at"))
        data["email_2fa_enabled"] = bool(int(data.get("email_2fa_enabled") or 0))
        return data

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT user_id, email, password_hash, display_name, created_at, "
                    "email_verified_at, email_2fa_enabled FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
            finally:
                conn.close()
        return self._user_from_row(row)

    def user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT user_id, email, password_hash, display_name, created_at, "
                    "email_verified_at, email_2fa_enabled FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            finally:
                conn.close()
        return self._user_from_row(row)

    def mark_email_verified(self, user_id: str) -> None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) "
                    "WHERE user_id = ?",
                    (now, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_password(self, user_id: str, password_hash: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE user_id = ?",
                    (password_hash, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_display_name(self, user_id: str, display_name: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET display_name = ? WHERE user_id = ?",
                    (display_name, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    def set_email_2fa_enabled(self, user_id: str, enabled: bool) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET email_2fa_enabled = ? WHERE user_id = ?",
                    (1 if enabled else 0, user_id),
                )
                conn.commit()
            finally:
                conn.close()

    # --- sessions --------------------------------------------------------------

    def create_session(
        self,
        *,
        sid: str,
        user_id: Optional[str],
        expires_at: str,
    ) -> None:
        created_at = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO sessions (sid, user_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (sid, user_id, created_at, expires_at),
                )
                conn.commit()
            finally:
                conn.close()

    def get_session(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT sid, user_id, created_at, expires_at FROM sessions "
                    "WHERE sid = ?",
                    (sid,),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return dict(row)

    def session_is_active(self, sid: str) -> bool:
        row = self.get_session(sid)
        if not row:
            return False
        expires_at = str(row.get("expires_at") or "")
        if not expires_at:
            return False
        return expires_at >= _utc_now()

    def delete_session(self, sid: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def delete_sessions_for_user(self, user_id: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM sessions WHERE user_id = ?", (user_id,)
                )
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()

    def delete_other_sessions_for_user(self, user_id: str, keep_sid: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND sid != ?",
                    (user_id, keep_sid),
                )
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()

    def count_sessions_for_user(self, user_id: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM sessions "
                    "WHERE user_id = ? AND expires_at >= ?",
                    (user_id, _utc_now()),
                ).fetchone()
            finally:
                conn.close()
        return int(row["count"] if row else 0)

    def create_auth_token(
        self,
        *,
        token_hash: str,
        user_id: str,
        purpose: str,
        expires_at: str,
        code_hash: Optional[str] = None,
    ) -> None:
        created_at = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                # Invalidate prior unused tokens of the same purpose.
                conn.execute(
                    "UPDATE auth_tokens SET used_at = ? "
                    "WHERE user_id = ? AND purpose = ? AND used_at IS NULL",
                    (created_at, user_id, purpose),
                )
                conn.execute(
                    """
                    INSERT INTO auth_tokens
                        (token_hash, user_id, purpose, created_at, expires_at,
                         used_at, code_hash)
                    VALUES (?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        token_hash,
                        user_id,
                        purpose,
                        created_at,
                        expires_at,
                        code_hash,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_auth_token(
        self, *, token_hash: str, purpose: str
    ) -> dict[str, Any] | None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT token_hash, user_id, purpose, created_at, expires_at,
                           used_at, code_hash
                    FROM auth_tokens WHERE token_hash = ? AND purpose = ?
                    """,
                    (token_hash, purpose),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        data = dict(row)
        if data.get("used_at"):
            return None
        if str(data.get("expires_at") or "") < now:
            return None
        return data

    def refresh_auth_token_code(
        self,
        *,
        token_hash: str,
        purpose: str,
        code_hash: str,
        expires_at: str,
    ) -> bool:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE auth_tokens
                    SET code_hash = ?, expires_at = ?, created_at = ?
                    WHERE token_hash = ? AND purpose = ?
                      AND used_at IS NULL AND expires_at >= ?
                    """,
                    (code_hash, expires_at, now, token_hash, purpose, now),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def consume_auth_token(
        self, *, token_hash: str, purpose: str, code_hash: Optional[str] = None
    ) -> dict[str, Any] | None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT token_hash, user_id, purpose, created_at, expires_at,
                           used_at, code_hash
                    FROM auth_tokens WHERE token_hash = ? AND purpose = ?
                    """,
                    (token_hash, purpose),
                ).fetchone()
                if not row:
                    return None
                data = dict(row)
                if data.get("used_at"):
                    return None
                if str(data.get("expires_at") or "") < now:
                    return None
                expected_code = data.get("code_hash")
                if expected_code:
                    if not code_hash or not hmac.compare_digest(
                        str(expected_code), str(code_hash)
                    ):
                        return None
                elif code_hash:
                    return None
                conn.execute(
                    "UPDATE auth_tokens SET used_at = ? WHERE token_hash = ?",
                    (now, token_hash),
                )
                conn.commit()
                return data
            finally:
                conn.close()

    def list_user_ids(self) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT user_id FROM users ORDER BY created_at ASC"
                ).fetchall()
            finally:
                conn.close()
        return [str(r["user_id"]) for r in rows]

    # --- rooms -----------------------------------------------------------------

    def create_room(
        self,
        room_id: str,
        title: str,
        token_hash: str,
        *,
        owner_user_id: Optional[str] = None,
        kind: str = "people",
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        created_at = _utc_now()
        kind_norm = str(kind or "people").strip().lower() or "people"
        if kind_norm not in {"people", "specialist"}:
            kind_norm = "people"
        config_obj = config if isinstance(config, dict) else {}
        config_json = json.dumps(config_obj, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO rooms
                        (room_id, title, token_hash, created_at, owner_user_id,
                         kind, config_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room_id,
                        title,
                        token_hash,
                        created_at,
                        owner_user_id,
                        kind_norm,
                        config_json,
                    ),
                )
                if owner_user_id:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO room_members
                            (room_id, user_id, joined_at)
                        VALUES (?, ?, ?)
                        """,
                        (room_id, owner_user_id, created_at),
                    )
                conn.commit()
            finally:
                conn.close()
        return {
            "room_id": room_id,
            "title": title,
            "created_at": created_at,
            "owner_user_id": owner_user_id,
            "kind": kind_norm,
            "config": config_obj,
        }

    def add_room_member(self, room_id: str, user_id: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO room_members
                        (room_id, user_id, joined_at)
                    VALUES (?, ?, ?)
                    """,
                    (room_id, user_id, _utc_now()),
                )
                conn.commit()
            finally:
                conn.close()

    def user_in_room(self, room_id: str, user_id: str) -> bool:
        if room_id == "legacy":
            return True
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
                    (room_id, user_id),
                ).fetchone()
            finally:
                conn.close()
        return bool(row)

    def list_rooms_for_user(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT r.room_id, r.title, r.created_at, r.owner_user_id,
                           r.kind, r.config_json
                    FROM rooms r
                    INNER JOIN room_members m ON m.room_id = r.room_id
                    WHERE m.user_id = ?
                    ORDER BY r.created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            finally:
                conn.close()
        return [_room_row(r) for r in rows]

    def list_rooms(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT room_id, title, created_at, owner_user_id, kind, config_json "
                    "FROM rooms ORDER BY created_at ASC"
                ).fetchall()
            finally:
                conn.close()
        return [_room_row(r) for r in rows]

    def room(self, room_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT room_id, title, token_hash, created_at, owner_user_id, "
                    "kind, config_json FROM rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
            finally:
                conn.close()
        return _room_row(row) if row else None

    def update_room_config(
        self,
        room_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        config_json = json.dumps(config if isinstance(config, dict) else {}, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE rooms SET config_json = ? WHERE room_id = ?",
                    (config_json, room_id),
                )
                conn.commit()
            finally:
                conn.close()
        return self.room(room_id) if cur.rowcount else None

    def update_room_token(self, room_id: str, token_hash: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE rooms SET token_hash = ? WHERE room_id = ?",
                    (token_hash, room_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def room_token_ok(self, room_id: str, token_hash: str) -> bool:
        room = self.room(room_id)
        return bool(room and hmac.compare_digest(str(room["token_hash"]), token_hash))

    # --- messages --------------------------------------------------------------

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
        created_at = _utc_now()
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

    def delete_room(self, room_id: str) -> bool:
        """Permanently remove a room, its members, and messages.

        Refuses the built-in ``legacy`` room. Returns False if the room
        does not exist (or is legacy).
        """
        if room_id == "legacy":
            return False
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
                if not row:
                    return False
                conn.execute("DELETE FROM messages WHERE room_id = ?", (room_id,))
                conn.execute(
                    "DELETE FROM room_members WHERE room_id = ?",
                    (room_id,),
                )
                conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
                conn.commit()
            finally:
                conn.close()
        return True
