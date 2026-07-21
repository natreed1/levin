"""Deterministic label proposer — suggests ``topic:`` labels from text.

Pure function, no model calls or network. It only *proposes*; nothing here
writes to the ledger or applies a label. The chat/dashboard layer surfaces the
proposals for the human to confirm (auto-suggest + confirm, per the design).

Scope kept deliberately tight for high precision:
- topics from a keyword map (only values already in ``labels.TOPICS``)
- topics from explicit tickers via a small sector map
Entity extraction is the actionable detector's job (see ``actionable.py``).
"""

from __future__ import annotations

import re
from typing import Dict, List, Set

from .labels import TOPICS, normalize_label
from .web_search import extract_tickers

# Trigger phrases -> controlled topic slug. Values MUST be in labels.TOPICS.
TOPIC_KEYWORDS: Dict[str, Set[str]] = {
    "semiconductors": {
        "semiconductor", "semiconductors", "chip", "chips", "gpu", "gpus",
        "foundry", "fab", "wafer",
    },
    "ai-models": {"llm", "llms", "transformer", "inference", "fine-tune", "fine-tuning"},
    "ai-capex": {"data center", "datacenter", "capex", "hyperscaler", "hyperscalers"},
    "ai-startups": {"startup", "startups", "seed round", "series a", "series b", "y combinator"},
    "cloud": {"aws", "azure", "gcp", "saas"},
    "cybersecurity": {"cybersecurity", "ransomware", "data breach", "vulnerability"},
    "rate-cuts": {"rate cut", "rate cuts", "fomc", "interest rate", "interest rates", "powell"},
    "inflation": {"inflation", "cpi", "ppi", "pce"},
    "earnings": {"earnings", "guidance", "10-q", "10-k"},
    "energy": {"crude oil", "opec", "natural gas"},
    "crypto": {"crypto", "bitcoin", "ethereum", "blockchain"},
    "biotech": {"biotech", "fda approval", "clinical trial"},
    "regulation": {"antitrust", "regulation", "ftc", "doj"},
    "m-and-a": {"merger", "acquisition", "buyout", "takeover"},
    "ipos": {"ipo", "ipos", "s-1", "direct listing"},
    "consumer": {"consumer spending", "retail sales"},
    "macro": {"gdp", "recession", "jobs report", "unemployment"},
}

# Small, explicit ticker -> sector topic map (extend as needed).
TICKER_TOPICS: Dict[str, str] = {
    "NVDA": "semiconductors",
    "AMD": "semiconductors",
    "INTC": "semiconductors",
    "AVGO": "semiconductors",
    "MU": "semiconductors",
    "TSM": "semiconductors",
    "ASML": "semiconductors",
    "MSFT": "cloud",
    "AMZN": "cloud",
    "GOOGL": "cloud",
    "COIN": "crypto",
}


def _matches(keyword: str, low: str) -> bool:
    """Word-boundary match for single words, substring for multi-word phrases."""
    if " " in keyword or "-" in keyword:
        return keyword in low
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", low) is not None


def propose(text: str) -> List[Dict[str, str]]:
    """Return proposed labels with a short reason, sorted and de-duplicated."""
    low = (text or "").casefold()
    found: Dict[str, str] = {}
    for topic, keywords in TOPIC_KEYWORDS.items():
        if topic not in TOPICS:
            continue
        hit = next((kw for kw in sorted(keywords) if _matches(kw, low)), None)
        if hit:
            found.setdefault(normalize_label(f"topic:{topic}"), f"keyword '{hit}'")
    for ticker in extract_tickers(text or "", limit=3):
        topic = TICKER_TOPICS.get(ticker)
        if topic and topic in TOPICS:
            found.setdefault(normalize_label(f"topic:{topic}"), f"ticker {ticker}")
    return [{"label": label, "reason": reason} for label, reason in sorted(found.items())]


def suggest_labels(text: str) -> List[str]:
    """Just the proposed label strings (normalized, sorted)."""
    return [item["label"] for item in propose(text)]
