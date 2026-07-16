"""Event / session / artifact schema with sensitivity labels."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class Surface(str, Enum):
    CURSOR = "cursor"
    TRADINGVIEW = "tradingview"
    NOTES = "notes"
    INBOX = "inbox"
    BROWSER = "browser"
    OBSIDIAN = "obsidian"
    APPLE_NOTES = "apple_notes"
    GDOCS = "gdocs"
    SYSTEM = "system"
    SYNTHESIS = "synthesis"
    RITUAL = "ritual"


SENSITIVITY_LEVELS = {s.value: s for s in Sensitivity}
SURFACES = {s.value: s for s in Surface}

# Ordered: higher index = more sensitive
_SENSITIVITY_RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.CONFIDENTIAL: 2,
    Sensitivity.RESTRICTED: 3,
}

# Event types used across collectors
EVENT_TYPES = frozenset(
    {
        "session_start",
        "session_end",
        "note",
        "symbol_focus",
        "interval_change",
        "drawing_meta",
        "artifact_attach",
        "file_edit",
        "shell",
        "agent_stop",
        "inbox_file",
        "tag",
        "synthesis_request",
        "synthesis_result",
        "egress_audit",
        "feedback",
        "heartbeat",
        "url_focus",
        "ritual_candidate",
        "ritual_suggest",
        "ritual_run",
        "ritual_build",
        "ritual_integrate",
        "external_note",
    }
)

SESSION_TAGS = frozenset({"idea", "reject", "followup", "neutral"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def parse_sensitivity(value: Optional[str], default: Sensitivity = Sensitivity.INTERNAL) -> Sensitivity:
    if value is None or value == "":
        return default
    key = value.strip().lower()
    if key not in SENSITIVITY_LEVELS:
        raise ValueError(f"Unknown sensitivity '{value}'. Expected one of: {', '.join(SENSITIVITY_LEVELS)}")
    return SENSITIVITY_LEVELS[key]


def parse_surface(value: Optional[str], default: Surface = Surface.NOTES) -> Surface:
    if value is None or value == "":
        return default
    key = value.strip().lower()
    if key not in SURFACES:
        raise ValueError(f"Unknown surface '{value}'. Expected one of: {', '.join(SURFACES)}")
    return SURFACES[key]


def sensitivity_allows_egress(level: Sensitivity, max_egress: Sensitivity = Sensitivity.INTERNAL) -> bool:
    """Restricted never egresses. Otherwise compare rank to max allowed."""
    if level == Sensitivity.RESTRICTED:
        return False
    return _SENSITIVITY_RANK[level] <= _SENSITIVITY_RANK[max_egress]


@dataclass
class Event:
    """Single append-only ledger row."""

    type: str
    surface: str
    session_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    sensitivity: str = Sensitivity.INTERNAL.value
    event_id: str = field(default_factory=lambda: new_id("evt"))
    ts: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type '{self.type}'")
        parse_sensitivity(self.sensitivity)
        parse_surface(self.surface)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        return cls(
            type=data["type"],
            surface=data["surface"],
            session_id=data.get("session_id"),
            payload=data.get("payload") or {},
            sensitivity=data.get("sensitivity", Sensitivity.INTERNAL.value),
            event_id=data.get("event_id") or new_id("evt"),
            ts=data.get("ts") or utc_now_iso(),
        )


@dataclass
class Session:
    session_id: str
    title: str
    surface: str
    sensitivity: str = Sensitivity.INTERNAL.value
    desk_tag: Optional[str] = None
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    status: str = "open"  # open | closed

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactRef:
    """Pointer to a file on disk; content stays local unless opted in."""

    path: str
    sha256: str
    mime: str
    artifact_id: str = field(default_factory=lambda: new_id("art"))
    size_bytes: int = 0
    sensitivity: str = Sensitivity.INTERNAL.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
