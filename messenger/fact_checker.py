"""Frontier fact-checker with web search (replaces regex claim gates).

Per claim: supported | contradicted | insufficient, with sources.
Stub mode is deterministic for tests (no network / no model).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("messenger.fact_checker")

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?%?\b")


@dataclass
class ClaimCheck:
    claim: str
    verdict: str  # supported | contradicted | insufficient | unavailable
    sources: List[str] = field(default_factory=list)
    note: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "claim": self.claim,
            "verdict": self.verdict,
            "sources": list(self.sources),
            "note": self.note,
        }


def extract_claims(text: str, *, limit: int = 6) -> List[str]:
    """Heuristic claim split — material sentences / number-bearing lines."""
    raw = (text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in _SENTENCE_RE.split(raw) if p.strip()]
    claims: List[str] = []
    for p in parts:
        if len(p) < 24:
            continue
        # Prefer factual-looking lines
        if _NUMBER_RE.search(p) or any(
            k in p.casefold()
            for k in (
                "filed",
                "filing",
                "reported",
                "according",
                "sec",
                "revenue",
                "announced",
                "said",
            )
        ):
            claims.append(p[:400])
        elif len(claims) < 2:
            claims.append(p[:400])
        if len(claims) >= limit:
            break
    if not claims and parts:
        claims = [parts[0][:400]]
    return claims[:limit]


def _stub_check(claim: str, evidence: str) -> ClaimCheck:
    low = claim.casefold()
    ev = (evidence or "").casefold()
    # Contradict obvious invent markers when evidence lacks them
    if "invented-claim-marker" in low:
        return ClaimCheck(
            claim=claim,
            verdict="contradicted",
            sources=[],
            note="stub: invented marker",
        )
    nums = _NUMBER_RE.findall(claim)
    if nums and evidence:
        if any(n.casefold() in ev for n in nums):
            return ClaimCheck(
                claim=claim,
                verdict="supported",
                sources=["stub:evidence"],
                note="stub: number present in evidence",
            )
        return ClaimCheck(
            claim=claim,
            verdict="insufficient",
            sources=[],
            note="stub: number not in evidence",
        )
    if evidence and any(tok in ev for tok in low.split() if len(tok) > 5):
        return ClaimCheck(
            claim=claim,
            verdict="supported",
            sources=["stub:evidence"],
            note="stub: lexical overlap",
        )
    return ClaimCheck(
        claim=claim,
        verdict="insufficient",
        sources=[],
        note="stub: no support",
    )


def _web_hits(query: str, *, limit: int = 4) -> List[Dict[str, Any]]:
    try:
        from analyst_ledger.web_search import bing_search

        return bing_search(query, limit=limit) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("web search failed: %s", exc)
        return []


def _model_verdicts(
    claims: Sequence[str],
    *,
    evidence: str,
    hits_blob: str,
    endpoint: Any,
) -> Optional[List[Dict[str, Any]]]:
    if endpoint is None:
        return None
    try:
        from analyst_ledger.synthesize import call_chat_messages, use_llm_endpoint
    except Exception:
        return None
    prompt = (
        "You are a fact-checker. For each claim, return JSON array of objects with "
        'keys claim, verdict (supported|contradicted|insufficient), sources (urls), note.\n'
        "Use only the evidence and search hits. Never invent sources.\n\n"
        f"Evidence:\n{evidence[:3500]}\n\n"
        f"Search hits:\n{hits_blob[:2500]}\n\n"
        f"Claims:\n{json.dumps(list(claims), ensure_ascii=False)}"
    )
    try:
        with use_llm_endpoint(endpoint):
            raw = call_chat_messages(
                [{"role": "user", "content": prompt}],
                max_tokens=900,
                system="Return JSON only. No markdown fences.",
                temperature=0.1,
            ).strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("fact-checker model failed: %s", exc)
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, list):
        return None
    return [x for x in data if isinstance(x, dict)]


def check_claims(
    text_or_claims: Any,
    *,
    evidence: str = "",
    endpoint: Any = None,
    stub: bool = False,
    allow_web: bool = True,
) -> List[ClaimCheck]:
    """Fact-check claims with optional frontier model + web search."""
    if isinstance(text_or_claims, str):
        claims = extract_claims(text_or_claims)
    else:
        claims = [str(c).strip() for c in (text_or_claims or []) if str(c).strip()][:8]
    if not claims:
        return []

    if stub or endpoint is None:
        return [_stub_check(c, evidence) for c in claims]

    hits: List[Dict[str, Any]] = []
    if allow_web:
        for c in claims[:3]:
            hits.extend(_web_hits(c[:120], limit=3))
    hits_blob = "\n".join(
        f"- {h.get('title') or ''} | {h.get('url') or ''} | {h.get('snippet') or ''}"
        for h in hits[:10]
    )
    modeled = _model_verdicts(
        claims, evidence=evidence, hits_blob=hits_blob, endpoint=endpoint
    )
    if not modeled:
        # Visible degrade — insufficient, not invent
        return [
            ClaimCheck(
                claim=c,
                verdict="unavailable",
                sources=[str(h.get("url") or "") for h in hits[:2] if h.get("url")],
                note="fact-checker unavailable; do not treat as verified",
            )
            for c in claims
        ]

    by_claim = {_dedupe(str(m.get("claim") or "")): m for m in modeled}
    out: List[ClaimCheck] = []
    for c in claims:
        m = by_claim.get(_dedupe(c)) or next(
            (x for x in modeled if _overlap(c, str(x.get("claim") or ""))),
            None,
        )
        if not m:
            out.append(
                ClaimCheck(claim=c, verdict="insufficient", note="no model row")
            )
            continue
        verdict = str(m.get("verdict") or "insufficient").casefold()
        if verdict not in {"supported", "contradicted", "insufficient", "unavailable"}:
            verdict = "insufficient"
        sources = [str(s) for s in (m.get("sources") or []) if str(s).strip()][:6]
        if not sources:
            sources = [str(h.get("url") or "") for h in hits[:2] if h.get("url")]
        out.append(
            ClaimCheck(
                claim=c,
                verdict=verdict,
                sources=sources,
                note=str(m.get("note") or "")[:240],
            )
        )
    return out


def rewrite_with_checks(draft: str, checks: Sequence[ClaimCheck]) -> str:
    """Strip/mark contradicted claims; leave supported; flag insufficient."""
    text = draft or ""
    for c in checks:
        if c.verdict == "contradicted" and c.claim and c.claim in text:
            text = text.replace(
                c.claim,
                f"[Disputed — removed: {c.claim[:120]}]",
            )
        elif c.verdict in {"insufficient", "unavailable"} and c.claim and c.claim in text:
            text = text.replace(
                c.claim,
                f"{c.claim} _(unverified)_",
            )
    return text


def checks_public(checks: Sequence[ClaimCheck]) -> List[Dict[str, Any]]:
    return [c.public() for c in checks]


def _dedupe(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold().strip())[:200]


def _overlap(a: str, b: str) -> bool:
    ta = set(re.findall(r"[a-z0-9]{4,}", a.casefold()))
    tb = set(re.findall(r"[a-z0-9]{4,}", b.casefold()))
    if not ta or not tb:
        return False
    return len(ta & tb) >= max(2, min(len(ta), len(tb)) // 3)
