"""Export human-confirmed (message -> kind) pairs for training a classifier.

The correction loop's payoff: every time you fix an auto-tag, that becomes a
labeled example (recorded via ``ledger.correct_message_kind``). This writes them
out as JSONL so a small ``kind`` classifier can be fine-tuned on your own
taxonomy later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import Ledger
from .paths import data_dir


def build_kind_pairs(ledger: Ledger, *, limit: int = 10000) -> List[Dict[str, Any]]:
    """Confirmed (text -> kind) examples, newest first."""
    return ledger.confirmed_kind_examples(limit=limit)


def export_kind_pairs(
    ledger: Optional[Ledger] = None, out_path: Optional[Path] = None
) -> Path:
    """Write confirmed (message -> kind) pairs to JSONL; return the path."""
    ledger = ledger or Ledger()
    pairs = build_kind_pairs(ledger)
    out_path = Path(out_path) if out_path else (data_dir() / "classify_pairs.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(
                json.dumps(
                    {
                        "text": pair["text"],
                        "kind": pair["kind"],
                        "meta": {"task": "message_kind"},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return out_path
