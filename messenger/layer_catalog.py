"""Compatibility shim — prefer ``analyst_ledger.registry``.

Keeps older imports working while the Flyleaf three-layer SoR lives in registry.
"""

from __future__ import annotations

from typing import Any, Dict, List

from analyst_ledger.registry import (
    list_agents_public,
    list_automations_public,
    list_builtin_capabilities,
    list_capabilities_public,
)


# Legacy names used by older callers / tests.
BUILTIN_CAPABILITIES = [c.to_public() for c in list_builtin_capabilities()]


def list_capabilities(ledger: Any = None) -> List[Dict[str, Any]]:
    return list_capabilities_public(ledger=ledger)


def list_agents_catalog() -> List[Dict[str, Any]]:
    return list_agents_public()


def list_automation_loops(ledger: Any = None) -> List[Dict[str, Any]]:
    return list_automations_public(ledger=ledger)
