"""Team harness workflow runner — posts executable capability steps into room chat.

Used when ``room.config.orchestrator == "workflow"`` (Run harness / Run workflow once).
Debate/chat modes stay in ``specialist_room``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional, Sequence

from messenger.specialist_room import _post, job_registry

logger = logging.getLogger("messenger.team_harness")


def normalize_graph(raw: Any) -> Dict[str, Any]:
    """Sanitize room.config.graph — layered steps, members, and inter-layer guards."""
    src = raw if isinstance(raw, dict) else {}
    layers_out: List[Dict[str, Any]] = []
    for idx, layer in enumerate((src.get("layers") or [])[:24]):
        if not isinstance(layer, dict):
            continue
        layer_id = str(layer.get("id") or f"layer_{idx + 1}").strip()[:64]
        if not layer_id:
            continue
        members: List[Dict[str, str]] = []
        seen: set[str] = set()
        for m in (layer.get("members") or [])[:24]:
            agent_id = ""
            instructions = ""
            if isinstance(m, str):
                agent_id = m.strip()
            elif isinstance(m, dict):
                agent_id = str(m.get("agent_id") or m.get("id") or "").strip()
                instructions = str(m.get("instructions") or "")[:2000]
            if not agent_id or agent_id in seen:
                continue
            seen.add(agent_id)
            members.append({"agent_id": agent_id[:80], "instructions": instructions})
        layers_out.append(
            {
                "id": layer_id,
                "title": str(layer.get("title") or f"Step {idx + 1}")[:80],
                "prompt": str(layer.get("prompt") or "")[:2000],
                "goal": str(layer.get("goal") or "")[:800],
                "members": members,
            }
        )
    layer_ids = {str(l["id"]) for l in layers_out}
    guards_out: List[Dict[str, Any]] = []
    for idx, guard in enumerate((src.get("guards") or [])[:24]):
        if not isinstance(guard, dict):
            continue
        from_id = str(guard.get("from_layer_id") or "").strip()
        to_id = str(guard.get("to_layer_id") or "").strip()
        if from_id not in layer_ids or to_id not in layer_ids:
            continue
        guards_out.append(
            {
                "id": str(guard.get("id") or f"guard_{idx + 1}")[:64],
                "from_layer_id": from_id,
                "to_layer_id": to_id,
                "prompt": str(guard.get("prompt") or "")[:2000],
            }
        )
    # Ensure consecutive layers have a guard edge
    existing = {(g["from_layer_id"], g["to_layer_id"]) for g in guards_out}
    for i in range(len(layers_out) - 1):
        pair = (layers_out[i]["id"], layers_out[i + 1]["id"])
        if pair not in existing:
            guards_out.insert(
                i,
                {
                    "id": f"guard_{i + 1}",
                    "from_layer_id": pair[0],
                    "to_layer_id": pair[1],
                    "prompt": "",
                },
            )
    return {"layers": layers_out, "guards": guards_out}


def capability_is_executable(cap: Any) -> bool:
    """True when a capability can invoke a runner/action (not prompt-only)."""
    if cap is None:
        return False
    if isinstance(cap, dict):
        runner = cap.get("runner")
        action = cap.get("action")
        cid = str(cap.get("id") or "")
        steps = cap.get("steps") or ()
    else:
        runner = getattr(cap, "runner", None)
        action = getattr(cap, "action", None)
        cid = str(getattr(cap, "id", "") or "")
        steps = getattr(cap, "steps", ()) or ()
    if cid == "web_research":
        return True
    if runner or action:
        return True
    # User drafts may store capability ids in steps
    return bool(steps)


def enrich_capability_public(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["executable"] = capability_is_executable(row)
    out["execution_kind"] = (
        "executable" if out["executable"] else "prompt_only"
    )
    return out


def _room_config(room: dict[str, Any]) -> dict[str, Any]:
    config = room.get("config") if isinstance(room.get("config"), dict) else {}
    if not config and room.get("config_json"):
        try:
            config = json.loads(room["config_json"])
        except Exception:
            config = {}
    return config if isinstance(config, dict) else {}


def _allowlisted_caps(config: dict[str, Any]) -> List[str]:
    skills = config.get("skills") or []
    if not isinstance(skills, list):
        return []
    return [str(s).strip() for s in skills if str(s).strip()][:20]


def _loops_for_room(ledger: Any, room_id: str) -> List[Dict[str, Any]]:
    """Ritual specs bound to this room (capability loops)."""
    from analyst_ledger.paths import ritual_specs_dir

    out: List[Dict[str, Any]] = []
    root = ritual_specs_dir()
    if not root.exists():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(spec, dict):
            continue
        if str(spec.get("room_id") or "") != str(room_id):
            continue
        out.append(spec)
    return out


def _resolve_step_ids(config: dict[str, Any], room_id: str, ledger: Any) -> List[str]:
    """Ordered executable capability ids: first bound loop steps, else allowlist."""
    loops = _loops_for_room(ledger, room_id)
    for spec in loops:
        caps = spec.get("capability_ids") or []
        if isinstance(caps, list) and caps:
            return [str(c).strip() for c in caps if str(c).strip()][:12]
        steps = spec.get("steps") or []
        ids: List[str] = []
        for step in steps:
            if isinstance(step, dict) and step:
                ids.append(str(next(iter(step))))
            elif isinstance(step, str) and step.strip():
                ids.append(step.strip())
        if ids:
            return ids[:12]
    return _allowlisted_caps(config)


def _run_capability(
    cap_id: str,
    *,
    ledger: Any,
    stub: bool,
    objective: str,
) -> Dict[str, Any]:
    from analyst_ledger.orchestration import ALLOWED_ACTIONS, ALLOWED_RUNNERS
    from analyst_ledger.registry import get_capability
    from analyst_ledger.runners import RUNNERS

    cap = get_capability(cap_id)
    runner_name = None
    action = None
    if cap is not None:
        runner_name = getattr(cap, "runner", None)
        action = getattr(cap, "action", None)
    if not runner_name and cap_id in ALLOWED_RUNNERS:
        runner_name = cap_id
    if not action and cap_id in ALLOWED_ACTIONS:
        action = cap_id

    if stub:
        return {
            "ok": True,
            "stub": True,
            "capability_id": cap_id,
            "summary": f"(stub) Would run {runner_name or action or cap_id}",
        }

    if runner_name and runner_name in RUNNERS:
        fn = RUNNERS[runner_name]
        try:
            result = fn(
                ledger=ledger,
                ritual_id=cap_id,
                stub=False,
                require_approved=False,
            )
            return {"ok": True, "capability_id": cap_id, "runner": runner_name, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "capability_id": cap_id, "error": str(exc)[:500]}

    if action and action in ALLOWED_ACTIONS:
        try:
            from analyst_ledger.workflow_engine import WorkflowEngine

            # Minimal one-off via temporary allowlisted step list
            engine = WorkflowEngine(ledger)
            # Prefer note_digest-style compact: execute_action if available
            execute = getattr(engine, "_execute_action", None)
            if callable(execute):
                result = execute(action, spec={"watchlist": [], "description": objective}, stub=False)
                return {"ok": True, "capability_id": cap_id, "action": action, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "capability_id": cap_id, "error": str(exc)[:500]}

    if cap_id == "web_research" or cap_id == "public_web_search":
        brief = _retrieve_web_sources(objective, stub=stub)
        if brief:
            return {
                "ok": True,
                "capability_id": cap_id,
                "summary": brief[:1800],
            }
        return {
            "ok": False,
            "capability_id": cap_id,
            "error": "no_search_hits",
            "hint": "Web search returned no usable hits for that topic.",
        }

    return {
        "ok": False,
        "capability_id": cap_id,
        "error": "not_executable",
        "hint": "Capability is prompt-only or needs a Phase 2 script.",
    }


def _arxiv_search(query: str, *, limit: int = 6) -> List[Dict[str, Any]]:
    """Query the public arXiv Atom API (no key)."""
    import re
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET

    q = " ".join(str(query or "").split()).strip()
    if not q:
        return []
    limit = max(1, min(int(limit), 8))
    # Prefer all: terms; strip quotes that confuse arxiv
    q = q.replace('"', " ").strip()
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {
            "search_query": f"all:{q}",
            "start": 0,
            "max_results": limit,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "FlyleafGraphResearch/0.1 (+local)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001
        logger.debug("arxiv search failed: %s", exc)
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    hits: List[Dict[str, Any]] = []
    for entry in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", (entry.findtext("a:title", default="") or "")).strip()
        summary = re.sub(
            r"\s+", " ", (entry.findtext("a:summary", default="") or "")
        ).strip()[:400]
        published = (entry.findtext("a:published", default="") or "")[:10]
        link = ""
        for l in entry.findall("a:link", ns):
            if l.attrib.get("type") == "text/html" or l.attrib.get("rel") == "alternate":
                link = l.attrib.get("href") or ""
                break
        if not link:
            link = entry.findtext("a:id", default="") or ""
        if title and link:
            hits.append(
                {
                    "title": title,
                    "url": link,
                    "snippet": summary,
                    "published_at": published,
                    "source": "arxiv",
                }
            )
    return hits


def _web_search_query_from_focus(topic: str) -> str:
    """Turn a /graph guiding question into a Bing subject query.

    Agents still get the full focus (“make a ww2 history essay”). Search must
    not — Bing treats a leading “Make” as Make.com / GNU Make.
    """
    import re

    text = " ".join(str(topic or "").split()).strip()
    if not text:
        return ""
    text = re.sub(
        r"^(?:please\s+)?(?:help\s+me\s+)?(?:"
        r"make|write|create|draft|compose|build|do|prepare|produce|generate|"
        r"research|find|get|summarize|outline"
        r")\s+(?:me\s+)?(?:a|an|the|some|my)\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^(?:please\s+)?(?:help\s+me\s+)?(?:"
        r"make|write|create|draft|compose|build|do|prepare|produce|generate|"
        r"summarize|outline"
        r")\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^(?:a|an|the)\s+", "", text, flags=re.I)
    cleaned = " ".join(text.split()).strip()
    return cleaned or " ".join(str(topic or "").split()).strip()


def _topic_wants_cs_academic_search(focus: str) -> bool:
    import re

    return bool(
        re.search(
            r"\bRAG\b|retrieval[\s-]?augmented|LLM|large language model|"
            r"transformer|machine learning|deep learning|NeurIPS|arxiv|"
            r"\bnlp\b|computer vision|reinforcement learning",
            focus or "",
            re.I,
        )
    )


def _retrieve_web_sources(topic: str, *, stub: bool = False) -> str:
    """Public web search pack for Graph research steps (Bing RSS + optional page enrich)."""
    focus = " ".join(str(topic or "").split()).strip()
    if not focus:
        return ""
    if stub:
        return (
            f"(stub) Sources for “{focus[:80]}”:\n"
            "- Stub Paper A (2025) — https://example.com/a — Overview of methods.\n"
            "- Stub Paper B (2024) — https://example.com/b — Evaluation metrics."
        )
    try:
        from analyst_ledger.web_search import (
            bing_search,
            enrich_trusted_hits,
            format_hits_for_prompt,
            rank_search_hits,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("web_search import failed: %s", exc)
        return ""

    import re

    # Guiding question stays intact for agents; search uses the subject only.
    subject = _web_search_query_from_focus(focus)
    focus_q = re.sub(r"\b[Ss]urvey\b", "literature review", subject)
    cs_academic = _topic_wants_cs_academic_search(focus)
    if cs_academic:
        focus_q = re.sub(r"\b[Pp]apers?\b", "arxiv OR ACL OR NeurIPS paper", focus_q)

    queries: List[str] = []
    arxiv_queries: List[str] = []
    if re.search(
        r"\bRAG\b|retrieval[\s-]?augmented|retrieval augmented generation",
        focus,
        re.I,
    ):
        queries.extend(
            [
                '"retrieval-augmented generation" OR "retrieval augmented generation" arxiv 2024 OR 2025',
                "RAG LLM evaluation metrics faithfulness citation 2024 2025",
                "RAG failure modes hallucination multi-hop retrieval 2024 2025",
            ]
        )
        arxiv_queries.extend(
            [
                "retrieval augmented generation",
                "RAG evaluation faithfulness",
                "retrieval augmented generation hallucination",
            ]
        )

    queries.append(focus_q[:160])
    expanded = re.sub(r"\bww2\b", "World War II", focus_q, flags=re.I)
    expanded = re.sub(r"\bwwii\b", "World War II", expanded, flags=re.I)
    if expanded != focus_q:
        queries.append(expanded[:160])
    if cs_academic:
        short = focus_q[:90]
        queries.append(f"{short} arxiv OR ACL OR EMNLP OR NeurIPS 2024 OR 2025")
        if not arxiv_queries:
            arxiv_queries.append(
                re.sub(r"[^\w\s\-]", " ", subject)[:80].strip() or subject[:80]
            )
    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:5]

    skip_hosts = {
        "surveymonkey.com",
        "surveyjunkie.com",
        "topsurveys.com",
        "qualtrics.com",
        "swagbucks.com",
        "inboxdollars.com",
        "reddit.com",
        "merriam-webster.com",
        "dictionary.com",
        "vocabulary.com",
        "cambridge.org",
        "thesaurus.com",
    }
    software_make = bool(
        re.search(
            r"\bgnu\s+make\b|\bmakefile\b|make\.com|\bintegromat\b",
            focus,
            re.I,
        )
    )
    if not software_make:
        skip_hosts.update({"make.com", "us2.make.com", "eu1.make.com"})

    all_hits: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for aq in arxiv_queries[:3]:
        for hit in _arxiv_search(aq, limit=5):
            url = str(hit.get("url") or "")
            if url and url not in seen:
                seen.add(url)
                all_hits.append(hit)

    for q in queries:
        try:
            for hit in bing_search(q, limit=5):
                url = str(hit.get("url") or "")
                host = ""
                try:
                    from urllib.parse import urlparse

                    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
                except Exception:
                    host = ""
                if host and any(host == h or host.endswith("." + h) for h in skip_hosts):
                    continue
                title = str(hit.get("title") or "").lower()
                snippet = str(hit.get("snippet") or "").lower()
                if "surveymonkey" in title or "survey junkie" in title:
                    continue
                if not software_make and (
                    "gnu make" in title
                    or "make.com" in title
                    or ("make (software)" in title)
                    or (host.endswith("gnu.org") and "/software/make" in url.lower())
                ):
                    continue
                if re.search(r"\bRAG\b|retrieval[\s-]?augmented", focus, re.I):
                    blob = f"{title} {snippet}"
                    if (
                        "retrieval-augmented" not in blob
                        and "retrieval augmented" not in blob
                        and " rag " not in f" {blob} "
                    ):
                        if (
                            host.endswith("wikipedia.org")
                            or "definition" in title
                            or "meaning" in title
                        ):
                            continue
                if url and url not in seen:
                    seen.add(url)
                    all_hits.append(hit)
        except Exception as exc:  # noqa: BLE001
            logger.debug("bing_search failed q=%r: %s", q, exc)

    if not all_hits and cs_academic:
        for q in [
            "site:arxiv.org retrieval augmented generation",
            '"retrieval-augmented generation" evaluation',
        ]:
            try:
                for hit in bing_search(q, limit=5):
                    url = str(hit.get("url") or "")
                    if url and url not in seen:
                        seen.add(url)
                        all_hits.append(hit)
            except Exception:
                pass

    if not all_hits:
        return ""

    def _rank_key(hit: Dict[str, Any]) -> tuple:
        url = str(hit.get("url") or "").lower()
        title = str(hit.get("title") or "").lower()
        score = 0
        if "arxiv.org" in url:
            score += 5
        if any(
            k in url
            for k in ("acl anthology", "aclanthology", "neurips", "openreview", "ieee")
        ):
            score += 4
        if (
            "rag" in title
            or "retrieval-augmented" in title
            or "retrieval augmented" in title
        ):
            score += 3
        if re.search(r"\bww2\b|\bwwii\b|world war", focus, re.I):
            if any(k in url for k in ("wikipedia.org", "britannica.com", "history.com", "archives.gov")):
                score += 4
            if "world war" in title or "wwii" in title or "ww2" in title:
                score += 3
        return (-score, title)

    all_hits.sort(key=_rank_key)
    try:
        ranked = rank_search_hits(all_hits, intent="general")
        preferred = all_hits[:8]
        merged = preferred + [h for h in ranked if h not in preferred]
        trusted = enrich_trusted_hits(merged[:8], max_pages=2)
        text = format_hits_for_prompt(trusted)
    except Exception as exc:  # noqa: BLE001
        logger.debug("rank/enrich failed: %s", exc)
        lines = []
        for hit in all_hits[:8]:
            lines.append(
                f"- {hit.get('title') or 'Untitled'} — {hit.get('url') or ''} — "
                f"{(hit.get('snippet') or '')[:180]}"
            )
        text = "\n".join(lines)
    if text and subject and subject.casefold() != focus.casefold():
        text = f"(search subject: {subject})\n{text}"
    return (text or "").strip()[:4500]


def _layer_wants_retrieval(title: str, prompt: str, skills: Sequence[str]) -> bool:
    skill_set = {str(s).strip() for s in (skills or []) if str(s).strip()}
    if not (skill_set & {"web_research", "public_web_search"}):
        return False
    title_l = (title or "").lower()
    blob = f"{title} {prompt}".lower()
    # Scope / write / synthesize steps should use prior evidence, not re-search.
    if any(k in title_l for k in ("scope", "define", "goal", "rules", "essay", "write", "synthes", "edit")):
        return False
    if any(k in blob for k in ("research", "source", "gather", "findings", "filings", "scan", "collect")):
        return True
    return False


def _guard_allows(
    prompt: str,
    *,
    prior: Sequence[str],
    layer_goal: str,
    stub: bool,
) -> tuple[bool, str]:
    """Orchestrator judgment between layers. Empty prompt always advances."""
    text = (prompt or "").strip()
    if not text:
        return True, "No guard — advancing."
    if stub:
        return True, "Guard passed (stub)."
    try:
        from analyst_ledger.synthesize import call_chat_messages

        prior_block = "\n---\n".join(prior[-8:]) if prior else "(none)"
        reply = call_chat_messages(
            [
                {
                    "role": "user",
                    "content": (
                        "You are the Graph orchestrator. Decide whether the team may "
                        "advance to the next step.\n"
                        f"Guard question: {text}\n"
                        f"Layer goal: {layer_goal or '(none)'}\n"
                        f"Recent turns:\n{prior_block}\n\n"
                        "Answer with YES or NO on the first line, then one short sentence."
                    ),
                }
            ],
            max_tokens=120,
            system=(
                "Judge only from the provided turns. Never invent evidence. "
                "Retrieved sources and substantive write-ups grounded in them count "
                "as progress — prefer YES when the step deliverable exists and cites "
                "those sources (even if coverage is incomplete or sources are "
                "technical overviews plus clearly labeled background-knowledge citations). "
                "Answer NO only if the team produced no real deliverable or only complained "
                "about missing access without writing substance."
            ),
            temperature=0.1,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph guard judgment failed: %s", exc)
        return True, f"Guard skipped (judgment failed: {exc}). Advancing."
    first = (reply.splitlines() or [""])[0].strip().upper()
    ok = first.startswith("Y")
    return ok, reply[:500] or ("YES" if ok else "NO")


# Max times a rejected step re-runs before the Graph advances anyway.
_GRAPH_LAYER_MAX_ATTEMPTS = 3


def _run_graph_layers(
    *,
    store: Any,
    hub: Any,
    room: dict[str, Any],
    room_id: str,
    config: dict[str, Any],
    job: Any,
    stub: bool,
    loop: Any,
    owner_user_id: str,
    run_focus: str = "",
) -> None:
    """Walk Graph layers: assigned analysts speak, then guard judgment advances.

    ``run_focus`` is an optional per-run topic (e.g. from ``/graph NVDA AI demand``)
    layered on top of the saved room objective so a generic research Graph can be
    pointed at a specific question without editing the Graph.
    """
    import time

    from analyst_ledger.friend_personalities import resolve_specialists
    from messenger.specialist_room import _recent_work_brief, _room_guidance, _speak
    from messenger.tenancy import user_context

    graph = normalize_graph(config.get("graph"))
    layers = graph.get("layers") or []
    guards = graph.get("guards") or []
    objective = str(config.get("objective") or "").strip()
    focus = " ".join(str(run_focus or "").split()).strip()[:800]
    # Prefer the per-run focus so analysts ground on the user's topic.
    topic = focus or objective
    if not layers:
        _post(
            store,
            hub,
            room_id,
            "Harness",
            "Graph has no steps. Open Graph, add a layer, Save, then /graph <topic>.",
            loop=loop,
        )
        job.status = "completed"
        return

    brief = _recent_work_brief(owner_user_id)
    base_guidance = _room_guidance(room)
    prior: List[str] = []

    endpoint = None
    try:
        from messenger.model_link import registry as model_registry

        room_profile = config.get("model_profile_id")
        endpoint = model_registry().endpoint_for_call(
            owner_user_id,
            profile_id=str(room_profile).strip() or None if room_profile else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph model link lookup failed: %s", exc)

    from analyst_ledger.synthesize import use_llm_endpoint

    with user_context(owner_user_id), use_llm_endpoint(endpoint):
        n_steps = len(layers)
        step_bit = f"{n_steps} step{'s' if n_steps != 1 else ''}."
        if focus:
            start_msg = f"Graph started — Focus: {focus}. {step_bit}"
        elif objective:
            start_msg = f"Graph started — {objective}. {step_bit}"
        else:
            start_msg = f"Graph started. {step_bit}"
        _post(store, hub, room_id, "Harness", start_msg, loop=loop)

        for idx, layer in enumerate(layers):
            if job.stop_event.is_set():
                _post(store, hub, room_id, "Harness", "Graph stopped.", loop=loop)
                job.status = "stopped"
                return

            job.round_num = idx + 1
            title = str(layer.get("title") or f"Step {idx + 1}")
            prompt = str(layer.get("prompt") or "").strip()
            goal = str(layer.get("goal") or "").strip()
            members = layer.get("members") or []
            member_ids = [
                str(m.get("agent_id") or "").strip()
                for m in members
                if isinstance(m, dict) and str(m.get("agent_id") or "").strip()
            ]
            instructions_by_id = {
                str(m.get("agent_id")): str(m.get("instructions") or "").strip()
                for m in members
                if isinstance(m, dict) and str(m.get("agent_id") or "").strip()
            }

            if not member_ids:
                intro_bits = [f"**Layer {idx + 1}: {title}**"]
                if prompt:
                    intro_bits.append(prompt)
                if goal:
                    intro_bits.append(f"Done when: {goal}")
                intro_bits.append("_No analysts on this step — skipping._")
                _post(store, hub, room_id, "Harness", "\n".join(intro_bits), loop=loop)
                continue

            personalities = resolve_specialists(member_ids)
            if not personalities:
                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    f"Could not resolve analysts for “{title}”.",
                    loop=loop,
                )
                continue

            attempt = 0
            while True:
                attempt += 1
                if job.stop_event.is_set():
                    _post(store, hub, room_id, "Harness", "Graph stopped.", loop=loop)
                    job.status = "stopped"
                    return

                intro_bits = [f"**Layer {idx + 1}: {title}**"]
                if attempt > 1:
                    intro_bits[0] += f" (continue · try {attempt})"
                if focus:
                    intro_bits.append(f"Focus: {focus}")
                if prompt:
                    intro_bits.append(prompt)
                if goal:
                    intro_bits.append(f"Done when: {goal}")
                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    "\n".join(intro_bits),
                    loop=loop,
                    max_len=4000,
                )
                time.sleep(0.12)

                retrieved = ""
                skills = config.get("skills") or []
                if _layer_wants_retrieval(title, prompt, skills):
                    _post(
                        store,
                        hub,
                        room_id,
                        "Harness",
                        "Retrieving public sources for this step…",
                        loop=loop,
                    )
                    retrieved = _retrieve_web_sources(topic or title, stub=stub)
                    if retrieved:
                        src_post = f"**Retrieved sources**\n{retrieved}"
                        _post(
                            store,
                            hub,
                            room_id,
                            "Harness",
                            src_post,
                            loop=loop,
                            max_len=8000,
                        )
                        prior.append(f"Harness: {src_post[:1200]}")
                        job.posted += 1
                    else:
                        _post(
                            store,
                            hub,
                            room_id,
                            "Harness",
                            "No public search hits returned — write from clearly labeled background knowledge and note gaps.",
                            loop=loop,
                        )

                layer_bits = [
                    f"Current Graph step: {title}",
                    f"Step prompt: {prompt or '(none)'}",
                    f"Step goal: {goal or '(none)'}",
                    "Produce a substantive deliverable for this step (not a plan to research later).",
                    "If Retrieved sources are present, ground claims in them and cite titles/URLs. "
                    "Do not claim you lack web/API access when sources were provided.",
                    "If sources are tutorials/overviews rather than peer-reviewed papers, still write "
                    "the deliverable: synthesize those sources and clearly label any additional "
                    "background-knowledge citations (author/year/title). Never refuse to write.",
                    "Clearly label background knowledge vs retrieved evidence.",
                ]
                if focus:
                    layer_bits.insert(
                        0,
                        f"Run focus (apply this topic to the step): {focus}",
                    )
                if objective and focus and objective != focus:
                    layer_bits.append(f"Standing team objective: {objective}")
                if retrieved:
                    layer_bits.append(
                        "Retrieved sources for this step:\n" + retrieved[:3500]
                    )
                if attempt > 1:
                    layer_bits.append(
                        "The orchestrator rejected the previous pass — deepen or "
                        "fix the work against the guard feedback above."
                    )
                layer_guidance = (
                    (base_guidance + "\n" if base_guidance else "")
                    + "\n".join(layer_bits)
                )

                for personality in personalities:
                    if job.stop_event.is_set():
                        _post(
                            store, hub, room_id, "Harness", "Graph stopped.", loop=loop
                        )
                        job.status = "stopped"
                        return
                    member_extra = instructions_by_id.get(personality.id) or ""
                    guidance = layer_guidance
                    if member_extra:
                        guidance = (
                            guidance
                            + f"\nYour instructions for this step: {member_extra}"
                        )
                    body = _speak(
                        personality,
                        mode="present",
                        topic=topic or title,
                        brief=brief,
                        prior=prior,
                        stub=stub,
                        room_guidance=guidance,
                        max_tokens=1800,
                        word_budget=900,
                    )
                    _post(
                        store,
                        hub,
                        room_id,
                        personality.name,
                        body,
                        loop=loop,
                        max_len=8000,
                    )
                    prior.append(f"{personality.name}: {body}")
                    job.posted += 1
                    time.sleep(0.18)

                if idx >= len(layers) - 1:
                    break

                next_layer = layers[idx + 1]
                guard = next(
                    (
                        g
                        for g in guards
                        if g.get("from_layer_id") == layer.get("id")
                        and g.get("to_layer_id") == next_layer.get("id")
                    ),
                    None,
                )
                guard_prompt = str((guard or {}).get("prompt") or "").strip()
                if not guard_prompt:
                    break

                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    f"**Guard** — {guard_prompt}",
                    loop=loop,
                )
                ok, judgment = _guard_allows(
                    guard_prompt,
                    prior=prior,
                    layer_goal=goal,
                    stub=stub,
                )
                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    f"Orchestrator: {judgment}",
                    loop=loop,
                )
                prior.append(f"Orchestrator: {judgment}")
                job.posted += 1
                if ok:
                    break

                if attempt >= _GRAPH_LAYER_MAX_ATTEMPTS:
                    _post(
                        store,
                        hub,
                        room_id,
                        "Harness",
                        (
                            f"Guard still not satisfied after {attempt} tries on "
                            f"“{title}” — continuing to the next step."
                        ),
                        loop=loop,
                    )
                    break

                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    (
                        f"Guard not satisfied — continuing “{title}” "
                        f"(try {attempt + 1}/{_GRAPH_LAYER_MAX_ATTEMPTS})."
                    ),
                    loop=loop,
                )

        if job.status == "running":
            _post(store, hub, room_id, "Harness", "Graph finished.", loop=loop)
            job.status = "completed"


def start_workflow_job(
    *,
    store: Any,
    hub: Any,
    room: dict[str, Any],
    owner_user_id: str,
    stub: bool = False,
    loop: Any = None,
    continuous: bool = False,
    run_focus: str = "",
) -> Any:
    """Background job: run Graph layers when present, else capability loops.

    ``run_focus`` is an optional per-run topic from ``/graph <topic>`` (does not
    mutate the saved room objective).
    """
    import uuid

    from messenger.specialist_room import SpecialistJob

    room_id = str(room.get("room_id") or "")
    if not room_id:
        raise ValueError("room_id required")

    existing = job_registry().active_for_room(room_id)
    if existing:
        raise ValueError("A harness run is already active in this team.")

    config = _room_config(room)
    objective = str(config.get("objective") or "").strip()
    focus = " ".join(str(run_focus or "").split()).strip()[:800]
    graph = normalize_graph(config.get("graph"))
    has_graph = bool(graph.get("layers"))
    job = SpecialistJob(
        job_id="job_" + uuid.uuid4().hex[:12],
        room_id=room_id,
        action="graph" if has_graph else "workflow",
        topic=focus
        or objective
        or ("team graph" if has_graph else "team workflow"),
        continuous=bool(continuous) and not has_graph,
        rounds=len(graph["layers"]) if has_graph else (20 if continuous else 1),
    )
    job_registry().register(job)

    def work() -> None:
        from messenger.tenancy import user_context

        try:
            if has_graph:
                _run_graph_layers(
                    store=store,
                    hub=hub,
                    room=room,
                    room_id=room_id,
                    config=config,
                    job=job,
                    stub=stub,
                    loop=loop,
                    owner_user_id=owner_user_id,
                    run_focus=focus,
                )
                return

            with user_context(owner_user_id) as ledger:
                step_ids = _resolve_step_ids(config, room_id, ledger)
                if not step_ids:
                    _post(
                        store,
                        hub,
                        room_id,
                        "Harness",
                        "No Graph steps and no executable capabilities. "
                        "Open Graph, add steps (or an allowlist/loop), Save, then /graph <topic>.",
                        loop=loop,
                    )
                    job.status = "completed"
                    return

                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    (
                        f"Workflow started"
                        + (f" — Focus: {focus}" if focus else (f" — {objective}" if objective else ""))
                        + f". Steps: {', '.join(step_ids)}."
                    ),
                    loop=loop,
                )

                rounds = 0
                max_rounds = 20 if continuous else 1
                while rounds < max_rounds:
                    if job.stop_event.is_set():
                        _post(store, hub, room_id, "Harness", "Workflow stopped.", loop=loop)
                        job.status = "stopped"
                        return
                    rounds += 1
                    job.round_num = rounds
                    for cap_id in step_ids:
                        if job.stop_event.is_set():
                            _post(store, hub, room_id, "Harness", "Workflow stopped.", loop=loop)
                            job.status = "stopped"
                            return
                        outcome = _run_capability(
                            cap_id,
                            ledger=ledger,
                            stub=stub,
                            objective=focus or objective,
                        )
                        if outcome.get("ok"):
                            summary = outcome.get("summary")
                            if not summary:
                                compact = outcome.get("result")
                                try:
                                    summary = json.dumps(compact, ensure_ascii=False)[:900]
                                except Exception:
                                    summary = str(compact)[:900]
                            _post(
                                store,
                                hub,
                                room_id,
                                "Harness",
                                f"[{cap_id}] {summary}",
                                loop=loop,
                            )
                        else:
                            _post(
                                store,
                                hub,
                                room_id,
                                "Harness",
                                f"[{cap_id}] failed: {outcome.get('error') or 'unknown'}"
                                + (
                                    f" ({outcome.get('hint')})"
                                    if outcome.get("hint")
                                    else ""
                                ),
                            )
                        job.posted += 1
                    if not continuous:
                        break
                if job.status == "running":
                    _post(store, hub, room_id, "Harness", "Workflow finished.", loop=loop)
                    job.status = "completed"
        except Exception as exc:  # noqa: BLE001
            logger.exception("team harness failed room=%s", room_id)
            try:
                _post(
                    store,
                    hub,
                    room_id,
                    "Harness",
                    f"Workflow error: {exc}",
                    loop=loop,
                )
            except Exception:
                pass
            job.status = "failed"
            job.error = str(exc)[:500]

    threading.Thread(target=work, name=f"harness-{job.job_id}", daemon=True).start()
    return job


def draft_capability_from_description(
    description: str,
    *,
    ledger: Any = None,
    stub: bool = False,
) -> Dict[str, Any]:
    """Map a natural-language need onto allowlisted runners/actions; save as draft."""
    from analyst_ledger.orchestration import ALLOWED_ACTIONS, ALLOWED_RUNNERS, extract_json
    from analyst_ledger.registry import (
        Capability,
        get_capability,
        list_capabilities_public,
        save_user_capability,
    )
    from analyst_ledger.registry import _slug_id  # noqa: PLC0415 — shared slug helper

    text = (description or "").strip()
    if len(text) < 8:
        raise ValueError("description_too_short")

    catalog = []
    for c in list_capabilities_public(ledger=ledger):
        if c.get("kind") == "builtin" or c.get("approved"):
            catalog.append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "summary": c.get("summary"),
                    "runner": c.get("runner"),
                    "action": c.get("action"),
                    "executable": capability_is_executable(c),
                }
            )

    allow = {
        "runners": sorted(ALLOWED_RUNNERS),
        "actions": sorted(ALLOWED_ACTIONS),
        "capabilities": catalog[:40],
    }

    proposal: Optional[Dict[str, Any]] = None
    if stub:
        # Deterministic offline mapping for tests / no model
        lower = text.lower()
        if "filing" in lower or "sec" in lower:
            proposal = {
                "name": "Filings scan",
                "summary": text[:400],
                "capability_ids": ["sec_filings_check"],
                "runner": "sec_filings_check",
                "in_scope": True,
            }
        elif "note" in lower or "digest" in lower:
            proposal = {
                "name": "Note digest",
                "summary": text[:400],
                "capability_ids": ["note_digest"],
                "runner": "note_digest",
                "in_scope": True,
            }
        elif "quote" in lower or "price" in lower or "watchlist" in lower:
            proposal = {
                "name": "Watchlist scan",
                "summary": text[:400],
                "capability_ids": ["morning_yf_scan"],
                "runner": "morning_yf_scan",
                "in_scope": True,
            }
        else:
            proposal = {
                "name": "",
                "summary": text[:400],
                "capability_ids": [],
                "in_scope": False,
                "reason": "No allowlisted runner/action matches; needs Phase 2 custom script.",
            }
    else:
        prompt = (
            "You draft Flyleaf capabilities. Reply with JSON only:\n"
            '{"in_scope":bool,"name":str,"summary":str,"capability_ids":[str],'
            '"runner":str|null,"action":str|null,"reason":str}\n'
            "Use ONLY ids from the allowlist. If the need cannot be met "
            "(e.g. Alibaba scraping, flight APIs, cargo tracking), set "
            "in_scope=false and explain in reason.\n"
            f"Allowlist: {json.dumps(allow)}\n"
            f"User need: {text[:1500]}"
        )
        try:
            from analyst_ledger.synthesize import call_chat_messages

            raw = call_chat_messages(
                [{"role": "user", "content": prompt}],
                max_tokens=800,
            )
            proposal = extract_json(raw if isinstance(raw, str) else str(raw))
        except Exception as exc:  # noqa: BLE001
            # Fall back to stub heuristics if model unavailable
            logger.info("draft-from-prompt model failed: %s", exc)
            return draft_capability_from_description(text, ledger=ledger, stub=True)

    if not isinstance(proposal, dict):
        raise ValueError("invalid_draft_response")

    if not proposal.get("in_scope"):
        return {
            "ok": True,
            "in_scope": False,
            "needs_script": True,
            "reason": str(
                proposal.get("reason")
                or "Cannot satisfy with current allowlisted runners/actions."
            ),
            "description": text[:400],
        }

    name = str(proposal.get("name") or "").strip() or "Draft capability"
    summary = str(proposal.get("summary") or text)[:400]
    caps = [
        str(c).strip()
        for c in (proposal.get("capability_ids") or [])
        if str(c).strip()
    ]
    runner = str(proposal.get("runner") or "").strip() or None
    action = str(proposal.get("action") or "").strip() or None
    if runner and runner not in ALLOWED_RUNNERS:
        runner = None
    if action and action not in ALLOWED_ACTIONS:
        action = None
    # Validate capability ids against allowlist / builtins
    allowed_ids = {c["id"] for c in catalog if c.get("id")}
    caps = [c for c in caps if c in allowed_ids or c in ALLOWED_RUNNERS or c in ALLOWED_ACTIONS]
    if not caps and runner:
        caps = [runner]
    if not caps and action:
        caps = [action]
    if not caps:
        return {
            "ok": True,
            "in_scope": False,
            "needs_script": True,
            "reason": "Model proposed nothing on the allowlist.",
            "description": text[:400],
        }

    rid = _slug_id(name)
    if get_capability(rid) is not None:
        rid = _slug_id(f"{name}_{len(caps)}")
    cap = Capability(
        id=rid,
        name=name[:80],
        kind="user",
        summary=summary,
        invoke="approve first",
        schedulable=bool(runner),
        needs_model=False,
        runner=runner,
        action=action,
        approved=False,
        enabled=False,
        status="draft",
        ritual_id=rid,
        proposed_by="hire_describe",
        steps=tuple(caps[:12]),
    )
    saved = save_user_capability(cap)
    pub = enrich_capability_public(saved.to_public())
    return {
        "ok": True,
        "in_scope": True,
        "needs_script": False,
        "capability": pub,
        "mapped_from": caps[:12],
    }
