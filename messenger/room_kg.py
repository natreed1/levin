"""Per-room knowledge graph — structured durable memory (not the transcript).

Nodes: Entity, Claim, Source, Question, Decision, Task, Agent.
Writes of factual claims prefer fact-checker outcomes (supported → verified).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_CLAIM_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass
class KGClaim:
    id: str
    text: str
    status: str = "unverified"  # verified | disputed | withdrawn | insufficient | unverified
    sources: List[str] = field(default_factory=list)
    agent_id: str = ""
    updated_at: float = 0.0


@dataclass
class KGQuestion:
    id: str
    text: str
    status: str = "open"  # open | resolved | unanswerable
    from_agent: str = ""
    updated_at: float = 0.0


@dataclass
class RoomKG:
    room_id: str
    entities: List[Dict[str, Any]] = field(default_factory=list)
    claims: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    questions: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    agents: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _kg_path(room_id: str) -> Path:
    from analyst_ledger.paths import data_dir

    safe = _CLAIM_ID_RE.sub("_", (room_id or "room").strip())[:80] or "room"
    root = data_dir() / "room_kg"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{safe}.json"


def load_kg(room_id: str) -> RoomKG:
    path = _kg_path(room_id)
    if not path.exists():
        return RoomKG(room_id=room_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RoomKG(room_id=room_id)
    if not isinstance(raw, dict):
        return RoomKG(room_id=room_id)
    return RoomKG(
        room_id=str(raw.get("room_id") or room_id),
        entities=list(raw.get("entities") or []),
        claims=list(raw.get("claims") or []),
        sources=list(raw.get("sources") or []),
        questions=list(raw.get("questions") or []),
        decisions=list(raw.get("decisions") or []),
        tasks=list(raw.get("tasks") or []),
        agents=list(raw.get("agents") or []),
        updated_at=float(raw.get("updated_at") or 0),
    )


def save_kg(kg: RoomKG) -> None:
    kg.updated_at = time.time()
    path = _kg_path(kg.room_id)
    path.write_text(json.dumps(kg.to_dict(), indent=2), encoding="utf-8")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _dedupe_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold().strip())[:240]


def upsert_claim(
    kg: RoomKG,
    text: str,
    *,
    status: str = "unverified",
    sources: Optional[Sequence[str]] = None,
    agent_id: str = "",
) -> Dict[str, Any]:
    key = _dedupe_key(text)
    if not key:
        return {}
    now = time.time()
    for row in kg.claims:
        if _dedupe_key(str(row.get("text") or "")) == key:
            row["status"] = status
            row["sources"] = list(sources or row.get("sources") or [])[:8]
            if agent_id:
                row["agent_id"] = agent_id
            row["updated_at"] = now
            return row
    row = {
        "id": _new_id("claim"),
        "text": (text or "").strip()[:500],
        "status": status,
        "sources": list(sources or [])[:8],
        "agent_id": agent_id,
        "updated_at": now,
    }
    kg.claims.append(row)
    # Soft cap
    if len(kg.claims) > 80:
        kg.claims = kg.claims[-80:]
    return row


def upsert_question(
    kg: RoomKG,
    text: str,
    *,
    status: str = "open",
    from_agent: str = "",
) -> Dict[str, Any]:
    key = _dedupe_key(text)
    if not key:
        return {}
    now = time.time()
    for row in kg.questions:
        if _dedupe_key(str(row.get("text") or "")) == key:
            row["status"] = status
            if from_agent:
                row["from_agent"] = from_agent
            row["updated_at"] = now
            return row
    row = {
        "id": _new_id("q"),
        "text": (text or "").strip()[:400],
        "status": status,
        "from_agent": from_agent,
        "updated_at": now,
    }
    kg.questions.append(row)
    if len(kg.questions) > 40:
        kg.questions = kg.questions[-40:]
    return row


def add_source(kg: RoomKG, url: str, *, title: str = "") -> Dict[str, Any]:
    u = (url or "").strip()
    if not u:
        return {}
    for row in kg.sources:
        if str(row.get("url") or "") == u:
            return row
    row = {"id": _new_id("src"), "url": u[:500], "title": (title or "")[:200]}
    kg.sources.append(row)
    if len(kg.sources) > 60:
        kg.sources = kg.sources[-60:]
    return row


def apply_fact_check(
    kg: RoomKG,
    results: Sequence[Dict[str, Any]],
    *,
    agent_id: str = "fact_checker",
) -> int:
    """Gate claim writes from fact-checker outcomes. Returns writes count."""
    n = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or item.get("text") or "").strip()
        if not claim:
            continue
        verdict = str(item.get("verdict") or item.get("status") or "insufficient").casefold()
        sources = [str(s) for s in (item.get("sources") or []) if str(s).strip()][:6]
        for s in sources:
            add_source(kg, s)
        if verdict == "supported":
            status = "verified"
        elif verdict == "contradicted":
            status = "disputed"
        elif verdict in {"insufficient", "unavailable"}:
            status = "insufficient"
        else:
            status = "unverified"
        upsert_claim(kg, claim, status=status, sources=sources, agent_id=agent_id)
        n += 1
    return n


def compress_snapshot(kg: RoomKG, *, max_chars: int = 1800) -> str:
    """Prompt-sized snapshot: open Qs, decisions, latest verified, decay stale."""
    now = time.time()
    stale_after = 14 * 86400
    lines: List[str] = ["[Room knowledge graph]"]

    open_qs = [
        q
        for q in kg.questions
        if str(q.get("status") or "") == "open"
        and (now - float(q.get("updated_at") or 0)) < stale_after
    ][:6]
    if open_qs:
        lines.append("Open questions:")
        for q in open_qs:
            lines.append(f"- {q.get('text')}")

    verified = [
        c
        for c in kg.claims
        if str(c.get("status") or "") == "verified"
        and (now - float(c.get("updated_at") or 0)) < stale_after
    ][-8:]
    if verified:
        lines.append("Verified claims:")
        for c in verified:
            lines.append(f"- {c.get('text')}")

    disputed = [
        c for c in kg.claims if str(c.get("status") or "") == "disputed"
    ][-4:]
    if disputed:
        lines.append("Disputed:")
        for c in disputed:
            lines.append(f"- {c.get('text')}")

    if kg.decisions:
        lines.append("Decisions:")
        for d in kg.decisions[-4:]:
            lines.append(f"- {d.get('text') or d}")

    blob = "\n".join(lines)
    if len(blob) > max_chars:
        return blob[: max_chars - 20] + "\n…(truncated)"
    return blob
