"""In-cloud scheduler: runs approved+enabled ritual specs per user."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("messenger.scheduler")


def _parse_cron_fields(schedule: str) -> Optional[dict[str, set[int]]]:
    """Parse a 5-field cron (min hour dom month dow). Returns None if invalid."""
    parts = (schedule or "").strip().split()
    if len(parts) != 5:
        return None

    def expand(field: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for piece in field.split(","):
            piece = piece.strip()
            if piece == "*":
                out.update(range(lo, hi + 1))
                continue
            if "/" in piece:
                base, step_s = piece.split("/", 1)
                step = int(step_s)
                if base == "*":
                    start, end = lo, hi
                elif "-" in base:
                    a, b = base.split("-", 1)
                    start, end = int(a), int(b)
                else:
                    start = end = int(base)
                out.update(range(start, end + 1, step))
                continue
            if "-" in piece:
                a, b = piece.split("-", 1)
                out.update(range(int(a), int(b) + 1))
                continue
            out.add(int(piece))
        return out

    try:
        return {
            "minute": expand(parts[0], 0, 59),
            "hour": expand(parts[1], 0, 23),
            "dom": expand(parts[2], 1, 31),
            "month": expand(parts[3], 1, 12),
            "dow": expand(parts[4], 0, 6),  # 0=Sunday
        }
    except ValueError:
        return None


def cron_matches(schedule: str, when: Optional[datetime] = None) -> bool:
    """Return True if ``schedule`` matches the given UTC datetime (minute precision)."""
    fields = _parse_cron_fields(schedule)
    if not fields:
        return False
    when = when or datetime.now(timezone.utc)
    # Python weekday: Mon=0 … Sun=6; cron: Sun=0 … Sat=6
    cron_dow = (when.weekday() + 1) % 7
    return (
        when.minute in fields["minute"]
        and when.hour in fields["hour"]
        and when.day in fields["dom"]
        and when.month in fields["month"]
        and cron_dow in fields["dow"]
    )


class CloudScheduler:
    """Background loop that fires approved automations for every known user."""

    def __init__(
        self,
        *,
        list_user_ids: Callable[[], list[str]],
        interval_seconds: float = 30.0,
        run_stub: bool = True,
    ) -> None:
        self._list_user_ids = list_user_ids
        self._interval = max(5.0, float(interval_seconds))
        self._run_stub = bool(run_stub)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # (user_id, ritual_id, YYYY-MM-DDTHH:MM) → already fired
        self._fired: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="messenger-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("CloudScheduler started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def tick(self, when: Optional[datetime] = None) -> list[dict[str, Any]]:
        """Run one scheduling pass. Returns list of fired run summaries."""
        when = when or datetime.now(timezone.utc)
        slot = when.strftime("%Y-%m-%dT%H:%M")
        fired: list[dict[str, Any]] = []
        try:
            user_ids = list(self._list_user_ids())
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler list_user_ids failed: %s", exc)
            return fired

        from messenger.tenancy import user_context

        for user_id in user_ids:
            try:
                with user_context(user_id) as ledger:
                    from analyst_ledger.rituals import list_automations, load_spec
                    from analyst_ledger.runners import resolve_runner
                    from analyst_ledger.workflow_engine import WorkflowEngine

                    for auto in list_automations(ledger):
                        if not auto.get("approved") or not auto.get("enabled", True):
                            continue
                        ritual_id = str(auto.get("ritual_id") or "")
                        if not ritual_id:
                            continue
                        try:
                            spec = load_spec(ritual_id)
                        except Exception:
                            continue
                        schedule = str(spec.get("schedule") or "").strip()
                        if not schedule or not cron_matches(schedule, when):
                            continue
                        key = (user_id, ritual_id, slot)
                        with self._lock:
                            if key in self._fired:
                                continue
                            self._fired.add(key)
                            # Bound memory: keep last ~10k entries
                            if len(self._fired) > 10_000:
                                self._fired = set(list(self._fired)[-5_000:])

                        stub = self._run_stub
                        model = spec.get("model")
                        # Prefer agentic run when a model is configured and stub is off.
                        try:
                            if not stub and model:
                                result = WorkflowEngine(ledger).run(
                                    ritual_id, request="scheduled run", stub=False
                                )
                            else:
                                _, runner_fn = resolve_runner(
                                    ritual_id,
                                    explicit=(spec.get("runner") or "").strip() or None,
                                )
                                result = runner_fn(
                                    ledger=ledger,
                                    ritual_id=ritual_id,
                                    stub=True,
                                    require_approved=True,
                                )
                            fired.append(
                                {
                                    "user_id": user_id,
                                    "ritual_id": ritual_id,
                                    "slot": slot,
                                    "result": result,
                                }
                            )
                            logger.info(
                                "scheduled run user=%s ritual=%s ok",
                                user_id,
                                ritual_id,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "scheduled run failed user=%s ritual=%s: %s",
                                user_id,
                                ritual_id,
                                exc,
                            )
                            fired.append(
                                {
                                    "user_id": user_id,
                                    "ritual_id": ritual_id,
                                    "slot": slot,
                                    "error": str(exc),
                                }
                            )
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduler user %s failed: %s", user_id, exc)
        return fired

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduler tick error: %s", exc)
            self._stop.wait(self._interval)
