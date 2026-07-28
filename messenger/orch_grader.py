"""Deterministic process grades for Orchestrator runs (not market truth)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class ProcessGrade:
    path: str
    rounds: int = 0
    dry: bool = False
    questions_opened: int = 0
    questions_resolved: int = 0
    fact_supported: int = 0
    fact_contradicted: int = 0
    fact_insufficient: int = 0
    stages: List[str] = field(default_factory=list)
    score: float = 0.0
    notes: List[str] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "rounds": self.rounds,
            "dry": self.dry,
            "questions_opened": self.questions_opened,
            "questions_resolved": self.questions_resolved,
            "fact_supported": self.fact_supported,
            "fact_contradicted": self.fact_contradicted,
            "fact_insufficient": self.fact_insufficient,
            "stages": list(self.stages),
            "score": round(self.score, 2),
            "notes": list(self.notes),
            "letter": self.letter(),
        }

    def letter(self) -> str:
        s = self.score
        if s >= 0.85:
            return "A"
        if s >= 0.7:
            return "B"
        if s >= 0.55:
            return "C"
        if s >= 0.4:
            return "D"
        return "F"


def grade_run(
    *,
    path: str,
    rounds: int = 0,
    dry: bool = False,
    questions_opened: int = 0,
    questions_resolved: int = 0,
    fact_checks: Optional[Sequence[Dict[str, Any]]] = None,
    stages: Optional[Sequence[str]] = None,
) -> ProcessGrade:
    checks = list(fact_checks or [])
    supported = sum(1 for c in checks if str(c.get("verdict") or "") == "supported")
    contradicted = sum(
        1 for c in checks if str(c.get("verdict") or "") == "contradicted"
    )
    insufficient = sum(
        1
        for c in checks
        if str(c.get("verdict") or "") in {"insufficient", "unavailable"}
    )
    total_fc = max(1, supported + contradicted + insufficient) if checks else 0

    notes: List[str] = []
    score = 0.5
    if path == "orchestrator":
        score += 0.05
    if dry:
        score += 0.15
        notes.append("loop dry")
    elif rounds > 0:
        score += min(0.1, 0.03 * rounds)
        notes.append(f"{rounds} research rounds")
    if questions_opened:
        resolved_rate = questions_resolved / max(1, questions_opened)
        score += 0.15 * resolved_rate
        notes.append(
            f"questions {questions_resolved}/{questions_opened} resolved"
        )
    if checks:
        contradict_rate = contradicted / total_fc
        support_rate = supported / total_fc
        score += 0.2 * support_rate
        score -= 0.25 * contradict_rate
        notes.append(
            f"fact-check supported={supported} contradicted={contradicted} "
            f"insufficient={insufficient}"
        )
        if contradicted:
            notes.append("contradictions caught before critique trusted claims")
    score = max(0.0, min(1.0, score))

    return ProcessGrade(
        path=path,
        rounds=rounds,
        dry=dry,
        questions_opened=questions_opened,
        questions_resolved=questions_resolved,
        fact_supported=supported,
        fact_contradicted=contradicted,
        fact_insufficient=insufficient,
        stages=list(stages or []),
        score=score,
        notes=notes,
    )


def format_grade_chip(grade: ProcessGrade) -> str:
    return (
        f"Process {grade.letter()} ({grade.score:.0%}) · path={grade.path} · "
        f"FC +{grade.fact_supported}/−{grade.fact_contradicted}"
    )
