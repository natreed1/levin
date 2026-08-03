"""Durable per-room file library, output grading, and prompt context."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CONTEXT_CHARS = 24_000
VALID_GRADES = frozenset({1, 2, 3, 4, 5})
_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(value: str, fallback: str) -> str:
    return _SAFE_RE.sub("_", str(value or "").strip())[:100] or fallback


def _root() -> Path:
    raw = os.environ.get("MESSENGER_DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / "data"
    root = base / "room_files"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _room_dir(room_id: str) -> Path:
    path = _root() / _safe(room_id, "room")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(room_id: str) -> Path:
    return _room_dir(room_id) / "manifest.json"


def _load(room_id: str) -> list[dict[str, Any]]:
    path = _manifest_path(room_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [dict(row) for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _save(room_id: str, rows: list[dict[str, Any]]) -> None:
    path = _manifest_path(room_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "stored_name"}


def list_files(room_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        rows = _load(room_id)
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return [_public(row) for row in rows]


def get_file(room_id: str, file_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        row = next((row for row in _load(room_id) if row.get("id") == file_id), None)
    return dict(row) if row else None


def content_path(room_id: str, row: dict[str, Any]) -> Path:
    return _room_dir(room_id) / _safe(str(row.get("stored_name") or ""), "missing")


def read_content(room_id: str, row: dict[str, Any]) -> bytes:
    try:
        return content_path(room_id, row).read_bytes()
    except OSError:
        return b""


def text_content(room_id: str, row: dict[str, Any]) -> str:
    data = read_content(room_id, row)
    mime = str(row.get("mime") or "").casefold()
    name = str(row.get("name") or "").casefold()
    if not (
        mime.startswith("text/")
        or mime in {"application/json", "application/xml", "application/javascript"}
        or name.endswith((".md", ".txt", ".csv", ".json", ".xml", ".yaml", ".yml"))
    ):
        return ""
    return data.decode("utf-8", errors="replace")


def add_file(
    room_id: str,
    *,
    name: str,
    content: bytes,
    title: str = "",
    kind: str = "upload",
    mime: str = "application/octet-stream",
    source: str = "upload",
    tags: Optional[list[str]] = None,
    notes: str = "",
    use_for_context: bool = True,
    created_by: str = "",
) -> dict[str, Any]:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("file_too_large")
    file_id = f"file_{uuid.uuid4().hex}"
    suffix = Path(name or "").suffix[:20]
    stored_name = f"{_safe(file_id, 'file')}{suffix}"
    now = _now()
    row: dict[str, Any] = {
        "id": file_id,
        "name": str(name or "document")[:240],
        "title": str(title or name or "Document").strip()[:200],
        "kind": str(kind or "upload")[:40],
        "mime": str(mime or "application/octet-stream")[:120],
        "source": str(source or "upload")[:80],
        "tags": [str(tag).strip()[:50] for tag in (tags or []) if str(tag).strip()][:12],
        "notes": str(notes or "").strip()[:800],
        "use_for_context": bool(use_for_context),
        "size": len(content),
        "created_by": str(created_by or "")[:120],
        "created_at": now,
        "updated_at": now,
        "grade": None,
        "grade_notes": "",
        "graded_by": "",
        "graded_at": None,
        "stored_name": stored_name,
    }
    with _LOCK:
        content_path(room_id, row).write_bytes(content)
        rows = _load(room_id)
        rows.append(row)
        _save(room_id, rows)
    return _public(row)


def save_report(
    room_id: str,
    *,
    title: str,
    body: str,
    source: str,
    created_by: str = "",
) -> dict[str, Any]:
    return add_file(
        room_id,
        name=f"{_safe(title, 'report')}.md",
        content=str(body or "").encode("utf-8"),
        title=title,
        kind="report",
        mime="text/markdown",
        source=source,
        tags=["report", source],
        use_for_context=True,
        created_by=created_by,
    )


def update_file(room_id: str, file_id: str, changes: dict[str, Any], *, grader: str = "") -> Optional[dict[str, Any]]:
    allowed = {"use_for_context", "grade", "grade_notes"}
    supplied = allowed.intersection(changes)
    if not supplied:
        return get_file(room_id, file_id)
    with _LOCK:
        rows = _load(room_id)
        row = next((item for item in rows if item.get("id") == file_id), None)
        if row is None:
            return None
        if "use_for_context" in supplied:
            row["use_for_context"] = bool(changes["use_for_context"])
        if "grade" in supplied:
            raw_grade = changes["grade"]
            grade = None if raw_grade in {None, ""} else int(raw_grade)
            if grade is not None and grade not in VALID_GRADES:
                raise ValueError("invalid_grade")
            row["grade"] = grade
            row["graded_by"] = str(grader or "")[:120] if grade is not None else ""
            row["graded_at"] = _now() if grade is not None else None
        if "grade_notes" in supplied:
            row["grade_notes"] = str(changes["grade_notes"] or "").strip()[:2000]
            row["graded_by"] = str(grader or "")[:120]
            row["graded_at"] = _now()
        row["updated_at"] = _now()
        _save(room_id, rows)
        return _public(row)


def delete_file(room_id: str, file_id: str) -> bool:
    with _LOCK:
        rows = _load(room_id)
        row = next((item for item in rows if item.get("id") == file_id), None)
        if row is None:
            return False
        try:
            content_path(room_id, row).unlink(missing_ok=True)
        except OSError:
            pass
        _save(room_id, [item for item in rows if item.get("id") != file_id])
        return True


def context_for_room(room_id: str, *, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Return opted-in documents, including human grades and reviewer notes."""
    blocks: list[str] = []
    used = 0
    for row in list_files(room_id):
        if not row.get("use_for_context"):
            continue
        full = get_file(room_id, str(row.get("id") or ""))
        if not full:
            continue
        text = text_content(room_id, full).strip()
        notes = str(row.get("notes") or "").strip()
        grade_notes = str(row.get("grade_notes") or "").strip()
        grade = row.get("grade")
        header = f"[Team file: {row.get('title') or row.get('name')}]"
        feedback: list[str] = []
        if grade is not None:
            feedback.append(f"Human grade: {grade}/5")
        if grade_notes:
            feedback.append(f"Human reviewer context: {grade_notes}")
        if notes:
            feedback.append(f"File notes: {notes}")
        block = "\n".join([header, *feedback, text]).strip()
        if not block:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        blocks.append(block[:remaining])
        used += min(len(block), remaining)
    return "\n\n".join(blocks)
