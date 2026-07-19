"""Opt-in dual-agent arena: simultaneous runs, split view, human grading.

Inspired by raskinolov landing-page / game-task arenas. Trials are disposable
evaluation sessions — they never write into the durable workflow chat thread
or the master coordinator handoff path.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import Ledger
from .models import list_agent_models, model_label, normalize_agent_model
from .paths import arena_dir, arena_trials_dir
from .schema import Event, Sensitivity, Surface, new_id


GRADE_DIMS = ("helpfulness", "correctness", "research_quality", "concision")
WINNERS = frozenset({"a", "b", "tie", "neither"})
_TRIAL_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@dataclass
class ArenaLane:
    lane: str  # "a" | "b"
    model: str
    model_label: str
    thread_id: str
    job_id: Optional[str] = None
    status: str = "queued"
    output: str = ""
    error: Optional[str] = None
    estimated_tokens: Optional[int] = None
    steps: Optional[int] = None


@dataclass
class ArenaTrial:
    trial_id: str
    ritual_id: str
    request: str
    created_at: str
    source_thread_id: Optional[str] = None
    lanes: Dict[str, ArenaLane] = field(default_factory=dict)
    grade: Optional[Dict[str, Any]] = None

    def public(self) -> Dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "ritual_id": self.ritual_id,
            "request": self.request,
            "created_at": self.created_at,
            "source_thread_id": self.source_thread_id,
            "lanes": {k: asdict(v) for k, v in self.lanes.items()},
            "grade": self.grade,
            "both_done": all(
                lane.status in {"completed", "failed", "cancelled"}
                for lane in self.lanes.values()
            )
            if self.lanes
            else False,
        }


def trial_path(trial_id: str) -> Path:
    return arena_trials_dir() / trial_id / "trial.json"


def comparisons_path() -> Path:
    return arena_dir() / "comparisons.jsonl"


def _trial_from_raw(raw: Dict[str, Any]) -> ArenaTrial:
    lanes = {
        key: ArenaLane(**val)
        for key, val in (raw.get("lanes") or {}).items()
        if isinstance(val, dict)
    }
    return ArenaTrial(
        trial_id=str(raw["trial_id"]),
        ritual_id=str(raw["ritual_id"]),
        request=str(raw.get("request") or ""),
        created_at=str(raw.get("created_at") or ""),
        source_thread_id=raw.get("source_thread_id"),
        lanes=lanes,
        grade=raw.get("grade"),
    )


def save_trial(trial: ArenaTrial) -> Path:
    path = trial_path(trial.trial_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(trial.public(), indent=2, ensure_ascii=False) + "\n"
    with _TRIAL_LOCK:
        path.write_text(payload, encoding="utf-8")
    return path


def load_trial(trial_id: str) -> ArenaTrial:
    path = trial_path(trial_id)
    with _TRIAL_LOCK:
        if not path.is_file():
            raise RuntimeError(f"Arena trial '{trial_id}' not found.")
        raw = json.loads(path.read_text(encoding="utf-8"))
    return _trial_from_raw(raw)


def _normalize_score(value: Any, default: int = 3) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(5, score))


def create_trial(
    ledger: Ledger,
    *,
    ritual_id: str,
    request: str,
    model_a: str,
    model_b: str,
    source_thread_id: Optional[str] = None,
) -> ArenaTrial:
    """Create an isolated dual-lane trial with two ephemeral chat threads."""
    mid_a = normalize_agent_model(model_a)
    mid_b = normalize_agent_model(model_b)
    if not mid_a or not mid_b:
        raise ValueError("Both arena lanes need a valid agent model (Claude or Qwen).")
    if mid_a == mid_b:
        raise ValueError("Pick two different models for a meaningful comparison.")
    prompt = (request or "").strip()
    if not prompt:
        raise ValueError("Arena request text is required.")

    trial_id = new_id("arena")
    thread_a = ledger.create_arena_thread(ritual_id, trial_id, "a")
    thread_b = ledger.create_arena_thread(ritual_id, trial_id, "b")
    for thread, mid in ((thread_a, mid_a), (thread_b, mid_b)):
        ledger.append_chat_message(
            thread.session_id,
            role="user",
            content=prompt,
            kind="arena_prompt",
            metadata={"trial_id": trial_id, "model": mid},
        )

    trial = ArenaTrial(
        trial_id=trial_id,
        ritual_id=ritual_id,
        request=prompt,
        created_at=_utc_now(),
        source_thread_id=source_thread_id,
        lanes={
            "a": ArenaLane(
                lane="a",
                model=mid_a,
                model_label=model_label(mid_a),
                thread_id=thread_a.session_id,
            ),
            "b": ArenaLane(
                lane="b",
                model=mid_b,
                model_label=model_label(mid_b),
                thread_id=thread_b.session_id,
            ),
        },
    )
    save_trial(trial)
    ledger.append_event(
        Event(
            type="arena_trial_started",
            surface=Surface.CHAT.value,
            session_id=source_thread_id or thread_a.session_id,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={
                "trial_id": trial_id,
                "ritual_id": ritual_id,
                "model_a": mid_a,
                "model_b": mid_b,
                "request": prompt[:500],
            },
        )
    )
    return trial


def attach_jobs(trial: ArenaTrial, *, job_a: str, job_b: str) -> ArenaTrial:
    trial.lanes["a"].job_id = job_a
    trial.lanes["a"].status = "running"
    trial.lanes["b"].job_id = job_b
    trial.lanes["b"].status = "running"
    save_trial(trial)
    return trial


def sync_lane_from_job(
    trial: ArenaTrial,
    lane: str,
    *,
    job_status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> ArenaTrial:
    """Merge one lane's status into the on-disk trial (safe under concurrent lanes)."""
    path = trial_path(trial.trial_id)
    with _TRIAL_LOCK:
        raw = json.loads(path.read_text(encoding="utf-8"))
        lanes_raw = raw.setdefault("lanes", {})
        lane_raw = lanes_raw.setdefault(lane, {})
        if job_status in {"completed", "failed", "cancelled", "queued", "running"}:
            lane_raw["status"] = job_status
        if result:
            lane_raw["output"] = str(result.get("output") or "")
            if result.get("estimated_tokens") is not None:
                lane_raw["estimated_tokens"] = int(result["estimated_tokens"])
            if result.get("steps") is not None:
                lane_raw["steps"] = int(result["steps"])
        if error:
            lane_raw["error"] = str(error)
        path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        updated = _trial_from_raw(raw)
    trial.lanes = updated.lanes
    trial.grade = updated.grade
    return updated


def save_grade(
    ledger: Ledger,
    trial_id: str,
    *,
    winner: str,
    scores_a: Dict[str, Any],
    scores_b: Dict[str, Any],
    notes_a: str = "",
    notes_b: str = "",
    training_note: str = "",
) -> Dict[str, Any]:
    """Persist a pairwise grade (JSONL + trial file + ledger event)."""
    trial = load_trial(trial_id)
    win = str(winner or "").strip().lower()
    if win not in WINNERS:
        raise ValueError("winner must be one of: a, b, tie, neither")
    norm_a = {dim: _normalize_score(scores_a.get(dim)) for dim in GRADE_DIMS}
    norm_b = {dim: _normalize_score(scores_b.get(dim)) for dim in GRADE_DIMS}
    grade = {
        "rated_at": _utc_now(),
        "winner": win,
        "scores_a": norm_a,
        "scores_b": norm_b,
        "notes_a": str(notes_a or "")[:2000],
        "notes_b": str(notes_b or "")[:2000],
        "training_note": str(training_note or "")[:4000],
        "model_a": trial.lanes["a"].model,
        "model_b": trial.lanes["b"].model,
        "ritual_id": trial.ritual_id,
        "request": trial.request,
        "output_a": trial.lanes["a"].output,
        "output_b": trial.lanes["b"].output,
    }
    trial.grade = {
        k: grade[k]
        for k in (
            "rated_at",
            "winner",
            "scores_a",
            "scores_b",
            "notes_a",
            "notes_b",
            "training_note",
        )
    }
    save_trial(trial)
    row = {"trial_id": trial_id, **grade}
    _append_jsonl(comparisons_path(), row)
    grade_path = trial_path(trial_id).parent / "grade.json"
    grade_path.write_text(
        json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    session_id = trial.source_thread_id or trial.lanes["a"].thread_id
    ledger.append_event(
        Event(
            type="arena_grade",
            surface=Surface.CHAT.value,
            session_id=session_id,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={
                "trial_id": trial_id,
                "ritual_id": trial.ritual_id,
                "winner": win,
                "model_a": trial.lanes["a"].model,
                "model_b": trial.lanes["b"].model,
                "scores_a": norm_a,
                "scores_b": norm_b,
            },
        )
    )
    return {"status": "ok", "trial_id": trial_id, "grade": trial.grade, "path": str(grade_path)}


def available_models() -> List[Dict[str, str]]:
    return list_agent_models()
