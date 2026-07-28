"""Master setup tools — create rooms, agents, workflows via the same registry/DB APIs as the website.

Master chat calls these allowlisted tools instead of free-form shell. Successful setups
are remembered (lightweight RAG) so later prompts can reuse patterns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from messenger.secrets_crypto import decrypt_secret, encrypt_secret, secret_suffix

logger = logging.getLogger(__name__)

KNOWN_NEED_KINDS = frozenset(
    {
        "github_repo",
        "github_token",
        "cursor_api_key",
        "openrouter_api_key",
        "anthropic_api_key",
        "other_secret",
    }
)

ALLOWED_TOOLS = frozenset(
    {
        "create_room",
        "hire_agent",
        "assign_agent",
        "configure_room",
        "set_workspace",
        "create_loop",
        "draft_capability",
        "list_catalog",
        "apply_recipe",
    }
)

_NAME_ALIASES = (
    "name",
    "title",
    "agent_name",
    "agent",
    "label",
    "display_name",
    "role_name",
)


def _pick_agent_name(args: dict) -> str:
    """Resolve hire_agent display name from common model aliases."""
    for key in _NAME_ALIASES:
        val = str(args.get(key) or "").strip()
        if val:
            return val[:80]
    for key in ("summary", "role", "stage", "description", "objective"):
        val = str(args.get(key) or "").strip()
        if not val:
            continue
        words = re.findall(r"[A-Za-z0-9]+", val)
        if words:
            return " ".join(words[:4])[:80]
    return ""


def _looks_like_display_name(ref: str) -> bool:
    s = (ref or "").strip()
    if not s or s.startswith("agent_"):
        return False
    if " " in s:
        return True
    if "_" not in s and any(c.isupper() for c in s[1:]):
        return True
    return bool(re.match(r"^[A-Z][a-z]+(?:[A-Z][a-z]+)+", s))


def _resolve_agent_id(agent_ref: str) -> Optional[str]:
    """Match catalog id, slug, mention, or display name."""
    from analyst_ledger.registry import get_agent, list_agents_public

    ref = (agent_ref or "").strip()
    if not ref:
        return None
    if get_agent(ref) is not None:
        return ref
    ref_slug = re.sub(r"[^a-z0-9_]+", "_", ref.casefold()).strip("_")
    ref_cf = ref.casefold().lstrip("@")
    for agent in list_agents_public():
        aid = str(agent.get("id") or "")
        if not aid:
            continue
        if aid == ref or aid == ref_slug:
            return aid
        name = str(agent.get("name") or "")
        mention = str(agent.get("mention") or "").lstrip("@")
        if name.casefold() == ref_cf or mention.casefold() == ref_cf:
            return aid
        if re.sub(r"[^a-z0-9]+", "", name.casefold()) == re.sub(
            r"[^a-z0-9]+", "", ref_cf
        ):
            return aid
    return None


def _registry_dir() -> Path:
    from analyst_ledger.paths import data_dir

    path = data_dir() / "registry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_memory_path() -> Path:
    return _registry_dir() / "setup_memory.jsonl"


def remember_setup(
    *,
    user_request: str,
    actions: Sequence[Dict[str, Any]],
    summary: str,
) -> None:
    """Append a successful setup pattern for later retrieval (RAG-lite)."""
    from analyst_ledger.schema import utc_now_iso

    row = {
        "ts": utc_now_iso(),
        "request": (user_request or "")[:800],
        "summary": (summary or "")[:800],
        "actions": list(actions)[:20],
        "tokens": _tokenize(f"{user_request} {summary}"),
    }
    path = setup_memory_path()
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.info("setup memory write failed: %s", exc)


def retrieve_setup_memory(query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Rank past setups by token overlap with the current request."""
    path = setup_memory_path()
    if not path.exists():
        return []
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        return []
    scored: List[tuple[float, Dict[str, Any]]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-200:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        tokens = set(row.get("tokens") or _tokenize(str(row.get("request") or "")))
        if not tokens:
            continue
        overlap = len(q_tokens & tokens) / max(1, len(q_tokens | tokens))
        if overlap <= 0:
            continue
        scored.append((overlap, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for score, row in scored[:limit]:
        out.append(
            {
                "score": round(score, 3),
                "request": row.get("request"),
                "summary": row.get("summary"),
                "actions": row.get("actions") or [],
            }
        )
    return out


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9_]{3,}", (text or "").lower())[:80]


def public_workspace(config: Optional[dict]) -> Dict[str, Any]:
    """Safe workspace view for API/UI — never returns decrypted secrets."""
    cfg = config if isinstance(config, dict) else {}
    ws = dict(cfg.get("workspace") or {}) if isinstance(cfg.get("workspace"), dict) else {}
    secrets_map = (
        cfg.get("workspace_secrets")
        if isinstance(cfg.get("workspace_secrets"), dict)
        else {}
    )
    needs_in = ws.get("needs") if isinstance(ws.get("needs"), list) else []
    needs_out = []
    for raw in needs_in[:16]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "other_secret").strip().lower()
        if kind not in KNOWN_NEED_KINDS:
            kind = "other_secret"
        need_id = str(raw.get("id") or kind).strip()[:64] or kind
        enc = secrets_map.get(need_id) or secrets_map.get(kind) or ""
        filled = bool(str(enc).strip())
        if kind == "github_repo":
            filled = bool(str(ws.get("repo_url") or "").strip())
        needs_out.append(
            {
                "id": need_id,
                "kind": kind,
                "label": str(raw.get("label") or kind.replace("_", " ")).strip()[:120],
                "hint": str(raw.get("hint") or "").strip()[:240],
                "filled": filled,
                "suffix": (
                    secret_suffix(decrypt_secret(str(enc)))
                    if filled and kind != "github_repo"
                    else ""
                ),
            }
        )
    return {
        "repo_url": str(ws.get("repo_url") or "").strip()[:400],
        "default_ref": str(ws.get("default_ref") or "main").strip()[:80] or "main",
        "notes": str(ws.get("notes") or "").strip()[:800],
        "needs": needs_out,
    }


def apply_workspace_patch(
    config: dict,
    *,
    repo_url: Optional[str] = None,
    default_ref: Optional[str] = None,
    notes: Optional[str] = None,
    needs: Optional[Sequence[dict]] = None,
    secrets: Optional[dict] = None,
) -> dict:
    """Merge workspace fields into room config. Encrypts secret values in place."""
    cfg = dict(config or {})
    ws = dict(cfg.get("workspace") or {}) if isinstance(cfg.get("workspace"), dict) else {}
    if repo_url is not None:
        ws["repo_url"] = str(repo_url or "").strip()[:400]
    if default_ref is not None:
        ws["default_ref"] = str(default_ref or "main").strip()[:80] or "main"
    if notes is not None:
        ws["notes"] = str(notes or "").strip()[:800]
    if needs is not None:
        cleaned = []
        for raw in list(needs)[:16]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "other_secret").strip().lower()
            if kind not in KNOWN_NEED_KINDS:
                kind = "other_secret"
            need_id = str(raw.get("id") or kind).strip()[:64] or kind
            cleaned.append(
                {
                    "id": need_id,
                    "kind": kind,
                    "label": str(raw.get("label") or kind.replace("_", " ")).strip()[:120],
                    "hint": str(raw.get("hint") or "").strip()[:240],
                }
            )
        ws["needs"] = cleaned
    cfg["workspace"] = ws

    if secrets is not None and isinstance(secrets, dict):
        enc_map = (
            dict(cfg.get("workspace_secrets") or {})
            if isinstance(cfg.get("workspace_secrets"), dict)
            else {}
        )
        for key, value in list(secrets.items())[:16]:
            kid = str(key).strip()[:64]
            if not kid:
                continue
            raw = str(value or "").strip()
            if not raw:
                enc_map.pop(kid, None)
                continue
            if raw.startswith("enc:v1:"):
                enc_map[kid] = raw
            else:
                enc_map[kid] = encrypt_secret(raw)
        cfg["workspace_secrets"] = enc_map
    return cfg


def catalog_snapshot(*, store: Any = None, user_id: Optional[str] = None) -> Dict[str, Any]:
    """What Master sees — same building blocks as Hire / Graph / Teams."""
    from analyst_ledger.registry import (
        list_agents_public,
        list_automations_public,
        list_capabilities_public,
        list_lenses_public,
    )

    agents = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "mention": a.get("mention"),
            "kind": a.get("kind"),
            "capabilities": a.get("capabilities") or a.get("capability_ids") or [],
            "summary": a.get("summary"),
        }
        for a in list_agents_public()
        if a.get("id") != "master"
    ][:60]
    caps = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "summary": c.get("summary"),
            "runner": c.get("runner"),
            "action": c.get("action"),
            "approved": c.get("approved"),
            "kind": c.get("kind"),
        }
        for c in list_capabilities_public()
    ][:60]
    lenses = [{"id": ln.get("id"), "name": ln.get("name")} for ln in list_lenses_public()][:40]
    loops = [
        {
            "id": a.get("ritual_id") or a.get("id") or a.get("name"),
            "name": a.get("name"),
            "approved": a.get("approved"),
            "enabled": a.get("enabled"),
            "capability_ids": a.get("capability_ids") or [],
        }
        for a in list_automations_public()
    ][:40]
    rooms: List[Dict[str, Any]] = []
    if store is not None and user_id:
        try:
            for room in store.list_rooms_for_user(user_id)[:40]:
                cfg = room.get("config") or {}
                rooms.append(
                    {
                        "room_id": room.get("room_id"),
                        "title": room.get("title"),
                        "kind": room.get("kind"),
                        "agents": (cfg.get("agents") or cfg.get("specialists") or [])[:12],
                        "skills": (cfg.get("skills") or [])[:12],
                        "orchestrator": cfg.get("orchestrator") or "chat",
                        "workspace": public_workspace(cfg),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("catalog rooms failed: %s", exc)
    from messenger.graph_recipes import list_recipes

    return {
        "agents": agents,
        "capabilities": caps,
        "lenses": lenses,
        "loops": loops,
        "rooms": rooms,
        "recipes": list_recipes(),
        "need_kinds": sorted(KNOWN_NEED_KINDS),
        "tools": sorted(ALLOWED_TOOLS),
    }


def execute_tool(
    name: str,
    args: Optional[dict],
    *,
    store: Any,
    user_id: str,
) -> Dict[str, Any]:
    """Run one allowlisted setup tool. Mirrors website registry/room APIs."""
    tool = str(name or "").strip()
    payload = args if isinstance(args, dict) else {}
    if tool not in ALLOWED_TOOLS:
        return {"ok": False, "error": f"unknown_tool:{tool}"}
    try:
        if tool == "list_catalog":
            return {"ok": True, "catalog": catalog_snapshot(store=store, user_id=user_id)}
        if tool == "create_room":
            return _tool_create_room(payload, store=store, user_id=user_id)
        if tool == "hire_agent":
            return _tool_hire_agent(payload)
        if tool == "assign_agent":
            return _tool_assign_agent(payload, store=store, user_id=user_id)
        if tool == "configure_room":
            return _tool_configure_room(payload, store=store, user_id=user_id)
        if tool == "set_workspace":
            return _tool_set_workspace(payload, store=store, user_id=user_id)
        if tool == "create_loop":
            return _tool_create_loop(payload)
        if tool == "draft_capability":
            return _tool_draft_capability(payload)
        if tool == "apply_recipe":
            return _tool_apply_recipe(payload, store=store, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("master tool %s failed: %s", tool, exc)
        return {"ok": False, "error": str(exc)[:400]}
    return {"ok": False, "error": "unhandled"}


def _owned_room(store: Any, room_id: str, user_id: str) -> Optional[dict]:
    room = store.room(room_id)
    if not room:
        return None
    if str(room.get("owner_user_id") or "") != str(user_id):
        return None
    return room


def _tool_create_room(args: dict, *, store: Any, user_id: str) -> Dict[str, Any]:
    title = str(args.get("title") or args.get("name") or "").strip()[:80]
    if not title:
        return {"ok": False, "error": "title_required"}
    objective = str(args.get("objective") or "").strip()[:800]
    orchestrator = str(args.get("orchestrator") or "chat").strip().lower()
    if orchestrator not in {"chat", "debate", "workflow"}:
        orchestrator = "chat"
    agents = [
        str(a).strip()
        for a in (args.get("agents") or args.get("agent_ids") or [])
        if str(a).strip()
    ][:24]
    skills = [str(s).strip() for s in (args.get("skills") or []) if str(s).strip()][:20]
    roles_raw = args.get("roles") if isinstance(args.get("roles"), dict) else {}
    roles = {
        str(k).strip(): str(v or "").strip()[:80]
        for k, v in list(roles_raw.items())[:24]
        if str(k).strip()
    }
    prompts = args.get("prompts")
    if isinstance(prompts, str):
        prompt_list = [ln.strip() for ln in prompts.splitlines() if ln.strip()][:12]
    elif isinstance(prompts, list):
        prompt_list = [str(p).strip() for p in prompts if str(p).strip()][:12]
    else:
        prompt_list = []

    config: Dict[str, Any] = {
        "objective": objective,
        "prompts": prompt_list,
        "orchestrator": orchestrator,
        "agents": agents,
        "specialists": agents,
        "skills": skills,
        "roles": roles,
    }
    if args.get("repo_url") or args.get("needs"):
        config = apply_workspace_patch(
            config,
            repo_url=args.get("repo_url"),
            default_ref=args.get("default_ref"),
            notes=args.get("workspace_notes"),
            needs=args.get("needs") if isinstance(args.get("needs"), list) else None,
        )

    room_id = secrets.token_urlsafe(9)
    invite = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(invite.encode("utf-8")).hexdigest()
    store.create_room(
        room_id,
        title,
        token_hash,
        owner_user_id=user_id,
        kind="people",
        config=config,
    )
    return {
        "ok": True,
        "room_id": room_id,
        "title": title,
        "workspace": public_workspace(config),
        "message": (
            f"Created team “{title}”. Open it from Teams; fill Room settings "
            "for any repo URL or API keys Master requested."
        ),
    }


def _tool_hire_agent(args: dict) -> Dict[str, Any]:
    from analyst_ledger.registry import create_composed_agent, get_agent

    name = _pick_agent_name(args)
    if not name:
        return {"ok": False, "error": "name_required"}
    existing_id = _resolve_agent_id(name)
    if existing_id and get_agent(existing_id) is not None:
        agent = get_agent(existing_id)
        return {
            "ok": True,
            "agent_id": existing_id,
            "name": agent.name if agent else name,
            "mention": agent.mention if agent else f"@{name.replace(' ', '')}",
            "capabilities": list(agent.capabilities) if agent else [],
            "message": f"Agent {existing_id} already exists — reusing it.",
            "reused": True,
        }
    lenses = [str(x).strip() for x in (args.get("lens_ids") or []) if str(x).strip()][:12]
    caps = [
        str(x).strip() for x in (args.get("capability_ids") or []) if str(x).strip()
    ][:12]
    prompt = str(args.get("prompt") or "").strip()[:2000]
    summary = str(args.get("summary") or "").strip()[:240]
    memory_hint = str(args.get("memory_hint") or "").strip()[:600]
    if memory_hint and memory_hint not in prompt:
        prompt = (prompt + "\n\n# Learned from past setups\n" + memory_hint).strip()
    agent = create_composed_agent(
        name=name,
        lens_ids=lenses,
        capability_ids=caps,
        prompt=prompt,
        summary=summary,
        stage=str(args.get("stage") or "").strip(),
        skills=args.get("skills") or (),
    )
    return {
        "ok": True,
        "agent_id": agent.id,
        "name": agent.name,
        "mention": agent.mention,
        "capabilities": list(agent.capabilities),
        "message": f"Hired {agent.mention}. Assign it to a team or ask Master to assign.",
    }


def _tool_assign_agent(args: dict, *, store: Any, user_id: str) -> Dict[str, Any]:
    room_id = str(args.get("room_id") or "").strip()
    agent_ref = str(
        args.get("agent_id")
        or args.get("agent")
        or args.get("agent_name")
        or args.get("name")
        or ""
    ).strip()
    role = str(args.get("role") or args.get("label") or "").strip()[:80]
    room = _owned_room(store, room_id, user_id)
    if not room:
        return {"ok": False, "error": "room_not_found_or_forbidden"}
    if not agent_ref:
        return {"ok": False, "error": "agent_id_required"}
    agent_id = _resolve_agent_id(agent_ref)
    if not agent_id and _looks_like_display_name(agent_ref):
        hired = _tool_hire_agent(
            {
                "name": agent_ref,
                "capability_ids": args.get("capability_ids") or [],
                "lens_ids": args.get("lens_ids") or [],
                "prompt": args.get("prompt")
                or f"You are {agent_ref}."
                + (f" Role: {role}." if role else ""),
                "summary": str(args.get("summary") or role or agent_ref)[:240],
                "stage": args.get("stage") or role,
            }
        )
        if not hired.get("ok"):
            return hired
        agent_id = str(hired.get("agent_id") or "")
    elif not agent_id:
        agent_id = agent_ref
    config = dict(room.get("config") or {})
    agents = list(config.get("agents") or config.get("specialists") or [])
    if agent_id not in agents:
        agents.append(agent_id)
    config["agents"] = agents[:24]
    config["specialists"] = list(config["agents"])
    roles = dict(config.get("roles") or {})
    if role:
        roles[agent_id] = role
    config["roles"] = roles
    store.update_room_config(room_id, config)
    return {
        "ok": True,
        "room_id": room_id,
        "agent_id": agent_id,
        "role": role,
        "message": f"Assigned {agent_id} to room {room_id}.",
    }


def _tool_configure_room(args: dict, *, store: Any, user_id: str) -> Dict[str, Any]:
    room_id = str(args.get("room_id") or "").strip()
    room = _owned_room(store, room_id, user_id)
    if not room:
        return {"ok": False, "error": "room_not_found_or_forbidden"}
    config = dict(room.get("config") or {})
    if "objective" in args:
        config["objective"] = str(args.get("objective") or "").strip()[:800]
    if "orchestrator" in args:
        mode = str(args.get("orchestrator") or "chat").strip().lower()
        if mode in {"chat", "debate", "workflow"}:
            config["orchestrator"] = mode
    if "skills" in args and isinstance(args.get("skills"), list):
        config["skills"] = [str(s).strip() for s in args["skills"] if str(s).strip()][:20]
    if "prompts" in args:
        raw = args.get("prompts")
        if isinstance(raw, str):
            config["prompts"] = [ln.strip() for ln in raw.splitlines() if ln.strip()][:12]
        elif isinstance(raw, list):
            config["prompts"] = [str(p).strip() for p in raw if str(p).strip()][:12]
    if "roles" in args and isinstance(args.get("roles"), dict):
        roles = {
            str(k).strip(): str(v or "").strip()[:80]
            for k, v in list(args["roles"].items())[:24]
            if str(k).strip()
        }
        config["roles"] = roles
    if "agents" in args and isinstance(args.get("agents"), list):
        agents = [str(a).strip() for a in args["agents"] if str(a).strip()][:24]
        config["agents"] = agents
        config["specialists"] = agents
    store.update_room_config(room_id, config)
    return {
        "ok": True,
        "room_id": room_id,
        "config_keys": sorted(config.keys()),
        "message": f"Updated Graph config for room {room_id}.",
    }


def _tool_set_workspace(args: dict, *, store: Any, user_id: str) -> Dict[str, Any]:
    room_id = str(args.get("room_id") or "").strip()
    room = _owned_room(store, room_id, user_id)
    if not room:
        return {"ok": False, "error": "room_not_found_or_forbidden"}
    config = apply_workspace_patch(
        dict(room.get("config") or {}),
        repo_url=args.get("repo_url"),
        default_ref=args.get("default_ref"),
        notes=args.get("notes"),
        needs=args.get("needs") if isinstance(args.get("needs"), list) else None,
        secrets=None,
    )
    store.update_room_config(room_id, config)
    ws = public_workspace(config)
    unfilled = [n for n in ws.get("needs") or [] if not n.get("filled")]
    return {
        "ok": True,
        "room_id": room_id,
        "workspace": ws,
        "needs_user_input": unfilled,
        "message": (
            "Workspace updated. Open the team → Graph → Room settings to paste "
            "the GitHub repo URL and any API keys listed there."
            if unfilled or ws.get("repo_url")
            else "Workspace updated."
        ),
    }


def _tool_apply_recipe(args: dict, *, store: Any, user_id: str) -> Dict[str, Any]:
    """Match or load a recipe, hire agents, write Team Graph onto a room."""
    from messenger.graph_recipes import (
        build_graph_from_recipe,
        get_recipe,
        match_recipe,
        summarize_graph,
    )
    from messenger.team_harness import normalize_graph

    recipe_id = str(args.get("recipe_id") or args.get("recipe") or "").strip()
    query = str(args.get("query") or args.get("message") or args.get("prompt") or "").strip()
    recipe = get_recipe(recipe_id) if recipe_id else None
    if recipe is None and query:
        recipe = match_recipe(query)
    if recipe is None and recipe_id:
        return {"ok": False, "error": f"unknown_recipe:{recipe_id}"}
    if recipe is None:
        from messenger.graph_recipes import list_recipes

        return {
            "ok": False,
            "error": "recipe_id_or_query_required",
            "recipes": [r["id"] for r in list_recipes()],
        }

    edits = args.get("edits") if isinstance(args.get("edits"), dict) else {}
    disable_steps = edits.get("disable_steps") if isinstance(edits.get("disable_steps"), list) else []
    guard_overrides = (
        edits.get("guard_overrides")
        if isinstance(edits.get("guard_overrides"), dict)
        else {}
    )
    agent_overrides = (
        edits.get("agent_overrides")
        if isinstance(edits.get("agent_overrides"), dict)
        else {}
    )

    role_to_agent: Dict[str, str] = {}
    hired: List[Dict[str, Any]] = []
    for agent_spec in recipe.get("agents") or []:
        if not isinstance(agent_spec, dict):
            continue
        role = str(agent_spec.get("role") or "").strip()
        override_name = str(agent_overrides.get(role) or "").strip() if role else ""
        hire_args = {
            "name": override_name or agent_spec.get("name"),
            "capability_ids": list(agent_spec.get("capability_ids") or []),
            "lens_ids": list(agent_spec.get("lens_ids") or []),
            "prompt": agent_spec.get("prompt") or "",
            "summary": agent_spec.get("summary") or "",
            "stage": agent_spec.get("stage") or "",
        }
        hired_result = _tool_hire_agent(hire_args)
        if not hired_result.get("ok"):
            return {
                "ok": False,
                "error": hired_result.get("error") or "hire_failed",
                "role": role,
                "recipe_id": recipe["id"],
            }
        agent_id = str(hired_result.get("agent_id") or "")
        if role and agent_id:
            role_to_agent[role] = agent_id
        hired.append(
            {
                "role": role,
                "agent_id": agent_id,
                "name": hired_result.get("name"),
                "reused": bool(hired_result.get("reused")),
            }
        )

    graph = normalize_graph(
        build_graph_from_recipe(
            recipe,
            role_to_agent=role_to_agent,
            disable_steps=disable_steps,
            guard_overrides=guard_overrides,
        )
    )
    empty_steps = [l for l in graph.get("layers") or [] if not (l.get("members") or [])]
    if not (graph.get("layers") or []):
        return {"ok": False, "error": "recipe_produced_empty_graph", "recipe_id": recipe["id"]}

    room_id = str(args.get("room_id") or "").strip()
    title = str(args.get("title") or "").strip()[:80] or str(recipe.get("title") or recipe["name"])[:80]
    objective = (
        str(args.get("objective") or "").strip()[:800]
        or str(recipe.get("objective") or "")[:800]
    )
    created_new = False
    if room_id:
        room = _owned_room(store, room_id, user_id)
        if not room:
            return {"ok": False, "error": "room_not_found_or_forbidden"}
    else:
        create_args: Dict[str, Any] = {
            "title": title,
            "objective": objective,
            "orchestrator": "workflow",
            "skills": list(recipe.get("skills") or []),
            "agents": list(role_to_agent.values()),
            "roles": {
                aid: role for role, aid in role_to_agent.items() if aid and role
            },
        }
        needs = recipe.get("workspace_needs")
        if needs:
            create_args["needs"] = needs
            create_args["workspace_notes"] = recipe.get("workspace_notes") or ""
        created = _tool_create_room(create_args, store=store, user_id=user_id)
        if not created.get("ok"):
            return created
        room_id = str(created.get("room_id") or "")
        created_new = True
        room = _owned_room(store, room_id, user_id)
        if not room:
            return {"ok": False, "error": "room_create_inconsistent"}

    config = dict(room.get("config") or {})
    config["objective"] = objective or config.get("objective") or ""
    config["orchestrator"] = "workflow"
    if recipe.get("skills"):
        config["skills"] = list(recipe.get("skills") or [])[:20]
    agents = list(config.get("agents") or config.get("specialists") or [])
    for aid in role_to_agent.values():
        if aid and aid not in agents:
            agents.append(aid)
    config["agents"] = agents[:24]
    config["specialists"] = list(config["agents"])
    roles = dict(config.get("roles") or {})
    for role, aid in role_to_agent.items():
        if aid and role:
            roles[aid] = role
    config["roles"] = roles
    config["graph"] = graph
    if recipe.get("workspace_needs") and not (config.get("workspace") or {}).get("needs"):
        config = apply_workspace_patch(
            config,
            notes=recipe.get("workspace_notes"),
            needs=recipe.get("workspace_needs"),
        )
    store.update_room_config(room_id, config)

    loop_result = None
    loop_spec = recipe.get("create_loop")
    if isinstance(loop_spec, dict) and loop_spec.get("name"):
        loop_result = _tool_create_loop(
            {
                **loop_spec,
                "room_id": room_id,
                "transcript": f"From recipe {recipe['id']}",
            }
        )

    map_summary = summarize_graph(graph)
    warn = ""
    if empty_steps:
        warn = f" Warning: {len(empty_steps)} step(s) have no analysts."
    action = "Created" if created_new else "Updated"
    return {
        "ok": True,
        "recipe_id": recipe["id"],
        "recipe_name": recipe.get("name"),
        "room_id": room_id,
        "title": title,
        "created_room": created_new,
        "agents": hired,
        "graph": graph,
        "graph_summary": map_summary,
        "loop": loop_result,
        "workspace": public_workspace(config),
        "message": (
            f"{action} team “{title}” with recipe “{recipe.get('name')}”: {map_summary}."
            f"{warn} Open Graph to tweak steps/guards; fill Room settings for any secrets."
        ),
    }


def _tool_create_loop(args: dict) -> Dict[str, Any]:
    from analyst_ledger.registry import create_automation_from_chat

    name = str(args.get("name") or "").strip()
    caps = [
        str(c).strip()
        for c in (args.get("capability_ids") or args.get("steps") or [])
        if str(c).strip()
    ]
    if not name or not caps:
        return {"ok": False, "error": "name_and_capability_ids_required"}
    spec = create_automation_from_chat(
        name=name,
        capability_ids=caps,
        schedule=str(args.get("schedule") or "").strip() or None,
        room_id=str(args.get("room_id") or "").strip() or None,
        transcript=str(args.get("transcript") or "")[:2000],
        watchlist=args.get("watchlist") if isinstance(args.get("watchlist"), list) else None,
    )
    return {
        "ok": True,
        "ritual_id": spec.get("name") or name,
        "approved": False,
        "capability_ids": caps,
        "message": (
            f"Draft loop “{spec.get('name') or name}” created (not approved). "
            "Approve it before scheduled runs."
        ),
    }


def _tool_draft_capability(args: dict) -> Dict[str, Any]:
    from messenger.team_harness import draft_capability_from_description

    description = str(args.get("description") or args.get("text") or "").strip()
    if len(description) < 8:
        return {"ok": False, "error": "description_too_short"}
    result = draft_capability_from_description(description, stub=bool(args.get("stub")))
    return {"ok": True, **result}


def stub_plan_from_message(message: str) -> Dict[str, Any]:
    """Deterministic offline plan for tests / no-model fallback."""
    from messenger.graph_recipes import match_recipe

    lower = (message or "").lower()
    recipe = match_recipe(message)
    wants_room = any(
        w in lower for w in ("room", "team", "create", "set up", "setup", "build", "graph")
    )

    if recipe and (wants_room or recipe["id"] in {"equity_research", "pr_security", "filings_digest"}):
        return {
            "reply": (
                f"I'll apply the “{recipe['name']}” recipe: hire specialists, "
                "create the team, and map Graph steps from the library."
            ),
            "tools": [
                {
                    "name": "apply_recipe",
                    "args": {
                        "recipe_id": recipe["id"],
                        "query": message[:400],
                        "objective": message[:400],
                    },
                }
            ],
            "run_workflows": [],
        }

    # Legacy fallback when no recipe matches but user still wants a bare room
    if wants_room:
        title = "New team"
        return {
            "reply": f"I'll create a “{title}” team. Open Graph to map steps, or ask for a recipe.",
            "tools": [
                {
                    "name": "create_room",
                    "args": {
                        "title": title,
                        "objective": message[:400],
                        "orchestrator": "chat",
                    },
                }
            ],
            "run_workflows": [],
        }

    return {
        "reply": (
            "I'm Master — I set up Teams from Graph recipes, hire agents, and draft "
            "workflow loops using the same Hire/Graph pieces as the website.\n\n"
            "Try: “Create a research equities room”, “Create a coding room that reviews "
            "GitHub PRs then scans for exploits”, or “Set up a finance team for SEC filings.”\n\n"
            "After I create a room, open it → Graph → Room settings to paste "
            "repo URLs and API keys."
        ),
        "tools": [],
        "run_workflows": [],
    }
