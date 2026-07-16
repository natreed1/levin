"""Shared helpers for ingesting external notes (Obsidian, Apple Notes, GDocs)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from .ledger import Ledger
from .paths import sync_state_dir
from .schema import Event, Sensitivity, Surface, parse_sensitivity

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
LEDGER_TAG_RE = re.compile(r"(?:^|\s)#ledger(?:\b|/)", re.I)


def load_seen(name: str) -> Set[str]:
    path = sync_state_dir() / f"{name}_seen.json"
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def save_seen(name: str, seen: Set[str]) -> None:
    path = sync_state_dir() / f"{name}_seen.json"
    path.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def file_fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{path.resolve()}:{st.st_mtime_ns}:{st.st_size}"


def content_fingerprint(text: str, source_id: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{source_id}:{digest}"


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return (meta, body). Meta keys are lowercased strings."""
    m = FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if not m:
        return {}, text
    raw_meta, body = m.group(1), m.group(2)
    meta: Dict[str, Any] = {}
    for line in raw_meta.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip().strip("\"'")
        if val.lower() in {"true", "yes", "on"}:
            meta[key] = True
        elif val.lower() in {"false", "no", "off"}:
            meta[key] = False
        else:
            meta[key] = val
    return meta, body


def wants_ledger(meta: Dict[str, Any], body: str, require_opt_in: bool = True) -> bool:
    if not require_opt_in:
        return True
    if meta.get("ledger") is True:
        return True
    if str(meta.get("ledger", "")).lower() in {"true", "1", "yes"}:
        return True
    if meta.get("ledger_session") or meta.get("session_id"):
        return True
    if LEDGER_TAG_RE.search(body) or LEDGER_TAG_RE.search(
        " ".join(str(v) for v in meta.values())
    ):
        return True
    # tags: [ledger] style simple
    tags = str(meta.get("tags", ""))
    if "ledger" in tags.lower():
        return True
    return False


def extract_docx_text(path: Path) -> str:
    """Best-effort plain text from a .docx using stdlib only."""
    import zipfile
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    parts = []
    for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
        if node.text:
            parts.append(node.text)
        if node.tail:
            parts.append(node.tail)
    # Prefer paragraph breaks
    paras = []
    for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        texts = [
            t.text or ""
            for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        ]
        line = "".join(texts).strip()
        if line:
            paras.append(line)
    return "\n\n".join(paras) if paras else " ".join(parts)


def read_note_file(path: Path) -> Tuple[str, str]:
    """Return (title_guess, text) for md/txt/docx."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = extract_docx_text(path)
    elif suffix in {".md", ".markdown", ".txt", ".text"}:
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        raise ValueError(f"unsupported note type: {suffix}")
    title = path.stem
    return title, text


def ingest_note_text(
    ledger: Ledger,
    *,
    title: str,
    text: str,
    surface: str,
    source: str,
    source_path: Optional[str] = None,
    source_id: Optional[str] = None,
    require_opt_in: bool = False,
    attach_file: Optional[Path] = None,
    max_note_chars: int = 8000,
) -> Optional[Dict[str, Any]]:
    """
    Ingest external note text into the ledger.

    Returns a summary dict, or None if skipped (opt-in failed).
    """
    meta, body = parse_frontmatter(text)
    if require_opt_in and not wants_ledger(meta, body):
        return None

    sensitivity = str(meta.get("sensitivity") or Sensitivity.INTERNAL.value)
    parse_sensitivity(sensitivity)

    sid = meta.get("ledger_session") or meta.get("session_id") or ledger.get_active_session_id()
    if not sid:
        session = ledger.start_session(
            title=str(meta.get("title") or title)[:120],
            surface=surface,
            sensitivity=sensitivity,
            desk_tag=str(meta.get("desk_tag")) if meta.get("desk_tag") else None,
        )
        sid = session.session_id

    note_body = body.strip() or text.strip()
    if len(note_body) > max_note_chars:
        note_body = note_body[:max_note_chars] + "\n\n…[truncated]"

    # Prefer structured note event for synthesis/mining
    note_event = ledger.add_note(
        note_body,
        session_id=sid,
        sensitivity=sensitivity,
        surface=surface,
    )
    # Also stamp provenance
    ext = ledger.append_event(
        Event(
            type="external_note",
            surface=surface,
            session_id=sid,
            sensitivity=sensitivity,
            payload={
                "source": source,
                "title": title,
                "source_path": source_path,
                "source_id": source_id,
                "symbol": meta.get("symbol"),
                "meta": {k: meta[k] for k in meta if k not in {"ledger"}},
                "note_event_id": note_event.event_id,
                "chars": len(note_body),
            },
        )
    )

    artifact_id = None
    if attach_file and attach_file.is_file():
        art = ledger.attach_artifact(
            attach_file,
            session_id=sid,
            sensitivity=sensitivity,
            copy_into_store=True,
        )
        artifact_id = art.artifact_id

    return {
        "session_id": sid,
        "note_event_id": note_event.event_id,
        "external_event_id": ext.event_id,
        "artifact_id": artifact_id,
        "sensitivity": sensitivity,
        "title": title,
        "source": source,
    }
