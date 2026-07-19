"""Derive human-readable insights/tags from a session's events."""

from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .browser import normalize_url_key


def summarize_session_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build clear labels for what a session captured.

    Dedupes repeated URL visits into unique pages with visit counts.
    """
    hosts: Counter = Counter()
    symbols: Counter = Counter()
    sections: Counter = Counter()
    surfaces: Counter = Counter()
    types: Counter = Counter()
    notes: List[str] = []
    # key -> page meta (visits + last seen)
    pages: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    chronological = list(reversed(events)) if events and _looks_desc(events) else list(events)

    for ev in chronological:
        et = ev.get("type") or ""
        types[et] += 1
        surface = ev.get("surface") or ""
        # Ignore lifecycle noise when counting surfaces for chips
        if surface and et not in {"session_start", "session_end", "tag"}:
            surfaces[surface] += 1
        payload = ev.get("payload") or {}

        if et == "url_focus":
            host = payload.get("host") or _host_from_url(payload.get("url"))
            if host:
                hosts[host] += 1
            raw_url = str(payload.get("url") or "")
            key = normalize_url_key(raw_url) or raw_url
            if key:
                existing = pages.get(key)
                path = payload.get("path") or _path_from_key(key)
                if path != "/" and str(path).endswith("/"):
                    path = str(path).rstrip("/")
                if existing:
                    existing["visits"] += 1
                    existing["last_ts"] = ev.get("ts") or existing.get("last_ts")
                    if payload.get("title"):
                        existing["title"] = str(payload["title"])[:120]
                    if payload.get("quote"):
                        existing["quote"] = payload["quote"]
                    if payload.get("section"):
                        existing["section"] = payload.get("section")
                    existing["path"] = path
                else:
                    pages[key] = {
                        "url": key,
                        "host": host or "",
                        "path": path,
                        "symbol": (str(payload["symbol"]).upper() if payload.get("symbol") else None),
                        "section": payload.get("section"),
                        "title": str(payload.get("title") or "")[:120],
                        "quote": payload.get("quote"),
                        "visits": 1,
                        "last_ts": ev.get("ts"),
                    }
            if payload.get("symbol"):
                symbols[str(payload["symbol"]).upper()] += 1
            if payload.get("section"):
                sections[str(payload["section"])] += 1
        elif et == "symbol_focus":
            if payload.get("symbol"):
                symbols[str(payload["symbol"]).upper()] += 1
        elif et in {"interval_change", "drawing_meta"}:
            if payload.get("symbol"):
                symbols[str(payload["symbol"]).upper()] += 1
        elif et == "note":
            text = str(payload.get("text") or "").strip()
            if text:
                notes.append(text[:240])
        elif et == "inbox_file":
            name = payload.get("name") or payload.get("path")
            if name:
                notes.append(f"Inbox file: {name}")
        elif et == "artifact_attach":
            path = payload.get("path")
            if path:
                name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
                notes.append(f"Artifact: {name}")

    # Newest pages first
    page_list = sorted(
        pages.values(),
        key=lambda p: str(p.get("last_ts") or ""),
        reverse=True,
    )

    chips: List[Dict[str, str]] = []
    for host, n in hosts.most_common(4):
        chips.append({"kind": "site", "label": host, "detail": f"{n}×"})
    for sym, n in symbols.most_common(8):
        chips.append({"kind": "symbol", "label": sym, "detail": f"{n}×"})
    # Skip noisy "home" unless it's the only section
    section_items = [(s, n) for s, n in sections.most_common() if s != "home"]
    if not section_items and sections:
        section_items = sections.most_common(1)
    for sec, n in section_items[:4]:
        chips.append({"kind": "section", "label": f"/{sec}", "detail": f"{n}×"})
    # Only show surface chips when more than one surface (avoids "browser 10×" spam)
    if len(surfaces) > 1:
        for surf, n in surfaces.most_common(4):
            chips.append({"kind": "surface", "label": surf, "detail": f"{n}×"})

    parts = []
    if symbols:
        parts.append(", ".join(s for s, _ in symbols.most_common(5)))
    if hosts:
        parts.append(hosts.most_common(1)[0][0])
    if page_list:
        parts.append(f"{len(page_list)} page{'s' if len(page_list) != 1 else ''}")
    if notes and not parts:
        parts.append(f"{len(notes)} note(s)")
    if not parts:
        parts.append("no site/symbol signals yet")

    url_focus_n = types.get("url_focus", 0)
    unique_n = len(page_list)

    return {
        "hosts": [h for h, _ in hosts.most_common()],
        "symbols": [s for s, _ in symbols.most_common()],
        "sections": [s for s, _ in sections.most_common()],
        "surfaces": [s for s, _ in surfaces.most_common()],
        "event_types": dict(types),
        "notes": notes[:12],
        "pages": page_list[:40],
        "sample_urls": [p["url"] for p in page_list[:12]],
        "chips": chips,
        "summary_line": " · ".join(parts),
        "event_count": len(events),
        "unique_pages": unique_n,
        "url_focus_count": url_focus_n,
        "deduped_visits": max(0, url_focus_n - unique_n),
    }


def collapse_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapse consecutive identical url_focus (same normalized URL) into one row.

    Non-URL events pass through unchanged. Each collapsed row gains visit_count.
    Expects chronological (oldest→newest) or newest-first; grouping is consecutive.
    """
    out: List[Dict[str, Any]] = []
    for ev in events:
        et = ev.get("type")
        if et != "url_focus":
            out.append({**ev, "visit_count": 1})
            continue
        payload = ev.get("payload") or {}
        key = normalize_url_key(str(payload.get("url") or ""))
        if (
            out
            and out[-1].get("type") == "url_focus"
            and normalize_url_key(str((out[-1].get("payload") or {}).get("url") or ""))
            == key
            and key
        ):
            out[-1]["visit_count"] = int(out[-1].get("visit_count") or 1) + 1
            # Keep newest timestamp if list is newest-first
            if str(ev.get("ts") or "") > str(out[-1].get("ts") or ""):
                out[-1]["ts"] = ev.get("ts")
                out[-1]["payload"] = payload
            continue
        out.append({**ev, "visit_count": 1})
    return out


def recent_url_focus_same(
    events: List[Dict[str, Any]],
    url_key: str,
    *,
    within_seconds: float = 90.0,
    now_ts: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the newest matching url_focus if it is within the dedupe window."""
    if not url_key:
        return None
    newest = None
    for ev in events:
        if ev.get("type") != "url_focus":
            continue
        payload = ev.get("payload") or {}
        if normalize_url_key(str(payload.get("url") or "")) != url_key:
            continue
        newest = ev
        break  # list_events is newest-first
    if not newest:
        return None
    try:
        a = _parse_ts(str(newest.get("ts") or ""))
        b = _parse_ts(now_ts) if now_ts else datetime.utcnow()
        if a is None:
            return newest
        delta = abs((b - a).total_seconds())
        if delta <= within_seconds:
            return newest
    except Exception:  # noqa: BLE001
        return newest
    return None


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def _path_from_key(key: str) -> str:
    try:
        return urlparse(key).path or "/"
    except Exception:  # noqa: BLE001
        return "/"


def _host_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        return (urlparse(str(url)).hostname or "").lower() or None
    except Exception:  # noqa: BLE001
        return None


def _looks_desc(events: List[Dict[str, Any]]) -> bool:
    if len(events) < 2:
        return False
    a = str(events[0].get("ts") or "")
    b = str(events[-1].get("ts") or "")
    return a > b
