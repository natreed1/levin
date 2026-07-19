"""Append-only JSONL + SQLite index for sessions, events, artifacts, egress."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .paths import artifacts_dir, events_dir, sqlite_path, state_path
from .schema import (
    SESSION_TAGS,
    ArtifactRef,
    Event,
    Sensitivity,
    Session,
    Surface,
    new_id,
    parse_sensitivity,
    parse_surface,
    utc_now_iso,
)


def _day_key(ts: Optional[str] = None) -> str:
    if ts:
        return ts[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Ledger:
    """Local system of record: JSONL is canonical; SQLite is the query index."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else sqlite_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    desk_tag TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'open'
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    session_id TEXT,
                    sensitivity TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sensitivity TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS egress_audit (
                    audit_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    session_id TEXT,
                    destination TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    prompt_chars INTEGER NOT NULL,
                    max_sensitivity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    session_id TEXT,
                    synthesis_event_id TEXT,
                    label TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    edited_output TEXT
                );
                """
            )

    def _append_jsonl(self, event: Event) -> Path:
        day = _day_key(event.ts)
        path = events_dir() / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return path

    def append_event(self, event: Event) -> Event:
        self._append_jsonl(event)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events
                (event_id, ts, type, surface, session_id, sensitivity, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.ts,
                    event.type,
                    event.surface,
                    event.session_id,
                    event.sensitivity,
                    json.dumps(event.payload, ensure_ascii=False),
                ),
            )
        return event

    # --- active session state file ---

    def get_active_session_id(self) -> Optional[str]:
        path = state_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data.get("session_id")

    def set_active_session_id(self, session_id: Optional[str]) -> None:
        path = state_path()
        if session_id is None:
            if path.exists():
                path.unlink()
            return
        path.write_text(
            json.dumps({"session_id": session_id, "updated_at": utc_now_iso()}, indent=2),
            encoding="utf-8",
        )

    def get_session(self, session_id: str) -> Optional[Session]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return Session(
            session_id=row["session_id"],
            title=row["title"],
            surface=row["surface"],
            sensitivity=row["sensitivity"],
            desk_tag=row["desk_tag"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            tags=json.loads(row["tags_json"] or "[]"),
            status=row["status"],
        )

    def start_session(
        self,
        title: str,
        surface: str = Surface.NOTES.value,
        sensitivity: str = Sensitivity.INTERNAL.value,
        desk_tag: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        parse_surface(surface)
        parse_sensitivity(sensitivity)
        active = self.get_active_session_id()
        if active:
            existing = self.get_session(active)
            if existing and existing.status == "open":
                raise RuntimeError(
                    f"Session '{active}' is already open. End it first with: analyst session end"
                )

        sid = session_id or new_id("sess")
        session = Session(
            session_id=sid,
            title=title,
            surface=surface,
            sensitivity=sensitivity,
            desk_tag=desk_tag,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                (session_id, title, surface, sensitivity, desk_tag, started_at, ended_at, tags_json, status)
                VALUES (?, ?, ?, ?, ?, ?, NULL, '[]', 'open')
                """,
                (
                    session.session_id,
                    session.title,
                    session.surface,
                    session.sensitivity,
                    session.desk_tag,
                    session.started_at,
                ),
            )
        self.append_event(
            Event(
                type="session_start",
                surface=surface,
                session_id=sid,
                sensitivity=sensitivity,
                payload={
                    "title": title,
                    "desk_tag": desk_tag,
                },
            )
        )
        self.set_active_session_id(sid)
        return session

    def start_background_session(
        self,
        title: str,
        surface: str = Surface.SYSTEM.value,
        sensitivity: str = Sensitivity.INTERNAL.value,
        desk_tag: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        """Create a session without replacing the analyst's active capture session."""
        parse_surface(surface)
        parse_sensitivity(sensitivity)
        sid = session_id or new_id("sess")
        session = Session(
            session_id=sid,
            title=title,
            surface=surface,
            sensitivity=sensitivity,
            desk_tag=desk_tag,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                (session_id, title, surface, sensitivity, desk_tag, started_at, ended_at, tags_json, status)
                VALUES (?, ?, ?, ?, ?, ?, NULL, '[]', 'open')
                """,
                (
                    session.session_id,
                    session.title,
                    session.surface,
                    session.sensitivity,
                    session.desk_tag,
                    session.started_at,
                ),
            )
        self.append_event(
            Event(
                type="session_start",
                surface=surface,
                session_id=sid,
                sensitivity=sensitivity,
                payload={"title": title, "desk_tag": desk_tag, "background": True},
            )
        )
        return session

    def get_or_create_chat_thread(
        self, ritual_id: Optional[str] = None, *, master: bool = False
    ) -> Session:
        """Return the persistent master or per-workflow chat thread."""
        desk_tag = "chat:master" if master else f"chat:{ritual_id or ''}"
        if not master and not ritual_id:
            raise ValueError("ritual_id is required for a workflow chat")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id FROM sessions
                WHERE surface = ? AND desk_tag = ? AND status = 'open'
                ORDER BY started_at DESC LIMIT 1
                """,
                (Surface.CHAT.value, desk_tag),
            ).fetchone()
        if row:
            existing = self.get_session(row["session_id"])
            if existing:
                return existing
        title = "Master workflows" if master else str(ritual_id).replace("_", " ").title()
        return self.start_background_session(
            title=title,
            surface=Surface.CHAT.value,
            sensitivity=Sensitivity.INTERNAL.value,
            desk_tag=desk_tag,
        )

    def create_arena_thread(
        self, ritual_id: str, trial_id: str, lane: str
    ) -> Session:
        """Ephemeral chat lane for dual-run arena (hidden from normal Chats list)."""
        if not ritual_id:
            raise ValueError("ritual_id is required for an arena thread")
        lane_key = str(lane).strip().lower()
        if lane_key not in {"a", "b"}:
            raise ValueError("arena lane must be 'a' or 'b'")
        desk_tag = f"arena:{trial_id}:{lane_key}"
        title = f"Arena {lane_key.upper()} · {str(ritual_id).replace('_', ' ').title()}"
        return self.start_background_session(
            title=title,
            surface=Surface.CHAT.value,
            sensitivity=Sensitivity.INTERNAL.value,
            desk_tag=desk_tag,
        )

    def append_chat_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        kind: str = "message",
        metadata: Optional[Dict[str, Any]] = None,
        sensitivity: str = Sensitivity.INTERNAL.value,
    ) -> Event:
        """Append one persistent chat message through the canonical ledger path."""
        session = self.get_session(session_id)
        if not session or session.surface != Surface.CHAT.value:
            raise RuntimeError(f"Chat thread '{session_id}' not found.")
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError(f"invalid chat role: {role}")
        parse_sensitivity(sensitivity)
        return self.append_event(
            Event(
                type="chat_message",
                surface=Surface.CHAT.value,
                session_id=session_id,
                sensitivity=sensitivity,
                payload={
                    "role": role,
                    "content": str(content),
                    "kind": kind,
                    "metadata": metadata or {},
                },
            )
        )

    def list_chat_messages(self, session_id: str, limit: int = 300) -> List[Dict[str, Any]]:
        """Return chronological chat messages; restricted content is never read."""
        events = self.list_events(
            session_id=session_id, limit=max(1, min(limit, 1000)), types=["chat_message"]
        )
        out = [
            ev
            for ev in reversed(events)
            if ev.get("sensitivity") != Sensitivity.RESTRICTED.value
        ]
        return out

    def list_chat_threads(self) -> List[Dict[str, Any]]:
        """List the master thread first, followed by workflow chats.

        Arena lanes (desk_tag ``arena:…``) are evaluation sandboxes and are
        intentionally omitted so they do not clutter the coding/chat sidebar.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE surface = ? AND status = 'open'
                  AND desk_tag LIKE 'chat:%'
                ORDER BY CASE WHEN desk_tag = 'chat:master' THEN 0 ELSE 1 END, started_at DESC
                """,
                (Surface.CHAT.value,),
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "desk_tag": row["desk_tag"],
                "ritual_id": (
                    None
                    if row["desk_tag"] == "chat:master"
                    else str(row["desk_tag"] or "").removeprefix("chat:")
                ),
                "master": row["desk_tag"] == "chat:master",
                "started_at": row["started_at"],
            }
            for row in rows
        ]

    def end_session(
        self,
        session_id: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> Session:
        sid = session_id or self.get_active_session_id()
        if not sid:
            raise RuntimeError("No active session to end.")
        session = self.get_session(sid)
        if not session:
            raise RuntimeError(f"Session '{sid}' not found.")
        if session.status == "closed":
            raise RuntimeError(f"Session '{sid}' is already closed.")

        tag_list = list(tags or [])
        for t in tag_list:
            if t not in SESSION_TAGS:
                raise ValueError(f"Unknown tag '{t}'. Expected one of: {', '.join(sorted(SESSION_TAGS))}")

        ended = utc_now_iso()
        merged = sorted(set(session.tags) | set(tag_list))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET ended_at = ?, tags_json = ?, status = 'closed'
                WHERE session_id = ?
                """,
                (ended, json.dumps(merged), sid),
            )
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE session_id = ?", (sid,)
            ).fetchone()["c"]

        self.append_event(
            Event(
                type="session_end",
                surface=session.surface,
                session_id=sid,
                sensitivity=session.sensitivity,
                payload={
                    "tags": merged,
                    "event_count": count,
                    "started_at": session.started_at,
                    "ended_at": ended,
                },
            )
        )
        if self.get_active_session_id() == sid:
            self.set_active_session_id(None)
        session.ended_at = ended
        session.tags = merged
        session.status = "closed"
        return session

    def add_note(
        self,
        text: str,
        session_id: Optional[str] = None,
        sensitivity: Optional[str] = None,
        surface: str = Surface.NOTES.value,
    ) -> Event:
        sid = session_id or self.get_active_session_id()
        if not sid:
            raise RuntimeError("No active session. Start one with: analyst session start \"title\"")
        session = self.get_session(sid)
        if not session or session.status != "open":
            raise RuntimeError(f"Session '{sid}' is not open.")
        sens = sensitivity or session.sensitivity
        parse_sensitivity(sens)
        return self.append_event(
            Event(
                type="note",
                surface=surface,
                session_id=sid,
                sensitivity=sens,
                payload={"text": text},
            )
        )

    def add_tag(self, tag: str, session_id: Optional[str] = None) -> Event:
        if tag not in SESSION_TAGS:
            raise ValueError(f"Unknown tag '{tag}'. Expected one of: {', '.join(sorted(SESSION_TAGS))}")
        sid = session_id or self.get_active_session_id()
        if not sid:
            raise RuntimeError("No active session.")
        session = self.get_session(sid)
        if not session:
            raise RuntimeError(f"Session '{sid}' not found.")
        tags = sorted(set(session.tags) | {tag})
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET tags_json = ? WHERE session_id = ?",
                (json.dumps(tags), sid),
            )
        return self.append_event(
            Event(
                type="tag",
                surface=session.surface,
                session_id=sid,
                sensitivity=session.sensitivity,
                payload={"tag": tag, "tags": tags},
            )
        )

    def attach_artifact(
        self,
        file_path: Path,
        session_id: Optional[str] = None,
        sensitivity: Optional[str] = None,
        copy_into_store: bool = True,
    ) -> ArtifactRef:
        sid = session_id or self.get_active_session_id()
        src = Path(file_path).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(str(src))

        raw = src.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        mime = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
        session = self.get_session(sid) if sid else None
        sens = sensitivity or (session.sensitivity if session else Sensitivity.INTERNAL.value)
        parse_sensitivity(sens)

        if copy_into_store:
            dest_dir = artifacts_dir() / (sid or "unscoped")
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{digest[:16]}_{src.name}"
            if not dest.exists():
                dest.write_bytes(raw)
            stored_path = str(dest)
        else:
            stored_path = str(src)

        art = ArtifactRef(
            path=stored_path,
            sha256=digest,
            mime=mime,
            size_bytes=len(raw),
            sensitivity=sens,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts
                (artifact_id, session_id, path, sha256, mime, size_bytes, sensitivity, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    art.artifact_id,
                    sid,
                    art.path,
                    art.sha256,
                    art.mime,
                    art.size_bytes,
                    art.sensitivity,
                    utc_now_iso(),
                ),
            )
        self.append_event(
            Event(
                type="artifact_attach",
                surface=session.surface if session else Surface.SYSTEM.value,
                session_id=sid,
                sensitivity=sens,
                payload=art.to_dict(),
            )
        )
        return art

    def record_egress(
        self,
        destination: str,
        prompt: str,
        max_sensitivity: str,
        status: str,
        session_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> str:
        audit_id = new_id("aud")
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        detail = detail or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO egress_audit
                (audit_id, ts, session_id, destination, prompt_sha256, prompt_chars,
                 max_sensitivity, status, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    utc_now_iso(),
                    session_id,
                    destination,
                    prompt_sha,
                    len(prompt),
                    max_sensitivity,
                    status,
                    json.dumps(detail, ensure_ascii=False),
                ),
            )
        self.append_event(
            Event(
                type="egress_audit",
                surface=Surface.SYNTHESIS.value,
                session_id=session_id,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "audit_id": audit_id,
                    "destination": destination,
                    "prompt_sha256": prompt_sha,
                    "prompt_chars": len(prompt),
                    "max_sensitivity": max_sensitivity,
                    "status": status,
                    "detail": detail,
                },
            )
        )
        return audit_id

    def add_feedback(
        self,
        label: str,
        session_id: Optional[str] = None,
        synthesis_event_id: Optional[str] = None,
        notes: str = "",
        edited_output: Optional[str] = None,
    ) -> str:
        allowed = {"accept", "reject", "edit"}
        if label not in allowed:
            raise ValueError(f"label must be one of {sorted(allowed)}")
        sid = session_id or self.get_active_session_id()
        feedback_id = new_id("fb")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback
                (feedback_id, ts, session_id, synthesis_event_id, label, notes, edited_output)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    utc_now_iso(),
                    sid,
                    synthesis_event_id,
                    label,
                    notes,
                    edited_output,
                ),
            )
        self.append_event(
            Event(
                type="feedback",
                surface=Surface.SYNTHESIS.value,
                session_id=sid,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "feedback_id": feedback_id,
                    "label": label,
                    "notes": notes,
                    "synthesis_event_id": synthesis_event_id,
                    "has_edited_output": edited_output is not None,
                },
            )
        )
        return feedback_id

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "session_id": row["session_id"],
                    "title": row["title"],
                    "surface": row["surface"],
                    "sensitivity": row["sensitivity"],
                    "desk_tag": row["desk_tag"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "tags": json.loads(row["tags_json"] or "[]"),
                    "status": row["status"],
                }
            )
        return out

    def list_events(
        self,
        session_id: Optional[str] = None,
        limit: int = 200,
        types: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM events"
        clauses: List[str] = []
        params: List[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        type_list = list(types or [])
        if type_list:
            placeholders = ",".join("?" * len(type_list))
            clauses.append(f"type IN ({placeholders})")
            params.extend(type_list)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "event_id": row["event_id"],
                    "ts": row["ts"],
                    "type": row["type"],
                    "surface": row["surface"],
                    "session_id": row["session_id"],
                    "sensitivity": row["sensitivity"],
                    "payload": json.loads(row["payload_json"]),
                }
            )
        return out

    def session_context_for_synthesis(
        self,
        session_id: str,
        max_sensitivity: Sensitivity = Sensitivity.INTERNAL,
    ) -> Dict[str, Any]:
        """Build a redaction-ready context pack for a session."""
        from .redact import filter_events_for_egress

        session = self.get_session(session_id)
        if not session:
            raise RuntimeError(f"Session '{session_id}' not found.")
        events = self.list_events(session_id=session_id, limit=500)
        events.reverse()  # chronological
        allowed = filter_events_for_egress(events, max_sensitivity)
        with self._connect() as conn:
            arts = conn.execute(
                "SELECT * FROM artifacts WHERE session_id = ?", (session_id,)
            ).fetchall()
        artifact_meta = []
        for row in arts:
            if parse_sensitivity(row["sensitivity"]).value == Sensitivity.RESTRICTED.value:
                continue
            if _rank(row["sensitivity"]) > _rank(max_sensitivity.value):
                continue
            artifact_meta.append(
                {
                    "artifact_id": row["artifact_id"],
                    "path": row["path"],
                    "mime": row["mime"],
                    "sha256": row["sha256"],
                    "sensitivity": row["sensitivity"],
                }
            )
        return {
            "session": session.to_dict(),
            "events": allowed,
            "artifacts": artifact_meta,
        }

    def summary(self) -> Dict[str, Any]:
        with self._connect() as conn:
            sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
            events = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            open_s = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE status = 'open'"
            ).fetchone()["c"]
            egress = conn.execute("SELECT COUNT(*) AS c FROM egress_audit").fetchone()["c"]
            feedback = conn.execute("SELECT COUNT(*) AS c FROM feedback").fetchone()["c"]
        return {
            "sessions": sessions,
            "open_sessions": open_s,
            "events": events,
            "egress_audits": egress,
            "feedback": feedback,
            "active_session_id": self.get_active_session_id(),
            "db_path": str(self.db_path),
        }


def _rank(sensitivity: str) -> int:
    order = {
        Sensitivity.PUBLIC.value: 0,
        Sensitivity.INTERNAL.value: 1,
        Sensitivity.CONFIDENTIAL.value: 2,
        Sensitivity.RESTRICTED.value: 3,
    }
    return order.get(sensitivity, 1)
