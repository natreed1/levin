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


ROOM_ROLES = frozenset({"owner", "editor", "viewer"})


def normalize_room_role(role: Any, *, default: str = "editor") -> str:
    value = str(role or "").strip().lower()
    if value in ROOM_ROLES:
        return value
    fallback = str(default or "editor").strip().lower()
    return fallback if fallback in ROOM_ROLES else "editor"


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
                if "username" not in user_cols:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN username TEXT"
                    )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username
                    ON users(username) WHERE username IS NOT NULL AND username != ''
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS friendships (
                        requester_id TEXT NOT NULL,
                        addressee_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (requester_id, addressee_id),
                        CHECK (requester_id != addressee_id),
                        CHECK (status IN ('pending', 'accepted', 'rejected'))
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_friendships_addressee "
                    "ON friendships(addressee_id, status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_friendships_requester "
                    "ON friendships(requester_id, status)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notifications (
                        notification_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        read_at TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notifications_user "
                    "ON notifications(user_id, created_at DESC)"
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
                        role TEXT NOT NULL DEFAULT 'editor',
                        PRIMARY KEY (room_id, user_id)
                    )
                    """
                )
                member_cols = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(room_members)"
                    ).fetchall()
                }
                if "role" not in member_cols:
                    conn.execute(
                        "ALTER TABLE room_members ADD COLUMN role "
                        "TEXT NOT NULL DEFAULT 'editor'"
                    )
                    # Existing members keep collaborator access; owners marked.
                    conn.execute(
                        """
                        UPDATE room_members
                        SET role = 'owner'
                        WHERE user_id IN (
                            SELECT owner_user_id FROM rooms
                            WHERE rooms.room_id = room_members.room_id
                              AND owner_user_id IS NOT NULL
                              AND owner_user_id != ''
                        )
                        """
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS room_invites (
                        invite_id TEXT PRIMARY KEY,
                        room_id TEXT NOT NULL,
                        inviter_id TEXT NOT NULL,
                        invitee_id TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'editor',
                        status TEXT NOT NULL
                            CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled')),
                        created_at TEXT NOT NULL,
                        responded_at TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_room_invites_invitee "
                    "ON room_invites(invitee_id, status, created_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_room_invites_room "
                    "ON room_invites(room_id, status)"
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
        username: Optional[str] = None,
    ) -> dict[str, Any]:
        created_at = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO users
                        (user_id, email, password_hash, display_name, created_at,
                         email_verified_at, username)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        email,
                        password_hash,
                        display_name,
                        created_at,
                        email_verified_at,
                        username,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "username" in msg:
                    raise ValueError("username_taken") from exc
                raise ValueError("email_taken") from exc
            finally:
                conn.close()
        return {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "username": username,
            "created_at": created_at,
            "email_verified_at": email_verified_at,
        }

    def _user_from_row(self, row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        data["email_verified"] = bool(data.get("email_verified_at"))
        data["email_2fa_enabled"] = bool(int(data.get("email_2fa_enabled") or 0))
        data["username"] = (data.get("username") or None) or None
        return data

    _USER_COLS = (
        "user_id, email, password_hash, display_name, created_at, "
        "email_verified_at, email_2fa_enabled, username"
    )

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {self._USER_COLS} FROM users WHERE email = ?",
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
                    f"SELECT {self._USER_COLS} FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            finally:
                conn.close()
        return self._user_from_row(row)

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {self._USER_COLS} FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
            finally:
                conn.close()
        return self._user_from_row(row)

    def search_users_by_username(
        self, query: str, *, exclude_user_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        q = (query or "").strip().lower().lstrip("@")
        if not q:
            return []
        lim = max(1, min(int(limit or 20), 40))
        pattern = f"%{q}%"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"""
                    SELECT {self._USER_COLS} FROM users
                    WHERE username IS NOT NULL AND username != ''
                      AND username LIKE ?
                      AND user_id != ?
                    ORDER BY
                      CASE WHEN username = ? THEN 0
                           WHEN username LIKE ? THEN 1
                           ELSE 2 END,
                      username ASC
                    LIMIT ?
                    """,
                    (pattern, exclude_user_id, q, f"{q}%", lim),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "display_name": r["display_name"],
            }
            for r in rows
        ]

    def public_user(self, user_id: str) -> dict[str, Any] | None:
        user = self.user_by_id(user_id)
        if not user:
            return None
        return {
            "user_id": user["user_id"],
            "username": user.get("username"),
            "display_name": user["display_name"],
        }

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

    def update_username(self, user_id: str, username: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET username = ? WHERE user_id = ?",
                    (username, user_id),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("username_taken") from exc
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
                        INSERT INTO room_members
                            (room_id, user_id, joined_at, role)
                        VALUES (?, ?, ?, 'owner')
                        ON CONFLICT(room_id, user_id) DO UPDATE SET
                            role = 'owner'
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

    def add_room_member(
        self, room_id: str, user_id: str, *, role: str = "editor"
    ) -> str:
        """Add a member. Returns stored role. Does not demote an existing owner."""
        role_norm = normalize_room_role(role, default="editor")
        if role_norm == "owner":
            role_norm = "editor"
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    """
                    SELECT role FROM room_members
                    WHERE room_id = ? AND user_id = ?
                    """,
                    (room_id, user_id),
                ).fetchone()
                if existing:
                    current = normalize_room_role(
                        existing["role"], default="editor"
                    )
                    if current == "owner":
                        return "owner"
                    return current
                conn.execute(
                    """
                    INSERT INTO room_members
                        (room_id, user_id, joined_at, role)
                    VALUES (?, ?, ?, ?)
                    """,
                    (room_id, user_id, _utc_now(), role_norm),
                )
                conn.commit()
            finally:
                conn.close()
        return role_norm

    def room_member_role(self, room_id: str, user_id: str) -> Optional[str]:
        """Effective role for a user in a room (owner wins over member row)."""
        if room_id == "legacy":
            return "owner"
        room = self.room(room_id)
        if not room:
            return None
        owner = str(room.get("owner_user_id") or "").strip()
        if owner and owner == user_id:
            return "owner"
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT role FROM room_members
                    WHERE room_id = ? AND user_id = ?
                    """,
                    (room_id, user_id),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        role = normalize_room_role(row["role"], default="editor")
        return "editor" if role == "owner" else role

    def set_room_member_role(
        self, room_id: str, user_id: str, role: str
    ) -> Optional[str]:
        """Set editor/viewer for a non-owner member. Returns new role or None."""
        role_norm = normalize_room_role(role, default="editor")
        if role_norm == "owner":
            return None
        room = self.room(room_id)
        if not room:
            return None
        owner = str(room.get("owner_user_id") or "").strip()
        if owner and owner == user_id:
            return None
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE room_members SET role = ?
                    WHERE room_id = ? AND user_id = ?
                    """,
                    (role_norm, room_id, user_id),
                )
                conn.commit()
                if cur.rowcount <= 0:
                    return None
            finally:
                conn.close()
        return role_norm

    def remove_room_member(self, room_id: str, user_id: str) -> bool:
        """Remove a non-owner member from the room."""
        room = self.room(room_id)
        if not room:
            return False
        owner = str(room.get("owner_user_id") or "").strip()
        if owner and owner == user_id:
            return False
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    DELETE FROM room_members
                    WHERE room_id = ? AND user_id = ?
                    """,
                    (room_id, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
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
                           r.kind, r.config_json, m.role AS member_role
                    FROM rooms r
                    INNER JOIN room_members m ON m.room_id = r.room_id
                    WHERE m.user_id = ?
                    ORDER BY r.created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            finally:
                conn.close()
        rooms: list[dict[str, Any]] = []
        for r in rows:
            room = _room_row(r)
            member_role = room.pop("member_role", None)
            owner = str(room.get("owner_user_id") or "").strip()
            if owner and owner == user_id:
                room["my_role"] = "owner"
            else:
                role = normalize_room_role(member_role, default="editor")
                room["my_role"] = "editor" if role == "owner" else role
            rooms.append(room)
        return rooms

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

    def set_room_owner(self, room_id: str, user_id: str) -> dict[str, Any] | None:
        """Assign ownership to an orphan room (no-op if already owned by someone else)."""
        uid = str(user_id or "").strip()
        if not uid or room_id == "legacy":
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT owner_user_id FROM rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
                if not row:
                    return None
                existing = str(row["owner_user_id"] or "").strip()
                if existing and existing != uid:
                    return None
                if existing != uid:
                    conn.execute(
                        "UPDATE rooms SET owner_user_id = ? WHERE room_id = ?",
                        (uid, room_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO room_members
                            (room_id, user_id, joined_at, role)
                        VALUES (?, ?, ?, 'owner')
                        ON CONFLICT(room_id, user_id) DO UPDATE SET
                            role = 'owner'
                        """,
                        (room_id, uid, _utc_now()),
                    )
                    conn.commit()
            finally:
                conn.close()
        return self.room(room_id)

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

    # --- friendships -----------------------------------------------------------

    def friendship_between(
        self, user_a: str, user_b: str
    ) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT requester_id, addressee_id, status, created_at, updated_at
                    FROM friendships
                    WHERE (requester_id = ? AND addressee_id = ?)
                       OR (requester_id = ? AND addressee_id = ?)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_a, user_b, user_b, user_a),
                ).fetchone()
            finally:
                conn.close()
        return dict(row) if row else None

    def create_friend_request(
        self, requester_id: str, addressee_id: str
    ) -> dict[str, Any]:
        if requester_id == addressee_id:
            raise ValueError("self_friend")
        existing = self.friendship_between(requester_id, addressee_id)
        if existing:
            if existing["status"] == "accepted":
                raise ValueError("already_friends")
            if existing["status"] == "pending":
                raise ValueError("request_pending")
            # Rejected → allow a fresh request by replacing the row.
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM friendships WHERE "
                    "(requester_id = ? AND addressee_id = ?) OR "
                    "(requester_id = ? AND addressee_id = ?)",
                    (requester_id, addressee_id, addressee_id, requester_id),
                )
                conn.execute(
                    """
                    INSERT INTO friendships
                        (requester_id, addressee_id, status, created_at, updated_at)
                    VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (requester_id, addressee_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return {
            "requester_id": requester_id,
            "addressee_id": addressee_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }

    def respond_friend_request(
        self, addressee_id: str, requester_id: str, *, accept: bool
    ) -> dict[str, Any]:
        now = _utc_now()
        status = "accepted" if accept else "rejected"
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT requester_id, addressee_id, status, created_at, updated_at
                    FROM friendships
                    WHERE requester_id = ? AND addressee_id = ? AND status = 'pending'
                    """,
                    (requester_id, addressee_id),
                ).fetchone()
                if not row:
                    raise ValueError("request_not_found")
                conn.execute(
                    """
                    UPDATE friendships SET status = ?, updated_at = ?
                    WHERE requester_id = ? AND addressee_id = ?
                    """,
                    (status, now, requester_id, addressee_id),
                )
                if not accept:
                    conn.execute(
                        "DELETE FROM friendships WHERE requester_id = ? AND addressee_id = ?",
                        (requester_id, addressee_id),
                    )
                conn.commit()
            finally:
                conn.close()
        return {
            "requester_id": requester_id,
            "addressee_id": addressee_id,
            "status": status,
            "updated_at": now,
        }

    def remove_friendship(self, user_id: str, other_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    DELETE FROM friendships
                    WHERE (requester_id = ? AND addressee_id = ?)
                       OR (requester_id = ? AND addressee_id = ?)
                    """,
                    (user_id, other_id, other_id, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def list_friends(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"""
                    SELECT
                        CASE WHEN f.requester_id = ? THEN f.addressee_id
                             ELSE f.requester_id END AS friend_id,
                        f.created_at AS friends_since,
                        u.username, u.display_name
                    FROM friendships f
                    INNER JOIN users u ON u.user_id = (
                        CASE WHEN f.requester_id = ? THEN f.addressee_id
                             ELSE f.requester_id END
                    )
                    WHERE f.status = 'accepted'
                      AND (f.requester_id = ? OR f.addressee_id = ?)
                    ORDER BY LOWER(COALESCE(u.username, u.display_name)) ASC
                    """,
                    (user_id, user_id, user_id, user_id),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "user_id": r["friend_id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "friends_since": r["friends_since"],
            }
            for r in rows
        ]

    def list_pending_friend_requests(
        self, user_id: str, *, incoming: bool = True
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                if incoming:
                    rows = conn.execute(
                        f"""
                        SELECT f.requester_id AS other_id, f.created_at,
                               u.username, u.display_name
                        FROM friendships f
                        INNER JOIN users u ON u.user_id = f.requester_id
                        WHERE f.addressee_id = ? AND f.status = 'pending'
                        ORDER BY f.created_at DESC
                        """,
                        (user_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT f.addressee_id AS other_id, f.created_at,
                               u.username, u.display_name
                        FROM friendships f
                        INNER JOIN users u ON u.user_id = f.addressee_id
                        WHERE f.requester_id = ? AND f.status = 'pending'
                        ORDER BY f.created_at DESC
                        """,
                        (user_id,),
                    ).fetchall()
            finally:
                conn.close()
        return [
            {
                "user_id": r["other_id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "created_at": r["created_at"],
                "direction": "incoming" if incoming else "outgoing",
            }
            for r in rows
        ]

    def are_friends(self, user_a: str, user_b: str) -> bool:
        row = self.friendship_between(user_a, user_b)
        return bool(row and row.get("status") == "accepted")

    def list_room_members(self, room_id: str) -> list[dict[str, Any]]:
        room = self.room(room_id)
        owner = str((room or {}).get("owner_user_id") or "").strip()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT m.user_id, m.joined_at, m.role, u.username, u.display_name
                    FROM room_members m
                    INNER JOIN users u ON u.user_id = m.user_id
                    WHERE m.room_id = ?
                    ORDER BY m.joined_at ASC
                    """,
                    (room_id,),
                ).fetchall()
            finally:
                conn.close()
        members: list[dict[str, Any]] = []
        for r in rows:
            uid = str(r["user_id"])
            if owner and uid == owner:
                role = "owner"
            else:
                role = normalize_room_role(r["role"], default="editor")
                if role == "owner":
                    role = "editor"
            members.append(
                {
                    "user_id": uid,
                    "username": r["username"],
                    "display_name": r["display_name"],
                    "joined_at": r["joined_at"],
                    "role": role,
                }
            )
        return members

    # --- room invites ----------------------------------------------------------

    def create_room_invite(
        self,
        room_id: str,
        *,
        inviter_id: str,
        invitee_id: str,
        role: str = "editor",
    ) -> dict[str, Any]:
        """Create or refresh a pending team invite. Raises ValueError on conflict."""
        import secrets

        if inviter_id == invitee_id:
            raise ValueError("self_invite")
        if self.user_in_room(room_id, invitee_id):
            raise ValueError("already_member")
        role_norm = normalize_room_role(role, default="editor")
        if role_norm == "owner":
            role_norm = "editor"
        existing = self.pending_room_invite(room_id, invitee_id)
        if existing:
            # Refresh role on an open invite
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        """
                        UPDATE room_invites SET role = ?, created_at = ?
                        WHERE invite_id = ? AND status = 'pending'
                        """,
                        (role_norm, _utc_now(), existing["invite_id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
            existing["role"] = role_norm
            return existing

        invite_id = "ri" + secrets.token_hex(12)
        created_at = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO room_invites
                        (invite_id, room_id, inviter_id, invitee_id, role,
                         status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        invite_id,
                        room_id,
                        inviter_id,
                        invitee_id,
                        role_norm,
                        created_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return {
            "invite_id": invite_id,
            "room_id": room_id,
            "inviter_id": inviter_id,
            "invitee_id": invitee_id,
            "role": role_norm,
            "status": "pending",
            "created_at": created_at,
            "responded_at": None,
        }

    def pending_room_invite(
        self, room_id: str, invitee_id: str
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT invite_id, room_id, inviter_id, invitee_id, role,
                           status, created_at, responded_at
                    FROM room_invites
                    WHERE room_id = ? AND invitee_id = ? AND status = 'pending'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (room_id, invitee_id),
                ).fetchone()
            finally:
                conn.close()
        return dict(row) if row else None

    def room_invite(self, invite_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT invite_id, room_id, inviter_id, invitee_id, role,
                           status, created_at, responded_at
                    FROM room_invites
                    WHERE invite_id = ?
                    """,
                    (invite_id,),
                ).fetchone()
            finally:
                conn.close()
        return dict(row) if row else None

    def list_pending_room_invites_for_room(
        self, room_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT i.invite_id, i.room_id, i.inviter_id, i.invitee_id,
                           i.role, i.status, i.created_at,
                           u.username, u.display_name
                    FROM room_invites i
                    INNER JOIN users u ON u.user_id = i.invitee_id
                    WHERE i.room_id = ? AND i.status = 'pending'
                    ORDER BY i.created_at ASC
                    """,
                    (room_id,),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "invite_id": r["invite_id"],
                "room_id": r["room_id"],
                "inviter_id": r["inviter_id"],
                "invitee_id": r["invitee_id"],
                "user_id": r["invitee_id"],
                "role": normalize_room_role(r["role"], default="editor"),
                "status": r["status"],
                "created_at": r["created_at"],
                "username": r["username"],
                "display_name": r["display_name"],
            }
            for r in rows
        ]

    def respond_room_invite(
        self, invite_id: str, user_id: str, *, accept: bool
    ) -> dict[str, Any]:
        """Accept or decline a pending invite. Invitee only."""
        invite = self.room_invite(invite_id)
        if not invite or invite.get("status") != "pending":
            raise ValueError("invite_not_found")
        if str(invite.get("invitee_id") or "") != user_id:
            raise ValueError("forbidden")
        room_id = str(invite["room_id"])
        role = normalize_room_role(invite.get("role"), default="editor")
        now = _utc_now()
        status = "accepted" if accept else "rejected"
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE room_invites
                    SET status = ?, responded_at = ?
                    WHERE invite_id = ? AND status = 'pending'
                    """,
                    (status, now, invite_id),
                )
                if cur.rowcount <= 0:
                    raise ValueError("invite_not_found")
                if accept:
                    conn.execute(
                        """
                        INSERT INTO room_members
                            (room_id, user_id, joined_at, role)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(room_id, user_id) DO UPDATE SET
                            role = CASE
                                WHEN room_members.role = 'owner' THEN 'owner'
                                ELSE excluded.role
                            END
                        """,
                        (room_id, user_id, now, role if role != "owner" else "editor"),
                    )
                conn.commit()
            finally:
                conn.close()
        invite["status"] = status
        invite["responded_at"] = now
        return invite

    def cancel_room_invite(self, invite_id: str, *, by_user_id: str) -> bool:
        invite = self.room_invite(invite_id)
        if not invite or invite.get("status") != "pending":
            return False
        room = self.room(str(invite["room_id"]))
        owner = str((room or {}).get("owner_user_id") or "").strip()
        if by_user_id not in {owner, str(invite.get("inviter_id") or "")}:
            return False
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE room_invites
                    SET status = 'cancelled', responded_at = ?
                    WHERE invite_id = ? AND status = 'pending'
                    """,
                    (_utc_now(), invite_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    # --- notifications ---------------------------------------------------------

    def create_notification(
        self,
        user_id: str,
        type_: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        notification_id: Optional[str] = None,
    ) -> dict[str, Any]:
        import secrets

        nid = notification_id or ("n" + secrets.token_hex(12))
        created_at = _utc_now()
        payload_json = json.dumps(payload or {}, separators=(",", ":"))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO notifications
                        (notification_id, user_id, type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (nid, user_id, type_, payload_json, created_at),
                )
                conn.commit()
            finally:
                conn.close()
        return {
            "notification_id": nid,
            "user_id": user_id,
            "type": type_,
            "payload": payload or {},
            "created_at": created_at,
            "read_at": None,
        }

    def list_notifications(
        self, user_id: str, *, limit: int = 50, unread_only: bool = False
    ) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 50), 100))
        with self._lock:
            conn = self._connect()
            try:
                if unread_only:
                    rows = conn.execute(
                        """
                        SELECT notification_id, user_id, type, payload_json,
                               created_at, read_at
                        FROM notifications
                        WHERE user_id = ? AND read_at IS NULL
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (user_id, lim),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT notification_id, user_id, type, payload_json,
                               created_at, read_at
                        FROM notifications
                        WHERE user_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (user_id, lim),
                    ).fetchall()
            finally:
                conn.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload: dict[str, Any] = {}
            raw = row["payload_json"]
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            out.append(
                {
                    "notification_id": row["notification_id"],
                    "user_id": row["user_id"],
                    "type": row["type"],
                    "payload": payload,
                    "created_at": row["created_at"],
                    "read_at": row["read_at"],
                    "unread": row["read_at"] is None,
                }
            )
        return out

    def count_unread_notifications(self, user_id: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM notifications "
                    "WHERE user_id = ? AND read_at IS NULL",
                    (user_id,),
                ).fetchone()
            finally:
                conn.close()
        return int(row["n"] if row else 0)

    def mark_notification_read(
        self, user_id: str, notification_id: str
    ) -> bool:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE notifications SET read_at = COALESCE(read_at, ?)
                    WHERE notification_id = ? AND user_id = ?
                    """,
                    (now, notification_id, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def mark_all_notifications_read(self, user_id: str) -> int:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE notifications SET read_at = ?
                    WHERE user_id = ? AND read_at IS NULL
                    """,
                    (now, user_id),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
