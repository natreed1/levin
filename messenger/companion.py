"""Local companion link — Phase 6 safeguard (data stays on the user's machine).

When a companion is registered for a user, the cloud app can prefer reading
that user's ledger via a local Python bridge instead of the cloud volume.

This module ships the registration + probe surface. Full sync/relay lands
once accounts and cloud automations are stable.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _state_path() -> Path:
    raw = os.environ.get("MESSENGER_DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "companions.json"


class CompanionRegistry:
    """Maps user_id → local companion base URL (e.g. http://127.0.0.1:8791)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _state_path()
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def register(self, user_id: str, base_url: str, *, token: str = "") -> dict[str, Any]:
        uid = str(user_id).strip()
        url = str(base_url).strip().rstrip("/")
        if not uid or not url:
            raise ValueError("user_id and base_url required")
        entry = {
            "user_id": uid,
            "base_url": url,
            "token": token,
            "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with self._lock:
            data = self._load()
            data[uid] = entry
            self._save(data)
        return entry

    def get(self, user_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._load().get(str(user_id))

    def unlink(self, user_id: str) -> bool:
        with self._lock:
            data = self._load()
            existed = str(user_id) in data
            data.pop(str(user_id), None)
            self._save(data)
            return existed

    def probe(self, user_id: str, timeout: float = 2.0) -> dict[str, Any]:
        """Ping the companion /healthz. Returns status payload (never raises)."""
        entry = self.get(user_id)
        if not entry:
            return {"ok": False, "linked": False, "error": "not_registered"}
        url = f"{entry['base_url']}/healthz"
        headers = {}
        if entry.get("token"):
            headers["Authorization"] = f"Bearer {entry['token']}"
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "linked": True,
                "reachable": True,
                "base_url": entry["base_url"],
                "body": body[:500],
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "linked": True,
                "reachable": False,
                "base_url": entry["base_url"],
                "error": str(exc),
            }


# Module-level default registry used by the API.
registry = CompanionRegistry()
