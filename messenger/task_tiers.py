"""Per-step retrieve vs reason × local vs frontier mapping (Flyleaf-native).

Inspired by auditable tiering (local/frontier/cost awareness) but not a port of
external keyword tables. Orchestrator steps declare ``kind`` + ``power``; this
module maps them to a preferred backend class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

StepKind = Literal[
    "retrieve", "reason", "critique", "fact_check", "synthesize", "automate", "orchestrate"
]
Power = Literal["low", "high"]
BackendClass = Literal["local", "frontier", "hybrid"]


@dataclass(frozen=True)
class TierDecision:
    kind: str
    power: str
    backend: BackendClass
    reason: str

    def public(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "power": self.power,
            "backend": self.backend,
            "reason": self.reason,
        }


# Default power when the Orchestrator omits it.
_DEFAULT_POWER: Dict[str, Power] = {
    "retrieve": "low",
    "automate": "low",
    "reason": "high",
    "critique": "high",
    "fact_check": "high",
    "synthesize": "high",
    "orchestrate": "high",
}


def normalize_kind(kind: str) -> str:
    k = (kind or "").strip().casefold()
    aliases = {
        "research": "retrieve",
        "lookup": "retrieve",
        "fetch": "retrieve",
        "automation": "automate",
        "ritual": "automate",
        "discuss": "critique",
        "debate": "critique",
        "check": "fact_check",
        "verify": "fact_check",
        "plan": "orchestrate",
        "brief": "synthesize",
    }
    return aliases.get(k, k or "reason")


def default_power(kind: str) -> Power:
    return _DEFAULT_POWER.get(normalize_kind(kind), "high")


def map_step(
    kind: str,
    power: Optional[str] = None,
    *,
    frontier_available: bool = True,
    local_available: bool = True,
) -> TierDecision:
    """Map a roadmap step to local / frontier / hybrid."""
    k = normalize_kind(kind)
    p: Power = "high" if (power or "").strip().casefold() == "high" else (
        "low" if (power or "").strip().casefold() == "low" else default_power(k)
    )

    if k in {"retrieve", "automate"}:
        preferred: BackendClass = "local"
        reason = "retrieve/automate prefer cheaper/local when possible"
    elif k == "fact_check":
        preferred = "frontier"
        reason = "fact-check prefers frontier + web"
    elif k in {"orchestrate", "synthesize"}:
        preferred = "frontier"
        reason = "roadmap / hard synthesis prefer frontier"
    elif k == "critique":
        preferred = "hybrid" if local_available and frontier_available else "frontier"
        reason = "critique can use room model; escalate hard cases"
    else:
        preferred = "frontier" if p == "high" else "local"
        reason = f"power={p} for kind={k}"

    backend = preferred
    if preferred == "local" and not local_available and frontier_available:
        backend = "frontier"
        reason += "; degraded to frontier (local missing)"
    elif preferred == "frontier" and not frontier_available and local_available:
        backend = "local"
        reason += "; degraded to local (frontier missing)"
    elif preferred == "hybrid":
        if frontier_available:
            backend = "frontier" if p == "high" else "hybrid"
        elif local_available:
            backend = "local"
            reason += "; hybrid collapsed to local"
        else:
            backend = "frontier"
            reason += "; no backends advertised"

    return TierDecision(kind=k, power=p, backend=backend, reason=reason)


def prefer_frontier_endpoint(tier: TierDecision) -> bool:
    return tier.backend in {"frontier", "hybrid"} and tier.power == "high"
