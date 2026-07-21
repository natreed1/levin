"""Optional Qwen participant in the Friend messenger room.

Enabled from the Chats → Qwen tab. While enabled, messages that mention
``@Qwen`` get a reply posted to the cloud room as author ``Qwen``, using the
local OpenAI-compatible endpoint (``ANALYST_QWEN_*``).

``@Qwen research …`` (also look up / search / dig into) starts a background
web-search job; the Friend UI sidebar polls ``qwen_status()`` for progress.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .friend_personalities import (
    DEFAULT_PERSONALITY,
    MENTION_RE,
    PERSONALITIES,
    PERSONALITIES_BY_ID,
    FriendPersonality,
    match_personality,
    strip_personality_mentions,
)
from .finance_research import (
    build_outlook_evidence,
    classify_finance_intent,
    format_outlook_evidence,
    render_outlook_brief,
    resolve_symbol,
)
from .messenger_bridge import (
    MessengerBridgeError,
    _opener_for,
    _request,
    _save_jar,
    ensure_session_as,
    list_bot_rooms,
    list_raw_messages,
    list_room_messages,
    messenger_configured,
)
from .paths import data_dir
from .synthesize import _call_openai_compatible_messages
from .web_search import (
    bing_search,
    build_financial_brief,
    enrich_trusted_hits,
    finance_search_queries,
    format_financial_context,
    format_hits_for_prompt,
    rank_search_hits,
)

QWEN_THREAD_ID = "qwen"
QWEN_NAME = "Qwen"
RESEARCH_INTENT_RE = re.compile(
    r"(?<!\w)(?:"
    r"research|search|google|look\s+up|look\s+into|look\s+for|"
    r"dig\s+into|dig\s+up|find\s+out|pull\s+up|"
    r"what'?s\s+the\s+latest|latest\s+on|any\s+(?:recent\s+)?news"
    r")\b",
    re.IGNORECASE,
)
# Softer action verbs that only imply research when paired with a current-info
# noun (e.g. "find the filings", "verify their margins", "check recent news").
_RESEARCH_ACTION_RE = re.compile(
    r"(?<!\w)(?:"
    r"find|verify|confirm|fact[-\s]?check|double[-\s]?check|check|"
    r"pull|fetch|get|see\s+if|tell\s+me|show\s+me"
    r")\b",
    re.IGNORECASE,
)
_CURRENT_INFO_RE = re.compile(
    r"\b(?:latest|recent|current|today|this\s+(?:week|month|quarter|year)|"
    r"news|filing|filings|10-?k|10-?q|8-?k|sec|earnings|results|guidance|"
    r"revenue|margin|margins|profit|profits|valuation|price|shares|report|"
    r"reports|announce|announced|update|updates|outlook|forecast)\b",
    re.IGNORECASE,
)
# Follow-ups that ask the bot to carry out a just-stated request.
_FOLLOWUP_DO_RE = re.compile(
    r"(?<!\w)(?:do\s+(?:this|that|it|so|the\s+same)|go\s+ahead|please\s+do|"
    r"can\s+you\s+do\s+(?:this|that|it)|same|as\s+well|you\s+too)\b",
    re.IGNORECASE,
)
CONTEXT_REFERENCE_RE = re.compile(
    r"\b(?:this|that|it|above|preceding|same|more)\b", re.IGNORECASE
)
STRUCTURED_FINANCE_RE = re.compile(
    r"\b(?:price|revenue|eps|quarter|financial|valuation|52[- ]week|"
    r"market\s+snapshot)\b",
    re.IGNORECASE,
)
_TICK_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
# A research job runs in a daemon thread; if it takes longer than this (or the
# process restarted mid-run) the "researching" flag is treated as stale so new
# requests are not blocked forever.
STALE_RESEARCH_SECONDS = 240.0


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean_model_reply(
    reply: str, personality: FriendPersonality = DEFAULT_PERSONALITY
) -> str:
    """Remove model-added speaker labels and mentions from the start of replies."""
    value = (reply or "").strip()
    prefixes = [
        personality.mention,
        personality.name + ":",
        personality.mention + ":",
    ]
    changed = True
    while changed and value:
        changed = False
        for prefix in prefixes:
            if value.casefold().startswith(prefix.casefold()):
                value = value[len(prefix) :].lstrip(" :—-\n")
                changed = True
    return value


def qwen_thread_meta() -> Dict[str, Any]:
    st = load_state()
    return {
        "session_id": QWEN_THREAD_ID,
        "title": "Qwen",
        "desk_tag": "chat:qwen",
        "ritual_id": None,
        "master": False,
        "friend": False,
        "qwen": True,
        "in_conversation": bool(st.get("enabled")),
        "started_at": None,
    }


def _state_path() -> Path:
    path = data_dir() / "friend_qwen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


_ROOM_FIELDS = (
    "last_replied_id",
    "research_status",
    "research_progress",
    "research_query",
    "research_started_at",
    "research_trigger_id",
    "research_error",
)


def _default_room_state() -> Dict[str, Any]:
    return {
        "last_replied_id": 0,
        "research_status": "idle",
        "research_progress": "",
        "research_query": "",
        "research_started_at": None,
        "research_trigger_id": None,
        "research_error": "",
    }


def _normalize_room_state(data: Dict[str, Any]) -> Dict[str, Any]:
    status = str(data.get("research_status") or "idle")
    if status not in {"idle", "researching", "failed"}:
        status = "idle"
    return {
        "last_replied_id": int(data.get("last_replied_id") or 0),
        "research_status": status,
        "research_progress": str(data.get("research_progress") or ""),
        "research_query": str(data.get("research_query") or ""),
        "research_started_at": data.get("research_started_at"),
        "research_trigger_id": data.get("research_trigger_id"),
        "research_error": str(data.get("research_error") or ""),
    }


def _default_state() -> Dict[str, Any]:
    # Legacy room progress lives at the top level for backward compatibility;
    # created rooms live under ``rooms``.
    return {
        "enabled": False,
        **_default_room_state(),
        "rooms": {},
        "last_finance_symbol": None,
        "last_finance_intent": None,
    }


def load_state() -> Dict[str, Any]:
    path = _state_path()
    base = _default_state()
    if not path.exists():
        return dict(base)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(base)
    if not isinstance(data, dict):
        return dict(base)
    rooms: Dict[str, Any] = {}
    for rid, rstate in (data.get("rooms") or {}).items():
        if isinstance(rstate, dict):
            rooms[str(rid)] = _normalize_room_state(rstate)
    legacy = _normalize_room_state(data)
    return {
        "enabled": bool(data.get("enabled")),
        **legacy,
        "rooms": rooms,
        "last_finance_symbol": data.get("last_finance_symbol"),
        "last_finance_intent": data.get("last_finance_intent"),
    }


def save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    rooms: Dict[str, Any] = {}
    for rid, rstate in (state.get("rooms") or {}).items():
        if isinstance(rstate, dict):
            rooms[str(rid)] = _normalize_room_state(rstate)
    payload = {
        "enabled": bool(state.get("enabled")),
        **_normalize_room_state(state),
        "rooms": rooms,
        "last_finance_symbol": state.get("last_finance_symbol"),
        "last_finance_intent": state.get("last_finance_intent"),
    }
    with _STATE_LOCK:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_raw_locked() -> Dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return raw


def room_progress(state: Dict[str, Any], room_id: str) -> Dict[str, Any]:
    """Per-room progress from a loaded state dict."""
    if room_id == "legacy":
        return _normalize_room_state(state)
    room = (state.get("rooms") or {}).get(room_id)
    return _normalize_room_state(room or {})


def _write_raw_locked(raw: Dict[str, Any]) -> None:
    rooms: Dict[str, Any] = {}
    for rid, rstate in (raw.get("rooms") or {}).items():
        if isinstance(rstate, dict):
            rooms[str(rid)] = _normalize_room_state(rstate)
    payload = {
        "enabled": bool(raw.get("enabled")),
        **_normalize_room_state(raw),
        "rooms": rooms,
        "last_finance_symbol": raw.get("last_finance_symbol"),
        "last_finance_intent": raw.get("last_finance_intent"),
    }
    _state_path().write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _update_room(room_id: str, **fields: Any) -> Dict[str, Any]:
    """Merge per-room progress fields into persisted state (thread-safe)."""
    updates = {k: v for k, v in fields.items() if k in _ROOM_FIELDS}
    with _STATE_LOCK:
        raw = _read_raw_locked()
        if room_id == "legacy":
            current = _normalize_room_state(raw)
            current.update(updates)
            raw.update(current)
        else:
            rooms = raw.get("rooms")
            if not isinstance(rooms, dict):
                rooms = {}
            current = _normalize_room_state(rooms.get(room_id) or {})
            current.update(updates)
            rooms[room_id] = current
            raw["rooms"] = rooms
        _write_raw_locked(raw)
        return dict(current)


def _update_research(room_id: str = "legacy", **fields: Any) -> Dict[str, Any]:
    """Backward-compatible research updater; defaults to the legacy room."""
    return _update_room(room_id, **fields)


def reset_inflight_research() -> None:
    """Clear any ``researching`` flags left over from a prior process.

    Research runs in daemon threads that die with the process, so on startup any
    persisted ``researching`` status is stale and would otherwise block new
    requests until the timeout elapses.
    """
    st = load_state()
    for room_id in ["legacy", *list(st.get("rooms") or {})]:
        prog = room_progress(st, room_id)
        if (prog.get("research_status") or "idle") == "researching":
            _update_room(
                room_id,
                research_status="idle",
                research_progress="",
                research_started_at=None,
                research_trigger_id=None,
            )


def _active_research_view(state: Dict[str, Any]) -> Dict[str, Any]:
    """Surface the most relevant research activity across all watched rooms.

    Prefers a room that is actively researching, then a failed one, else legacy.
    """
    candidates: List[tuple[str, Dict[str, Any]]] = [
        ("legacy", room_progress(state, "legacy"))
    ]
    for rid in (state.get("rooms") or {}):
        candidates.append((rid, room_progress(state, rid)))

    def _rank(item: tuple[str, Dict[str, Any]]) -> tuple[int, float]:
        _rid, prog = item
        status = prog.get("research_status") or "idle"
        weight = {"researching": 2, "failed": 1, "idle": 0}.get(status, 0)
        started = prog.get("research_started_at") or 0
        return (weight, float(started or 0))

    _rid, prog = max(candidates, key=_rank)
    return {
        "research_status": prog.get("research_status") or "idle",
        "research_progress": prog.get("research_progress") or "",
        "research_query": prog.get("research_query") or "",
        "research_started_at": prog.get("research_started_at"),
        "research_trigger_id": prog.get("research_trigger_id"),
        "research_error": prog.get("research_error") or "",
        "research_room_id": _rid,
    }


def qwen_endpoint_info() -> Dict[str, str]:
    base = (
        os.environ.get("ANALYST_QWEN_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "http://127.0.0.1:11434/v1"
    ).rstrip("/")
    model = (
        os.environ.get("ANALYST_QWEN_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "qwen3:8b"
    ).strip()
    if model.lower() in {"qwen2.5:7b", "qwen2.5-7b"}:
        model = "qwen3:8b"
    return {"base_url": base, "model": model}


def probe_qwen_endpoint() -> Dict[str, Any]:
    """Lightweight check that the local OpenAI-compatible server answers."""
    info = qwen_endpoint_info()
    url = f"{info['base_url']}/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        body = json.loads(raw) if raw else {}
        ids = [
            str(m.get("id") or "")
            for m in (body.get("data") or [])
            if isinstance(m, dict)
        ]
        model = info["model"]
        model_ok = (not ids) or any(
            model == mid or model in mid or mid in model for mid in ids if mid
        )
        return {
            "ok": True,
            "reachable": True,
            "model": model,
            "model_present": model_ok,
            "models": ids[:12],
            "base_url": info["base_url"],
        }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "reachable": False,
            "model": info["model"],
            "model_present": False,
            "models": [],
            "base_url": info["base_url"],
            "error": str(exc),
        }


def qwen_status() -> Dict[str, Any]:
    st = load_state()
    probe = probe_qwen_endpoint()
    return {
        "ok": True,
        "enabled": bool(st["enabled"]),
        "name": QWEN_NAME,
        "messenger_configured": messenger_configured(),
        "mention": "@Qwen",
        "personalities": [
            {
                "id": personality.id,
                "name": personality.name,
                "mention": personality.mention,
                "description": personality.prompt,
            }
            for personality in PERSONALITIES
        ],
        "last_replied_id": st["last_replied_id"],
        "endpoint": probe,
        **_active_research_view(st),
        "hint": (
            "Add Qwen from the Friend room, then mention @Qwen or "
            "@Qwen-Contrarian. Add 'research' for background web research."
        ),
    }


def set_qwen_in_conversation(enabled: bool) -> Dict[str, Any]:
    if enabled and not messenger_configured():
        raise MessengerBridgeError(
            "Friend messenger env missing on the dashboard process. Restart with "
            "ANALYST_MESSENGER_URL and ANALYST_MESSENGER_INVITE set "
            "(invite = Fly MESSENGER_INVITE_TOKEN).",
            status=503,
        )
    if enabled:
        probe = probe_qwen_endpoint()
        if not probe.get("reachable"):
            info = qwen_endpoint_info()
            raise MessengerBridgeError(
                f"Cannot reach Qwen at {info['base_url']}. Start Ollama "
                f"(or set ANALYST_QWEN_BASE_URL), then try Add again.",
                status=503,
            )
        if probe.get("models") and not probe.get("model_present"):
            info = qwen_endpoint_info()
            available = ", ".join(probe.get("models") or []) or "(none)"
            raise MessengerBridgeError(
                f"Model {info['model']!r} not found on the Qwen server. "
                f"Set ANALYST_QWEN_MODEL to one of: {available}",
                status=503,
            )
    st = load_state()
    was = bool(st.get("enabled"))
    st["enabled"] = bool(enabled)
    if enabled and not was:
        for personality in PERSONALITIES:
            ensure_session_as(personality.name, cookie_key=personality.cookie_key)
        _post_as_qwen(
            "The Qwen personalities are in the chat. Mention @Qwen for a balanced "
            "answer or @Qwen-Contrarian to challenge the premise. Add 'research' "
            "to either mention for a background web search."
        )
        raw = list_raw_messages(limit=5)
        if raw:
            st["last_replied_id"] = max(int(m.get("id") or 0) for m in raw)
    save_state(st)
    return qwen_status()


def _room_cookie_key(cookie_key: str, room_id: str) -> str:
    """Per-room cookie jar so the bot can sit in many rooms at once."""
    if room_id == "legacy":
        return cookie_key
    safe = "".join(c for c in room_id if c.isalnum() or c in "-_") or "room"
    return f"{cookie_key}__{safe}"


def _post_as_qwen(body: str, room_id: str = "legacy") -> Dict[str, Any]:
    cookie_key = _room_cookie_key("qwen", room_id)
    ensure_session_as(QWEN_NAME, cookie_key=cookie_key, room_id=room_id)
    opener = _opener_for(cookie_key)
    data = _request(
        "POST",
        "/api/messages",
        payload={"body": body},
        opener=opener,
    )
    _save_jar(opener)
    if not data.get("ok"):
        raise MessengerBridgeError(
            f"Qwen could not post: {data.get('error') or 'unknown'}"
        )
    return data.get("message") or {}


def _post_as_personality(
    personality: FriendPersonality, body: str, room_id: str = "legacy"
) -> Dict[str, Any]:
    if personality.id == DEFAULT_PERSONALITY.id:
        return _post_as_qwen(body, room_id)
    cookie_key = _room_cookie_key(personality.cookie_key, room_id)
    ensure_session_as(personality.name, cookie_key=cookie_key, room_id=room_id)
    opener = _opener_for(cookie_key)
    data = _request(
        "POST",
        "/api/messages",
        payload={"body": body},
        opener=opener,
    )
    _save_jar(opener)
    if not data.get("ok"):
        raise MessengerBridgeError(
            f"{personality.name} could not post: {data.get('error') or 'unknown'}"
        )
    return data.get("message") or {}


def _wants_current_info(text: str) -> bool:
    """True if the message asks for something that needs a live lookup."""
    value = text or ""
    if RESEARCH_INTENT_RE.search(value):
        return True
    # "find the filings", "verify their margins", "check recent news", etc.
    if _RESEARCH_ACTION_RE.search(value) and _CURRENT_INFO_RE.search(value):
        return True
    # Explicit finance intent about current data (filings / news).
    if classify_finance_intent(value) in {"filings", "news"}:
        return True
    return False


def _is_research_request(body: str) -> bool:
    text = body or ""
    return bool(MENTION_RE.search(text) and _wants_current_info(text))


_NONANSWER_RE = re.compile(
    r"^(?:hi|hello|hey|greetings|good\s+(?:morning|afternoon|evening)|"
    r"i'?m\s+here|i\s+am\s+here|sure[!,. ]|of\s+course)\b",
    re.IGNORECASE,
)


def _looks_like_nonanswer(reply: str, symbol: Optional[str] = None) -> bool:
    """Detect vacuous 'how can I help' style replies with no real content."""
    text = (reply or "").strip()
    if len(text) < 40:
        return True
    low = text.lower()
    filler = (
        _NONANSWER_RE.match(text)
        or "feel free to ask" in low
        or "how can i help" in low
        or "let me know if" in low[:60]
    )
    if filler and (not symbol or symbol.upper() not in text.upper()):
        return True
    return False


def _deterministic_research_brief(
    symbol: str, hits: List[Dict[str, Any]]
) -> str:
    """Grounded fallback: verifiable market facts + the top sources found."""
    parts: List[str] = []
    try:
        brief = build_financial_brief(symbol)
    except Exception:  # noqa: BLE001
        brief = ""
    if brief:
        parts.append(brief)
    sources: List[str] = []
    for hit in hits[:5]:
        url = str(hit.get("url") or "").strip()
        if not url:
            continue
        title = str(hit.get("title") or "").strip()
        sources.append(f"- {title}: {url}" if title else f"- {url}")
    if sources:
        parts.append("Top sources I found:\n" + "\n".join(sources))
    return "\n\n".join(p for p in parts if p).strip()


def _context_snippet(
    raw: List[Dict[str, Any]], trigger: Dict[str, Any], n: int = 8
) -> List[Dict[str, Any]]:
    """Last ``n`` messages up to and including the trigger."""
    trigger_id = int(trigger.get("id") or 0)
    up_to = [m for m in raw if int(m.get("id") or 0) <= trigger_id]
    if not up_to:
        up_to = list(raw)
    return up_to[-max(1, n) :]


def _format_context_lines(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        author = str(msg.get("author") or "?")
        body = str(msg.get("body") or "").strip()
        if body:
            lines.append(f"{author}: {body}")
    return "\n".join(lines)


def _build_chat_messages(
    raw: List[Dict[str, Any]],
    trigger: Dict[str, Any],
    personality: FriendPersonality = DEFAULT_PERSONALITY,
) -> List[Dict[str, str]]:
    """Map room history to OpenAI chat roles for the local Qwen call."""
    out: List[Dict[str, str]] = []
    for msg in raw[-6:]:
        author = str(msg.get("author") or "")
        body = str(msg.get("body") or "").strip()
        if not body:
            continue
        if author == personality.name:
            out.append({"role": "assistant", "content": body})
        else:
            out.append({"role": "user", "content": f"{author}: {body}"})
    t_author = str(trigger.get("author") or "")
    t_body = str(trigger.get("body") or "").strip()
    if not out or out[-1].get("content") != f"{t_author}: {t_body}":
        out.append({"role": "user", "content": f"{t_author}: {t_body}"})
    return out


def _find_pending_mention(
    raw: List[Dict[str, Any]], last_replied_id: int
) -> Optional[Dict[str, Any]]:
    pending = None
    personality_names = {personality.name for personality in PERSONALITIES}
    for msg in raw:
        mid = int(msg.get("id") or 0)
        if mid <= last_replied_id:
            continue
        author = str(msg.get("author") or "")
        body = str(msg.get("body") or "")
        if author in personality_names:
            continue
        if MENTION_RE.search(body):
            pending = msg
    return pending


def _draft_search_queries(context_text: str, trigger_body: str) -> List[str]:
    system = (
        "You draft web search queries for a casual group chat research request. "
        "Return 1 or 2 short search queries, one per line. "
        f"Today is {_today_utc()} UTC. Prefer primary sources, investor relations, "
        "regulatory filings, and recent dated reporting. Preserve explicit ticker "
        "symbols. No numbering, no quotes, no commentary. Public web only."
    )
    prompt = (
        f"Recent chat:\n{context_text}\n\n"
        f"Research request:\n{trigger_body}\n\n"
        "Search queries:"
    )
    try:
        raw = _call_openai_compatible_messages(
            [{"role": "user", "content": prompt}],
            max_tokens=80,
            system=system,
            temperature=0.2,
        )
    except RuntimeError:
        # Fallback: strip @Qwen / intent words from the trigger.
        cleaned = strip_personality_mentions(trigger_body)
        cleaned = RESEARCH_INTENT_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?")
        return [cleaned] if cleaned else ["latest news"]

    queries: List[str] = []
    for line in (raw or "").splitlines():
        q = line.strip().lstrip("-*•0123456789. ").strip("\"'")
        if q and q.lower() not in {x.lower() for x in queries}:
            queries.append(q[:160])
        if len(queries) >= 2:
            break
    if not queries:
        cleaned = strip_personality_mentions(trigger_body)
        cleaned = RESEARCH_INTENT_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?")
        if cleaned:
            queries.append(cleaned)
    return queries or ["latest news"]


def _synthesize_research(
    context_text: str,
    trigger_body: str,
    hits_text: str,
    personality: FriendPersonality = DEFAULT_PERSONALITY,
) -> str:
    system = (
        f"You are {personality.name} in a casual group chat. {personality.prompt} "
        f"Today is {_today_utc()} UTC. You just finished a quick "
        "public-web research pass. Reply in plain text (no markdown fences). "
        "Treat the supplied source material as the only evidence for current facts. "
        "Never invent or repair a missing date, number, event, title, or URL. "
        "Conversation context is only the user's premise, never evidence. Do not repeat "
        "a named product, project, event, valuation, or company claim unless it appears "
        "in the supplied source material. Do not annualize one quarterly EPS figure or "
        "derive a P/E ratio from it. Preserve the company's stated fiscal-period label; "
        "do not substitute a calendar-quarter label. A 52-week price range does not "
        "establish valuation, and one quarter is not enough to characterize growth or "
        "performance without a supplied comparison period. "
        "For every material current claim, cite the supplied source URL and its stated "
        "date or timestamp. Clearly label inference and unavailable facts. Be concise "
        "and use exactly three sections: Verified evidence, Analysis, and "
        "Unavailable / next checks. Analysis may interpret supplied evidence but may "
        "not introduce new facts. In the unavailable section, mention only evidence "
        "the request explicitly asks for; do not brainstorm unrelated projects, "
        "products, risks, or events. Do not provide personalized trading advice. "
        "If results are thin or conflict, say so."
    )
    prompt = (
        f"Recent chat context:\n{context_text}\n\n"
        f"Request:\n{trigger_body}\n\n"
        f"Search results:\n{hits_text}\n\n"
        "Write your reply to the room:"
    )
    reply = _call_openai_compatible_messages(
        [{"role": "user", "content": prompt}],
        max_tokens=900,
        system=system,
        temperature=0.1,
    )
    return (reply or "").strip()


def _synthesize_outlook(
    request: str,
    evidence: Dict[str, Any],
    hits_text: str,
    personality: FriendPersonality = DEFAULT_PERSONALITY,
) -> str:
    system = (
        f"You are {personality.name}. {personality.prompt} Today is {_today_utc()} UTC. "
        "Write a stock research outlook, not a recommendation. Use only the supplied "
        "structured evidence and trusted-source excerpts. Never use model memory for "
        "current facts, invent a price target, or treat an analyst opinion as fact. "
        "Every current factual claim must name its source URL and date/timestamp. "
        "Use exactly these sections: Verified facts; Bull scenario; Bear scenario; "
        "Catalysts; Risks; What would change the view. Bull and bear statements must "
        "be conditional interpretations tied to verified evidence. If the evidence "
        "gate is not ready, say so in Verified facts and keep scenarios provisional. "
        "Do not say buy, sell, should buy, or should sell."
    )
    prompt = (
        f"User request:\n{request}\n\n"
        f"Structured evidence:\n{format_outlook_evidence(evidence)}\n\n"
        f"Ranked trusted web evidence:\n{hits_text}\n\n"
        "Write the outlook:"
    )
    reply = _call_openai_compatible_messages(
        [{"role": "user", "content": prompt}],
        max_tokens=1200,
        system=system,
        temperature=0.15,
    )
    return (reply or "").strip()


def _outlook_reply_valid(reply: str, evidence: Dict[str, Any]) -> bool:
    value = reply or ""
    if len(value) > 1900:
        return False
    lowered = value.casefold()
    required = (
        "verified facts",
        "bull scenario",
        "bear scenario",
        "catalysts",
        "risks",
        "what would change the view",
    )
    if not all(section in lowered for section in required):
        return False
    if re.search(r"\b(?:should|recommend(?:ation)?)\s+(?:buy|sell)\b", lowered):
        return False

    allowed_urls: set[str] = set()

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, str) and item.startswith(("http://", "https://")):
            allowed_urls.add(item.rstrip(".,);]"))

    collect(evidence)
    cited = {
        url.rstrip(".,);]")
        for url in re.findall(r"https?://[^\s)\]]+", value)
    }
    return cited.issubset(allowed_urls)


def _run_research_job(
    trigger: Dict[str, Any],
    context: List[Dict[str, Any]],
    personality: FriendPersonality = DEFAULT_PERSONALITY,
    room_id: str = "legacy",
) -> None:
    def _upd(**fields: Any) -> Dict[str, Any]:
        return _update_room(room_id, **fields)

    trigger_id = int(trigger.get("id") or 0)
    trigger_body = str(trigger.get("body") or "").strip()
    context_text = _format_context_lines(context)
    try:
        _upd(research_progress="Drafting search queries…")
        prior_state = load_state()
        # Prefer a company named in *this* request (tickers + aliases), then the
        # surrounding chat, then remembered state, and only as a last resort a
        # network SEC name lookup (which can false-match ordinary words).
        # Never let a stale symbol override a company the user explicitly named.
        symbol = (
            resolve_symbol(trigger_body, allow_network=False)
            or resolve_symbol("", context_text, allow_network=False)
            or prior_state.get("last_finance_symbol")
            or resolve_symbol(trigger_body, context_text, allow_network=True)
        )
        direct_intent = classify_finance_intent(trigger_body)
        intent = (
            direct_intent
            if direct_intent != "general"
            else (
                prior_state.get("last_finance_intent")
                or classify_finance_intent(context_text)
            )
        )
        model_queries = _draft_search_queries(context_text, trigger_body)
        queries: List[str] = []
        if symbol and intent in {"outlook", "news", "filings", "snapshot"}:
            queries.extend(finance_search_queries(symbol, intent=intent))
        queries.extend(model_queries)
        queries = list(dict.fromkeys(q for q in queries if q))[:4]
        topic = queries[0] if queries else trigger_body
        _upd(research_query=topic, research_progress="Searching…")

        all_hits: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()
        for q in queries:
            _upd(research_progress=f"Searching: {q[:80]}")
            for hit in bing_search(q, limit=5):
                url = str(hit.get("url") or "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_hits.append(hit)

        _upd(research_progress="Reading results…")
        ranked_hits = rank_search_hits(all_hits, intent=intent)
        trusted_hits = enrich_trusted_hits(ranked_hits[:8], max_pages=2)
        hits_text = format_hits_for_prompt(trusted_hits)
        _upd(research_progress="Writing reply…")
        if intent == "outlook" and symbol:
            evidence = build_outlook_evidence(symbol)
            evidence["trusted_sources"] = [
                {
                    "title": hit.get("title"),
                    "url": hit.get("url"),
                    "published_at": hit.get("published_at"),
                    "snippet": hit.get("snippet"),
                    "excerpt": hit.get("excerpt"),
                }
                for hit in trusted_hits[:6]
            ]
            trends = evidence.get("sec_trends") or {}
            has_comparable_facts = bool(
                ((trends.get("revenue") or {}).get("latest"))
                or ((trends.get("eps") or {}).get("latest"))
            )
            evidence["ready"] = bool(
                evidence.get("market")
                and has_comparable_facts
                and (evidence.get("filings") or evidence.get("trusted_sources"))
                and evidence.get("relative_return_pct") is not None
            )
            reply = _synthesize_outlook(
                trigger_body, evidence, hits_text, personality
            )
            if not _outlook_reply_valid(reply, evidence):
                reply = render_outlook_brief(
                    evidence, contrarian=personality.id == "qwen-contrarian"
                )
        elif symbol and (
            intent == "snapshot" or STRUCTURED_FINANCE_RE.search(trigger_body)
        ):
            reply = build_financial_brief(symbol)
        elif symbol and intent == "filings":
            evidence = build_outlook_evidence(symbol, include_provider=False)
            reply = _synthesize_research(
                context_text,
                trigger_body,
                (
                    f"Recent SEC filings and structured evidence:\n"
                    f"{format_outlook_evidence(evidence)}\n\n"
                    f"Ranked web sources:\n{hits_text}"
                ),
                personality,
            )
        else:
            structured_text = format_financial_context(symbol or trigger_body)
            source_text = (
                f"Web search results:\n{hits_text}\n\n"
                f"Deterministic structured sources:\n{structured_text}"
            )
            synthesis_context = (
                context_text if CONTEXT_REFERENCE_RE.search(trigger_body) else ""
            )
            reply = _synthesize_research(
                synthesis_context, trigger_body, source_text, personality
            )
        reply = _clean_model_reply(reply, personality)
        if symbol and _looks_like_nonanswer(reply, symbol):
            fallback = _deterministic_research_brief(symbol, trusted_hits)
            if fallback:
                reply = fallback
        if not reply:
            raise RuntimeError("empty research reply")

        posted = _post_as_personality(personality, reply, room_id)
        posted_id = int(posted.get("id") or 0)
        st = load_state()
        prior = room_progress(st, room_id)
        if posted_id > int(prior.get("last_replied_id") or 0):
            _upd(
                last_replied_id=posted_id,
                research_status="idle",
                research_progress="",
                research_error="",
                research_query="",
                research_started_at=None,
                research_trigger_id=None,
            )
        else:
            _upd(
                research_status="idle",
                research_progress="",
                research_error="",
                research_query="",
                research_started_at=None,
                research_trigger_id=None,
            )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:240]
        try:
            _post_as_personality(
                personality, f"Couldn't finish research: {err}", room_id
            )
        except Exception:  # noqa: BLE001
            pass
        _upd(
            research_status="failed",
            research_progress="",
            research_error=err,
            research_started_at=None,
            research_trigger_id=trigger_id or None,
        )


def _start_research(
    trigger: Dict[str, Any],
    raw: List[Dict[str, Any]],
    personality: FriendPersonality = DEFAULT_PERSONALITY,
    room_id: str = "legacy",
) -> Dict[str, Any]:
    context = _context_snippet(raw, trigger, n=8)
    trigger_id = int(trigger.get("id") or 0)
    topic_hint = RESEARCH_INTENT_RE.sub(
        " ", strip_personality_mentions(str(trigger.get("body") or ""))
    )
    topic_hint = re.sub(r"\s+", " ", topic_hint).strip(" .,!?") or "your request"

    ack = _post_as_personality(personality, "On it — researching…", room_id)
    ack_id = int(ack.get("id") or trigger_id or 0)

    prior = room_progress(load_state(), room_id)
    _update_room(
        room_id,
        last_replied_id=max(
            int(prior.get("last_replied_id") or 0), trigger_id, ack_id
        ),
        research_status="researching",
        research_progress="Starting…",
        research_query=topic_hint,
        research_started_at=time.time(),
        research_trigger_id=trigger_id,
        research_error="",
    )

    thread = threading.Thread(
        target=_run_research_job,
        args=(trigger, context, personality, room_id),
        name=f"{personality.id}-research-{room_id}-{trigger_id}",
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "enabled": True,
        "replied": True,
        "research": True,
        "room_id": room_id,
        "personality": personality.id,
        "trigger_id": trigger_id,
        "message": ack,
    }


def _load_room_messages(room_id: str = "legacy") -> List[Dict[str, Any]]:
    cookie_key = _room_cookie_key("qwen", room_id)
    return list_room_messages(
        room_id, cookie_key=cookie_key, name=QWEN_NAME, limit=80
    )


def tick_qwen() -> Dict[str, Any]:
    """Reply as the mentioned Qwen personality across every watched room."""
    if not _TICK_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "busy"}
    try:
        st = load_state()
        if not st.get("enabled"):
            return {"ok": True, "enabled": False, "replied": False}
        if not messenger_configured():
            return {"ok": False, "error": "messenger not configured", "replied": False}

        try:
            rooms = list_bot_rooms()
        except MessengerBridgeError:
            rooms = [{"room_id": "legacy"}]
        room_ids = list(
            dict.fromkeys(str(r.get("room_id") or "legacy") for r in rooms)
        ) or ["legacy"]

        results: List[Dict[str, Any]] = []
        replied_any = False
        for room_id in room_ids:
            try:
                res = _tick_room(room_id) or {
                    "ok": True,
                    "room_id": room_id,
                    "replied": False,
                }
            except MessengerBridgeError as exc:
                res = {"ok": False, "room_id": room_id, "error": str(exc)}
            results.append(res)
            if res.get("replied"):
                replied_any = True

        # Promote a representative room result to the top level: prefer a room
        # that replied, then one that was skipped, else the first.
        primary = next(
            (r for r in results if r.get("replied")),
            next((r for r in results if r.get("skipped")), None),
        ) or (results[0] if results else {})
        summary = {
            "ok": True,
            "enabled": True,
            "replied": replied_any,
            "rooms": results,
        }
        for key in (
            "research",
            "personality",
            "trigger_id",
            "message",
            "skipped",
            "room_id",
        ):
            if key in primary:
                summary[key] = primary[key]
        return summary
    finally:
        _TICK_LOCK.release()


def _tick_room(room_id: str) -> Dict[str, Any]:
    """Handle one pending mention in a single room (called under the tick lock)."""
    st = load_state()
    progress = room_progress(st, room_id)
    raw = _load_room_messages(room_id)
    trigger = _find_pending_mention(raw, int(progress.get("last_replied_id") or 0))
    if not trigger:
        return {"ok": True, "room_id": room_id, "replied": False}

    body = str(trigger.get("body") or "")
    personality = match_personality(body)
    if personality is None:
        return {"ok": True, "room_id": room_id, "replied": False}
    personality_names = {item.name for item in PERSONALITIES}
    human_context = [
        message
        for message in _context_snippet(raw, trigger, n=12)
        if str(message.get("author") or "") not in personality_names
    ]
    recent_context = _format_context_lines(human_context)
    finance_symbol = resolve_symbol(body, recent_context, allow_network=False)
    finance_intent = classify_finance_intent(f"{recent_context}\n{body}")
    # Finance memory is shared across rooms.
    if finance_symbol:
        st["last_finance_symbol"] = finance_symbol
    if finance_intent != "general":
        st["last_finance_intent"] = finance_intent
    if finance_symbol or finance_intent != "general":
        save_state(st)
    research_trigger = trigger
    do_research = _is_research_request(body)
    # Follow-ups like "@Qwen can you do this" inherit the most recent human
    # request that needed a live lookup, so the agent actually searches.
    if not do_research and MENTION_RE.search(body) and _FOLLOWUP_DO_RE.search(body):
        for prior in reversed(human_context[:-1]):
            prior_body = str(prior.get("body") or "")
            if _wants_current_info(prior_body):
                do_research = True
                merged = strip_personality_mentions(prior_body).strip()
                research_trigger = {
                    **trigger,
                    "body": f"{merged} ({strip_personality_mentions(body).strip()})",
                }
                break
    if do_research:
        if (progress.get("research_status") or "idle") == "researching":
            started = progress.get("research_started_at")
            stale = bool(started) and (
                time.time() - float(started) >= STALE_RESEARCH_SECONDS
            )
            if not stale:
                return {
                    "ok": True,
                    "room_id": room_id,
                    "replied": False,
                    "skipped": "researching",
                }
            # Hung job in this process: clear and start fresh.
            _update_room(
                room_id,
                research_status="idle",
                research_progress="",
                research_started_at=None,
                research_trigger_id=None,
            )
        return _start_research(research_trigger, raw, personality, room_id)

    system = (
            f"You are {personality.name}, a participant in a casual group chat. "
            f"{personality.prompt} Today is {_today_utc()} UTC. "
            f"Someone mentioned you with {personality.mention}. "
            "Your model memory may be stale: do not claim that a fact is current, "
            "recent, or true today unless it appears in the conversation. Ask for a "
            "research pass when current evidence is required. Focus on the latest "
            "request and use older room messages only when directly relevant. "
            "Do not infer valuation or price stability from a 52-week range. Do not "
            "claim results met expectations unless an expectation comparison is in "
            "the evidence. Never compare quantities with different units or unrelated "
            "periods. A falsification test must name evidence that could disprove the "
            "claim or caution being tested. Never turn missing evidence into an "
        "affirmative claim. "
        "Reply briefly and naturally in plain text. "
        "Do not provide personalized trading advice. "
        "Do not repeat your mention in the reply unless useful."
    )
    messages = _build_chat_messages(raw, trigger, personality)
    try:
        reply = _call_openai_compatible_messages(
            messages, max_tokens=512, system=system, temperature=0.2
        )
    except RuntimeError as exc:
        raise MessengerBridgeError(str(exc), status=503) from exc
    reply = _clean_model_reply(reply, personality)
    if not reply:
        raise MessengerBridgeError("Qwen returned an empty reply")

    posted = _post_as_personality(personality, reply, room_id)
    _update_room(
        room_id,
        last_replied_id=int(posted.get("id") or trigger.get("id") or 0),
    )
    return {
        "ok": True,
        "enabled": True,
        "replied": True,
        "research": False,
        "room_id": room_id,
        "personality": personality.id,
        "trigger_id": trigger.get("id"),
        "message": posted,
    }
