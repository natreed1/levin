"""Bounded declarative research loops and lightweight background jobs."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .ledger import Ledger
from .models import model_label, normalize_agent_model
from .orchestration import ALLOWED_ACTIONS, ClaudeGateway, estimate_tokens, extract_json
from .paths import ritual_specs_dir
from .redact import redact_text
from .schema import Event, Sensitivity, Surface, new_id, parse_sensitivity, sensitivity_allows_egress


class WorkflowCancelled(RuntimeError):
    pass


@dataclass
class BackgroundJob:
    job_id: str
    key: str
    kind: str
    status: str = "queued"
    progress: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "key": self.key,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def update(self, progress: str) -> None:
        self.progress = progress
        self.updated_at = time.time()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise WorkflowCancelled("Run cancelled by user.")


class JobManager:
    """In-process job registry with one active job per workflow key."""

    def __init__(self) -> None:
        self._jobs: Dict[str, BackgroundJob] = {}
        self._active: Dict[str, str] = {}
        self._lock = threading.Lock()

    def start(
        self, key: str, kind: str, fn: Callable[[BackgroundJob], Dict[str, Any]]
    ) -> BackgroundJob:
        with self._lock:
            active_id = self._active.get(key)
            if active_id:
                active = self._jobs.get(active_id)
                if active and active.status in {"queued", "running"}:
                    raise RuntimeError(f"A job is already running for {key}.")
            job = BackgroundJob(job_id=new_id("job"), key=key, kind=kind)
            self._jobs[job.job_id] = job
            self._active[key] = job.job_id

        def run() -> None:
            job.status = "running"
            job.updated_at = time.time()
            try:
                job.result = fn(job)
                job.status = "cancelled" if job.cancel_event.is_set() else "completed"
            except WorkflowCancelled as exc:
                job.status = "cancelled"
                job.error = str(exc)
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = str(exc)
            finally:
                job.updated_at = time.time()
                with self._lock:
                    if self._active.get(key) == job.job_id:
                        self._active.pop(key, None)

        threading.Thread(target=run, name=f"analyst-{job.job_id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[BackgroundJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> BackgroundJob:
        job = self.get(job_id)
        if not job:
            raise RuntimeError(f"Job '{job_id}' not found.")
        job.cancel_event.set()
        job.update("Cancellation requested")
        return job


def _load_approved_spec(ritual_id: str) -> Dict[str, Any]:
    path = ritual_specs_dir() / f"{ritual_id}.json"
    if not path.exists():
        raise RuntimeError(f"No spec for '{ritual_id}'.")
    spec = json.loads(path.read_text(encoding="utf-8"))
    if not spec.get("approved"):
        raise RuntimeError(f"Automation '{ritual_id}' is not approved.")
    if not spec.get("enabled", True):
        raise RuntimeError(f"Automation '{ritual_id}' is disabled.")
    return spec


class WorkflowEngine:
    def __init__(self, ledger: Ledger, gateway: Optional[ClaudeGateway] = None) -> None:
        self.ledger = ledger
        self.gateway = gateway or ClaudeGateway(ledger)

    def run(
        self,
        ritual_id: str,
        *,
        request: str = "",
        stub: bool = False,
        job: Optional[BackgroundJob] = None,
        model_override: Optional[str] = None,
        thread_id: Optional[str] = None,
        handoff: bool = True,
    ) -> Dict[str, Any]:
        spec = _load_approved_spec(ritual_id)
        agent_model = normalize_agent_model(model_override) or normalize_agent_model(
            spec.get("model")
        )
        if not stub and not agent_model:
            raise RuntimeError(
                "Choose an agent model (Claude or Qwen3 8B) in Edit automation "
                "before the first run."
            )
        # Prefer the model chosen on the spec; injected gateways keep their responder.
        if agent_model:
            if self.gateway.responder is None:
                self.gateway = ClaudeGateway(self.ledger, model=agent_model)
            else:
                self.gateway.model = agent_model
        if thread_id:
            thread = self.ledger.get_session(thread_id)
            if not thread or thread.surface != Surface.CHAT.value:
                raise RuntimeError(f"Chat thread '{thread_id}' not found.")
        else:
            thread = self.ledger.get_or_create_chat_thread(ritual_id)
        budget = spec.get("budget") if isinstance(spec.get("budget"), dict) else {}
        max_steps = max(1, min(6, int(budget.get("max_steps") or 6)))
        max_seconds = max(60, min(300, int(budget.get("max_minutes") or 5) * 60))
        max_tokens = max(512, min(16000, int(budget.get("max_tokens") or 8000)))
        started = time.monotonic()
        used_tokens = 0
        observations: List[Dict[str, Any]] = []
        remaining = [next(iter(step)) for step in (spec.get("steps") or [])]
        remaining = [a for a in remaining if a in ALLOWED_ACTIONS]
        if not remaining:
            raise RuntimeError("Automation has no executable allowlisted steps.")

        self._event(
            "workflow_run_started",
            thread.session_id,
            {
                "ritual_id": ritual_id,
                "budget": budget,
                "request": request[:500],
                "model": agent_model,
                "arena": bool(thread_id and not handoff),
            },
        )
        self.ledger.append_chat_message(
            thread.session_id,
            role="assistant",
            kind="status",
            content=(
                f"Started {ritual_id} research"
                + (f" with {model_label(agent_model)}." if agent_model else ".")
            ),
            metadata={"ritual_id": ritual_id, "model": agent_model},
        )
        try:
            for index in range(max_steps):
                self._check_limits(job, started, max_seconds, used_tokens, max_tokens)
                if not remaining:
                    break
                if job:
                    job.update(f"Choosing research step {index + 1}/{max_steps}")
                decision_prompt = (
                    "Choose the next action for this approved research workflow. Return JSON only "
                    "as {\"action\":\"allowed_action\"}. You may instead return "
                    "{\"final\":\"concise synthesis\"} when evidence is sufficient. "
                    f"Allowed remaining actions: {remaining}. Workflow: "
                    f"{json.dumps({'description': spec.get('description'), 'watchlist': spec.get('watchlist')})}. "
                    f"User request: {request[:1000]}. Observations: "
                    f"{json.dumps(observations, ensure_ascii=False)[-12000:]}"
                )
                decision_allowance = max_tokens - used_tokens - estimate_tokens(decision_prompt)
                if decision_allowance < 128:
                    break
                model = self.gateway.complete(
                    [{"role": "user", "content": decision_prompt}],
                    kind="workflow_decision",
                    session_id=thread.session_id,
                    max_tokens=min(500, decision_allowance),
                )
                used_tokens += model.estimated_input_tokens + model.estimated_output_tokens
                decision = extract_json(model.text)
                if isinstance(decision, dict) and decision.get("final"):
                    observations.append({"agent_interim": str(decision["final"])[:2000]})
                    break
                action = str(decision.get("action") if isinstance(decision, dict) else "")
                if action not in remaining:
                    action = remaining[0]
                remaining.remove(action)
                if job:
                    job.update(f"Running {action}")
                result = self._execute_action(
                    action, spec=spec, stub=stub, job=job
                )
                safe_result = json.loads(redact_text(json.dumps(result, ensure_ascii=False)))
                observations.append({"action": action, "result": safe_result})
                self._event(
                    "workflow_step",
                    thread.session_id,
                    {
                        "ritual_id": ritual_id,
                        "step": index + 1,
                        "action": action,
                        "result": safe_result,
                    },
                )
                self.ledger.append_chat_message(
                    thread.session_id,
                    role="tool",
                    kind="research_step",
                    content=f"{action}: {self._compact(safe_result)}",
                    metadata={"action": action, "step": index + 1},
                )

            self._check_limits(job, started, max_seconds, used_tokens, max_tokens)
            if job:
                job.update("Synthesizing research")
            final_prompt = (
                "Synthesize this workflow's research into a concise analyst chat response. "
                "Use only the supplied observations, clearly state missing data or errors, do not "
                "invent market facts, and do not recommend trades. Include a short summary, what "
                "was checked, and next checks.\n\n"
                f"Workflow: {ritual_id}\nRequest: {request[:1000]}\n"
                f"Observations: {json.dumps(observations, ensure_ascii=False)[-16000:]}"
            )
            final_allowance = max_tokens - used_tokens - estimate_tokens(final_prompt)
            if final_allowance < 128:
                raise RuntimeError("Workflow token budget exhausted before synthesis.")
            final_model = self.gateway.complete(
                [{"role": "user", "content": final_prompt}],
                kind="workflow_synthesis",
                session_id=thread.session_id,
                max_tokens=min(2048, final_allowance),
            )
            used_tokens += (
                final_model.estimated_input_tokens + final_model.estimated_output_tokens
            )
            final = final_model.text.strip()
            self.ledger.append_chat_message(
                thread.session_id,
                role="assistant",
                kind="synthesis",
                content=final,
                metadata={"ritual_id": ritual_id, "steps": len(observations)},
            )
            self._event(
                "workflow_run_completed",
                thread.session_id,
                {
                    "ritual_id": ritual_id,
                    "steps": len(observations),
                    "estimated_tokens": used_tokens,
                    "model": agent_model,
                },
            )
            if handoff:
                self._handoff(ritual_id, final)
            return {
                "status": "ok",
                "ritual_id": ritual_id,
                "thread_id": thread.session_id,
                "steps": len(observations),
                "estimated_tokens": used_tokens,
                "output": final,
                "model": agent_model,
            }
        except Exception as exc:
            self._event(
                "workflow_run_failed",
                thread.session_id,
                {"ritual_id": ritual_id, "error": str(exc)},
            )
            self.ledger.append_chat_message(
                thread.session_id,
                role="assistant",
                kind="error",
                content=f"Run stopped: {exc}",
                metadata={"ritual_id": ritual_id},
            )
            raise

    def _execute_action(
        self,
        action: str,
        *,
        spec: Dict[str, Any],
        stub: bool,
        job: Optional[BackgroundJob],
    ) -> Any:
        if job:
            job.check_cancelled()
        symbols = [str(s).upper() for s in (spec.get("watchlist") or [])][:20]
        if action in {"fetch_quote", "fetch_calendar", "fetch_headlines"}:
            from .morning_yf import _stub_quote, fetch_yahoo_quote

            rows = []
            for symbol in symbols or ["SPY"]:
                if job:
                    job.check_cancelled()
                row = _stub_quote(symbol) if stub else fetch_yahoo_quote(symbol)
                if action == "fetch_calendar":
                    row = {"symbol": symbol, "next_earnings": row.get("next_earnings")}
                elif action == "fetch_headlines":
                    row = {"symbol": symbol, "headlines": row.get("headlines") or []}
                rows.append(row)
            return rows
        if action == "sec_filings":
            from .runners import _recent_filings, _stub_filings, _ticker_to_cik_map

            cik_map = {} if stub else _ticker_to_cik_map()
            return [
                {
                    "symbol": symbol,
                    "filings": (
                        _stub_filings(symbol)
                        if stub
                        else _recent_filings(cik_map[symbol], days=3)
                        if symbol in cik_map
                        else []
                    ),
                }
                for symbol in symbols
            ]
        if action == "find_files":
            from .file_search import build_query, search_files, stub_matches

            params: Dict[str, Any] = {}
            for step in spec.get("steps") or []:
                if isinstance(step, dict) and "find_files" in step:
                    raw = step.get("find_files")
                    if isinstance(raw, dict):
                        params = raw
                    break
            limit = max(1, min(20, int(params.get("limit") or 5)))
            fquery = build_query(str(params.get("query") or ""), extra_symbols=symbols)
            found = stub_matches(fquery) if stub else search_files(fquery, limit=limit)
            # public() carries relative paths only — absolute paths never
            # enter observations, which are fed back to the model.
            return [m.public() for m in found]
        if action == "recent_notes":
            picked: List[Dict[str, str]] = []
            for ev in self.ledger.list_events(limit=500, types=["note"]):
                if ev.get("surface") in {Surface.RITUAL.value, Surface.CHAT.value}:
                    continue
                level = parse_sensitivity(ev.get("sensitivity"))
                if not sensitivity_allows_egress(level, Sensitivity.INTERNAL):
                    continue
                text = redact_text(str((ev.get("payload") or {}).get("text") or ""))
                if text:
                    picked.append({"ts": str(ev.get("ts")), "text": text[:500]})
                if len(picked) >= 30:
                    break
            return list(reversed(picked))
        if action == "public_web_search":
            from .web_search import bing_search

            step_cfg = None
            for step in spec.get("steps") or []:
                if isinstance(step, dict) and "public_web_search" in step:
                    step_cfg = step.get("public_web_search")
                    break
            queries: List[str] = []
            if isinstance(step_cfg, str) and step_cfg.strip():
                queries.append(step_cfg.strip()[:200])
            elif isinstance(step_cfg, list):
                queries.extend(str(q).strip()[:200] for q in step_cfg if str(q).strip())
            elif isinstance(step_cfg, dict):
                raw_q = step_cfg.get("query") or step_cfg.get("queries")
                if isinstance(raw_q, str) and raw_q.strip():
                    queries.append(raw_q.strip()[:200])
                elif isinstance(raw_q, list):
                    queries.extend(str(q).strip()[:200] for q in raw_q if str(q).strip())
            if not queries:
                desc = str(spec.get("description") or "").strip()
                if desc:
                    queries.append(desc[:200])
            if not queries:
                queries = ["market news"]
            rows = []
            for query in queries[:4]:
                if job:
                    job.check_cancelled()
                hits = [] if stub else bing_search(query, limit=5)
                if stub:
                    hits = [
                        {
                            "title": f"Stub hit for {query}",
                            "url": "https://example.com/stub",
                            "snippet": "Offline stub result.",
                        }
                    ]
                rows.append({"query": query, "hits": hits})
            return rows
        raise RuntimeError(f"Action '{action}' is not allowlisted.")

    def _handoff(self, ritual_id: str, final: str) -> None:
        master = self.ledger.get_or_create_chat_thread(master=True)
        summary = redact_text(final)[:3000]
        self._event(
            "workflow_handoff",
            master.session_id,
            {"from": ritual_id, "summary": summary},
        )
        self.ledger.append_chat_message(
            master.session_id,
            role="system",
            kind="handoff",
            content=f"{ritual_id} completed:\n{summary}",
            metadata={"from": ritual_id},
        )

    def _event(self, event_type: str, session_id: str, payload: Dict[str, Any]) -> None:
        self.ledger.append_event(
            Event(
                type=event_type,
                surface=Surface.CHAT.value,
                session_id=session_id,
                sensitivity=Sensitivity.INTERNAL.value,
                payload=payload,
            )
        )

    @staticmethod
    def _compact(value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= 1200 else text[:1197] + "..."

    @staticmethod
    def _check_limits(
        job: Optional[BackgroundJob],
        started: float,
        max_seconds: int,
        used_tokens: int,
        max_tokens: int,
    ) -> None:
        if job:
            job.check_cancelled()
        if time.monotonic() - started >= max_seconds:
            raise RuntimeError("Workflow time budget exhausted.")
        if used_tokens >= max_tokens:
            raise RuntimeError("Workflow token budget exhausted.")


class MasterCoordinator:
    """Route a master-chat request to approved workflows and consolidate results."""

    def __init__(self, ledger: Ledger, gateway: Optional[ClaudeGateway] = None) -> None:
        self.ledger = ledger
        self.gateway = gateway or ClaudeGateway(ledger)

    def run(self, message: str, *, job: Optional[BackgroundJob] = None) -> Dict[str, Any]:
        from .rituals import list_automations

        master = self.ledger.get_or_create_chat_thread(master=True)
        approved = [
            a["ritual_id"]
            for a in list_automations(self.ledger)
            if a.get("approved") and a.get("enabled", True) and a.get("model")
        ]
        if not approved:
            raise RuntimeError(
                "No approved, enabled automations with an agent model are available. "
                "Open each automation and choose Claude or Qwen3 8B before running."
            )
        route_prompt = (
            "Select up to three approved workflows for the user's request. Return JSON only as "
            "{\"ritual_ids\":[...]}. Use only names from this list: "
            f"{approved}. User request: {message[:2000]}"
        )
        routed = self.gateway.complete(
            [{"role": "user", "content": route_prompt}],
            kind="master_route",
            session_id=master.session_id,
            max_tokens=500,
        )
        route = extract_json(routed.text)
        selected = [
            rid
            for rid in (route.get("ritual_ids") if isinstance(route, dict) else [])
            if rid in approved
        ][:3]
        if not selected:
            selected = approved[:1]
        results = []
        for rid in selected:
            if job:
                job.check_cancelled()
                job.update(f"Running {rid}")
            results.append(
                WorkflowEngine(self.ledger, self.gateway).run(
                    rid, request=message, stub=False, job=job
                )
            )
        if job:
            job.update("Consolidating workflow results")
        summaries = [{"ritual_id": r["ritual_id"], "output": r["output"]} for r in results]
        final = self.gateway.complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Consolidate these workflow handoffs into one concise response to the "
                        "user. Preserve disagreements and uncertainty; do not invent facts or "
                        "recommend trades.\nUser request: "
                        + message[:2000]
                        + "\nHandoffs: "
                        + json.dumps(summaries, ensure_ascii=False)
                    ),
                }
            ],
            kind="master_synthesis",
            session_id=master.session_id,
            max_tokens=2048,
        ).text
        self.ledger.append_chat_message(
            master.session_id,
            role="assistant",
            kind="synthesis",
            content=final,
            metadata={"workflows": selected},
        )
        return {"status": "ok", "thread_id": master.session_id, "workflows": selected, "output": final}
