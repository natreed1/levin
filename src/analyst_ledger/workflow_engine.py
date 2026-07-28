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
    """Master chat: set up rooms/agents/workflows (website APIs) + optionally run approved loops."""

    def __init__(
        self,
        ledger: Ledger,
        gateway: Optional[ClaudeGateway] = None,
        *,
        store: Any = None,
        user_id: Optional[str] = None,
    ) -> None:
        self.ledger = ledger
        self.gateway = gateway or ClaudeGateway(ledger)
        self.store = store
        self.user_id = user_id

    def run(
        self,
        message: str,
        *,
        job: Optional[BackgroundJob] = None,
        stub: bool = False,
    ) -> Dict[str, Any]:
        from .rituals import list_automations
        from messenger.master_setup import (
            catalog_snapshot,
            execute_tool,
            remember_setup,
            retrieve_setup_memory,
            stub_plan_from_message,
        )

        master = self.ledger.get_or_create_chat_thread(master=True)
        approved = [
            a["ritual_id"]
            for a in list_automations(self.ledger)
            if a.get("approved") and a.get("enabled", True) and a.get("model")
        ]

        if job:
            job.update("Planning setup")

        memory = retrieve_setup_memory(message, limit=4)
        catalog = catalog_snapshot(store=self.store, user_id=self.user_id)
        plan = self._plan(
            message,
            catalog=catalog,
            memory=memory,
            approved=approved,
            session_id=master.session_id,
            stub=stub,
        )

        tool_results: List[Dict[str, Any]] = []
        created_room_id: Optional[str] = None
        hired_ids: List[str] = []
        for step in (plan.get("tools") or [])[:12]:
            if not isinstance(step, dict):
                continue
            name = str(step.get("name") or "").strip()
            args = step.get("args") if isinstance(step.get("args"), dict) else {}
            if job:
                job.check_cancelled()
                job.update(f"Setup: {name}")
            # Prompt-inject learned patterns into newly hired agents
            if name == "hire_agent" and memory and not args.get("memory_hint"):
                hints = [
                    str(m.get("summary") or m.get("request") or "")[:200]
                    for m in memory[:2]
                ]
                args = {**args, "memory_hint": " | ".join(h for h in hints if h)}
            # After create_room in this plan, force room-scoped tools onto that room.
            # Models invent placeholder room_ids (title / fake tokens) before create
            # returns the real id — only filling when room_id is missing left those
            # assigns failing with room_not_found_or_forbidden while the team existed.
            if created_room_id and name in {
                "assign_agent",
                "set_workspace",
                "configure_room",
                "create_loop",
                "apply_recipe",
            }:
                args = {**args, "room_id": created_room_id}

            if self.store is None or not self.user_id:
                result = {
                    "ok": False,
                    "error": "master_setup_requires_store",
                    "tool": name,
                }
            else:
                result = execute_tool(
                    name, args, store=self.store, user_id=self.user_id
                )
            result = {"tool": name, **result}
            tool_results.append(result)
            if result.get("ok") and name == "create_room":
                created_room_id = str(result.get("room_id") or "") or created_room_id
            if result.get("ok") and name == "hire_agent":
                aid = str(result.get("agent_id") or "")
                if aid:
                    hired_ids.append(aid)
            if result.get("ok") and name == "apply_recipe":
                rid = str(result.get("room_id") or "")
                if rid:
                    created_room_id = created_room_id or rid

        # If we created a room but never wrote a graph, and the ask matches a recipe,
        # apply it — covers models that create_room + hire without apply_recipe.
        applied_recipe = any(
            r.get("tool") == "apply_recipe" and r.get("ok") for r in tool_results
        )
        if (
            created_room_id
            and not applied_recipe
            and self.store is not None
            and self.user_id
        ):
            from messenger.graph_recipes import match_recipe
            from messenger.team_harness import normalize_graph

            matched = match_recipe(message)
            room = self.store.room(created_room_id)
            layers = normalize_graph((room or {}).get("config", {}).get("graph")).get(
                "layers"
            ) or []
            if matched and room and not layers:
                if job:
                    job.update(f"Apply recipe {matched['id']}")
                apply = execute_tool(
                    "apply_recipe",
                    {
                        "recipe_id": matched["id"],
                        "room_id": created_room_id,
                        "objective": message[:400],
                        "query": message[:400],
                    },
                    store=self.store,
                    user_id=self.user_id,
                )
                tool_results.append({"tool": "apply_recipe", **apply})

        # Auto-assign hired agents to the room we just created
        if (
            created_room_id
            and hired_ids
            and self.store is not None
            and self.user_id
        ):
            room_after = self.store.room(created_room_id) or {}
            already = set(
                (room_after.get("config") or {}).get("agents")
                or (room_after.get("config") or {}).get("specialists")
                or []
            )
            for aid in hired_ids:
                if aid in already:
                    continue
                if job:
                    job.update(f"Assign {aid}")
                assign = execute_tool(
                    "assign_agent",
                    {"room_id": created_room_id, "agent_id": aid},
                    store=self.store,
                    user_id=self.user_id,
                )
                tool_results.append({"tool": "assign_agent", **assign})

        # Optional: still run approved workflows when explicitly requested
        selected: List[str] = []
        workflow_summaries: List[Dict[str, Any]] = []
        requested = [
            rid
            for rid in (plan.get("run_workflows") or [])
            if isinstance(rid, str) and rid in approved
        ][:3]
        # Legacy path: if the plan is empty but user clearly wants a run and we have tools none
        if not tool_results and not requested and approved and self._looks_like_run(message):
            requested = approved[:1]
        for rid in requested:
            if job:
                job.check_cancelled()
                job.update(f"Running {rid}")
            run = WorkflowEngine(self.ledger, self.gateway).run(
                rid, request=message, stub=stub, job=job
            )
            selected.append(rid)
            workflow_summaries.append(
                {"ritual_id": run.get("ritual_id"), "output": run.get("output")}
            )

        if job:
            job.update("Writing reply")

        final = self._compose_reply(
            message=message,
            plan=plan,
            tool_results=tool_results,
            workflow_summaries=workflow_summaries,
            session_id=master.session_id,
            stub=stub,
        )

        ok_actions = [r for r in tool_results if r.get("ok")]
        if ok_actions:
            remember_setup(
                user_request=message,
                actions=[
                    {
                        "tool": a.get("tool"),
                        "room_id": a.get("room_id"),
                        "agent_id": a.get("agent_id"),
                        "ritual_id": a.get("ritual_id"),
                        "title": a.get("title") or a.get("name"),
                    }
                    for a in ok_actions
                ],
                summary=final[:600],
            )

        self.ledger.append_chat_message(
            master.session_id,
            role="assistant",
            kind="master_setup" if ok_actions else "synthesis",
            content=final,
            metadata={
                "workflows": selected,
                "tools": [
                    {
                        "tool": r.get("tool"),
                        "ok": r.get("ok"),
                        "room_id": r.get("room_id"),
                        "agent_id": r.get("agent_id"),
                        "ritual_id": r.get("ritual_id"),
                        "error": r.get("error"),
                    }
                    for r in tool_results
                ],
                "workspace_hints": [
                    r.get("needs_user_input") or r.get("workspace")
                    for r in tool_results
                    if r.get("needs_user_input") or r.get("workspace")
                ],
            },
        )
        return {
            "status": "ok",
            "thread_id": master.session_id,
            "workflows": selected,
            "tools": tool_results,
            "output": final,
        }

    @staticmethod
    def _looks_like_run(message: str) -> bool:
        lower = (message or "").lower()
        setup_words = (
            "create",
            "set up",
            "setup",
            "hire",
            "build a",
            "make a",
            "room",
            "team",
            "agent",
            "workflow",
            "loop",
        )
        if any(w in lower for w in setup_words):
            return False
        return any(w in lower for w in ("run", "execute", "kick off", "start "))

    def _plan(
        self,
        message: str,
        *,
        catalog: Dict[str, Any],
        memory: List[Dict[str, Any]],
        approved: List[str],
        session_id: str,
        stub: bool,
    ) -> Dict[str, Any]:
        from messenger.master_setup import stub_plan_from_message

        if stub:
            return stub_plan_from_message(message)

        slim_catalog = {
            "agents": [
                {"id": a.get("id"), "name": a.get("name"), "capabilities": a.get("capabilities")}
                for a in (catalog.get("agents") or [])[:30]
            ],
            "capabilities": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "runner": c.get("runner"),
                    "action": c.get("action"),
                }
                for c in (catalog.get("capabilities") or [])[:30]
            ],
            "lenses": (catalog.get("lenses") or [])[:20],
            "rooms": [
                {
                    "room_id": r.get("room_id"),
                    "title": r.get("title"),
                    "skills": r.get("skills"),
                    "agents": r.get("agents"),
                }
                for r in (catalog.get("rooms") or [])[:20]
            ],
            "recipes": (catalog.get("recipes") or [])[:12],
            "tools": catalog.get("tools"),
            "need_kinds": catalog.get("need_kinds"),
            "approved_workflows": approved[:20],
        }
        prompt = (
            "You are Master on Flyleaf. You set up Teams (rooms), hire agents, apply "
            "Graph recipes, and draft workflow loops using ONLY the allowlisted tools — "
            "the same building blocks as Hire + Graph on the website. You do NOT invent "
            "runners or merge code.\n"
            "Return JSON only:\n"
            '{"reply":str,"tools":[{"name":str,"args":{}}],"run_workflows":[str]}\n'
            "Tools: apply_recipe, create_room, hire_agent, assign_agent, configure_room, "
            "set_workspace, create_loop, draft_capability, list_catalog.\n"
            "Tool args (required fields must be present — use these exact keys):\n"
            "- apply_recipe: {\"recipe_id\":str from catalog.recipes OR \"query\":str, "
            "\"objective\":str optional, \"title\":str optional, \"room_id\":str optional, "
            "\"edits\":{\"disable_steps\":[str],\"agent_overrides\":{role:name},"
            "\"guard_overrides\":{from_to:prompt}}}. "
            "Creates the team + specialists + Graph steps from the library. "
            "Prefer this whenever a catalog recipe matches (equity research, PR+security, "
            "filings digest, multi-analyst).\n"
            "- hire_agent: {\"name\":str (required; also accepts title/agent_name/label), "
            "\"capability_ids\":[str], \"lens_ids\":[str], \"prompt\":str, "
            "\"summary\":str, \"stage\":str}. If no catalog agent fits, hire a new one "
            "instead of only assign_agent on missing ids.\n"
            "- assign_agent: {\"agent_id\":str, \"role\":str, \"room_id\":str optional}. "
            "Use catalog ids; display names like \"Diff Reviewer\" auto-create when missing. "
            "After create_room in the same plan, omit room_id (runtime injects the new id).\n"
            "- create_room: {\"title\":str (required), \"objective\":str, "
            "\"orchestrator\":\"chat\"|\"debate\"|\"workflow\", \"agents\":[str], "
            "\"skills\":[str], \"needs\":[{\"kind\":str,\"label\":str}]}\n"
            "- set_workspace: {\"repo_url\":str, \"needs\":[...], \"room_id\":str optional}\n"
            "Rules:\n"
            "- When the user asks to create a research/equities/coding/filings team, "
            "call apply_recipe with the matching recipe_id. Do NOT invent graph JSON.\n"
            "- Prefer composing existing capability ids from the catalog.\n"
            "- Never invent room_id values. create_room / apply_recipe first, then "
            "room-scoped tools without a fabricated id.\n"
            "- When the plan needs a specialist not in catalog, call hire_agent first "
            "(with name + capability_ids), then assign_agent.\n"
            "- For coding/GitHub rooms prefer recipe pr_security (includes workspace needs). "
            "Never ask the user to paste secrets in chat — tell them to use Room settings.\n"
            "- create_loop drafts are always unapproved.\n"
            "- run_workflows only for approved workflow ids when the user wants a run now.\n"
            "- If the ask is just conversation, tools=[] and a helpful reply.\n"
            f"Catalog: {json.dumps(slim_catalog, ensure_ascii=False)[:12000]}\n"
            f"Past successful setups (RAG): {json.dumps(memory, ensure_ascii=False)[:3000]}\n"
            f"User request: {message[:2000]}"
        )
        try:
            routed = self.gateway.complete(
                [{"role": "user", "content": prompt}],
                kind="master_setup_plan",
                session_id=session_id,
                max_tokens=1800,
            )
            plan = extract_json(routed.text)
            if not isinstance(plan, dict):
                return stub_plan_from_message(message)
            tools = plan.get("tools") if isinstance(plan.get("tools"), list) else []
            plan["tools"] = [t for t in tools if isinstance(t, dict)][:12]
            plan["run_workflows"] = [
                str(x)
                for x in (plan.get("run_workflows") or [])
                if str(x) in approved
            ][:3]
            plan["reply"] = str(plan.get("reply") or "").strip()
            return plan
        except Exception:
            return stub_plan_from_message(message)

    def _compose_reply(
        self,
        *,
        message: str,
        plan: Dict[str, Any],
        tool_results: List[Dict[str, Any]],
        workflow_summaries: List[Dict[str, Any]],
        session_id: str,
        stub: bool,
    ) -> str:
        base = str(plan.get("reply") or "").strip()
        lines: List[str] = []
        if base:
            lines.append(base)
        for r in tool_results:
            if r.get("ok") and r.get("message"):
                lines.append(f"• {r['message']}")
            elif not r.get("ok"):
                lines.append(f"• Failed {r.get('tool')}: {r.get('error')}")
            if r.get("needs_user_input"):
                needs = r["needs_user_input"]
                if isinstance(needs, list) and needs:
                    labels = ", ".join(
                        str(n.get("label") or n.get("id")) for n in needs if isinstance(n, dict)
                    )
                    lines.append(
                        f"• Still needed in Room settings: {labels}. "
                        "Open the team → Harness to paste them (not in this chat)."
                    )
        if workflow_summaries and not stub:
            try:
                final = self.gateway.complete(
                    [
                        {
                            "role": "user",
                            "content": (
                                "Consolidate these workflow handoffs into one concise response. "
                                "Preserve uncertainty; do not invent facts or recommend trades.\n"
                                f"User request: {message[:2000]}\n"
                                f"Setup notes: {base[:800]}\n"
                                f"Handoffs: {json.dumps(workflow_summaries, ensure_ascii=False)}"
                            ),
                        }
                    ],
                    kind="master_synthesis",
                    session_id=session_id,
                    max_tokens=2048,
                ).text
                return final
            except Exception:
                pass
        if workflow_summaries:
            for w in workflow_summaries:
                lines.append(f"• Ran {w.get('ritual_id')}: {str(w.get('output') or '')[:400]}")
        if not lines:
            return (
                "I'm Master. Ask me to create a team, hire agents, or draft a workflow loop — "
                "I'll use the same Hire/Harness building blocks as the website."
            )
        return "\n".join(lines)
