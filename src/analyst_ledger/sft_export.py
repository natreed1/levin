"""Export (context → memo) pairs from accept/edit feedback for later SFT/DPO.

Process rewards only: labels come from analyst accept/reject/edit, not market PnL.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import Ledger
from .paths import sft_export_dir, sqlite_path
from .redact import build_synthesis_prompt
from .schema import Sensitivity


def _load_feedback_rows(db_path: Path) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT * FROM feedback
        WHERE label IN ('accept', 'edit')
        ORDER BY ts ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _synthesis_output(ledger: Ledger, synthesis_event_id: Optional[str], session_id: Optional[str]) -> Optional[str]:
    if synthesis_event_id:
        events = ledger.list_events(session_id=session_id, limit=500, types=["synthesis_result"])
        for ev in events:
            if ev["event_id"] == synthesis_event_id:
                return (ev.get("payload") or {}).get("output")
    # fallback: latest successful synthesis for session
    if session_id:
        events = ledger.list_events(session_id=session_id, limit=50, types=["synthesis_result"])
        for ev in events:
            out = (ev.get("payload") or {}).get("output")
            if out:
                return out
    return None


def build_pairs(ledger: Optional[Ledger] = None) -> List[Dict[str, Any]]:
    ledger = ledger or Ledger()
    pairs: List[Dict[str, Any]] = []
    for row in _load_feedback_rows(Path(ledger.db_path)):
        sid = row.get("session_id")
        if not sid:
            continue
        try:
            context = ledger.session_context_for_synthesis(
                sid, max_sensitivity=Sensitivity.INTERNAL
            )
        except RuntimeError:
            continue
        prompt = build_synthesis_prompt(
            context,
            "Draft a research memo and next-checks list from this session.",
        )
        if row["label"] == "edit" and row.get("edited_output"):
            completion = row["edited_output"]
            reward_signal = "process_edit"
        else:
            completion = _synthesis_output(ledger, row.get("synthesis_event_id"), sid)
            reward_signal = "process_accept"
        if not completion:
            continue
        pairs.append(
            {
                "messages": [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": completion},
                ],
                "meta": {
                    "session_id": sid,
                    "feedback_id": row["feedback_id"],
                    "label": row["label"],
                    "reward_family": "analyst_process",
                    "reward_signal": reward_signal,
                    "notes": row.get("notes") or "",
                },
            }
        )
    return pairs


def export_pairs(out_path: Optional[Path] = None) -> Path:
    pairs = build_pairs()
    dest = out_path or (sft_export_dir() / "context_memo_pairs.jsonl")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
    # Also write a reject-preference side file for future DPO (process rewards)
    rejects_path = dest.with_name("dpo_reject_stubs.jsonl")
    ledger = Ledger()
    conn = sqlite3.connect(str(sqlite_path()))
    conn.row_factory = sqlite3.Row
    rejects = conn.execute(
        "SELECT * FROM feedback WHERE label = 'reject' ORDER BY ts ASC"
    ).fetchall()
    conn.close()
    with rejects_path.open("w", encoding="utf-8") as fh:
        for row in rejects:
            fh.write(
                json.dumps(
                    {
                        "session_id": row["session_id"],
                        "feedback_id": row["feedback_id"],
                        "reward_family": "analyst_process",
                        "reward_signal": "process_reject",
                        "notes": row["notes"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return dest
