"""Local timeline dashboard (stdlib only, binds to localhost by default)."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Callable, Optional
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from .ledger import Ledger


def _h(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _css() -> str:
    return """
    :root {
      --bg: #0f1419;
      --panel: #1a222c;
      --text: #e7ecf1;
      --muted: #8b9aab;
      --accent: #3d9cf0;
      --open: #2ecc71;
      --warn: #e6b84d;
      --border: #2a3542;
      --mono: "IBM Plex Mono", ui-monospace, monospace;
      --sans: "Source Sans 3", "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 2rem;
      font-family: var(--sans); background: var(--bg); color: var(--text);
      background-image: radial-gradient(ellipse at top, #1a2838 0%, var(--bg) 55%);
      min-height: 100vh;
    }
    h1 { font-weight: 600; letter-spacing: -0.02em; margin: 0 0 0.25rem; }
    h2 { font-size: 1.1rem; margin: 0 0 0.75rem; }
    h3 { font-size: 1rem; margin: 1.25rem 0 0.5rem; color: var(--muted); font-weight: 500; }
    .sub { color: var(--muted); margin-bottom: 1.5rem; }
    .nav { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
    .nav a {
      color: var(--accent); text-decoration: none;
      border-bottom: 1px solid transparent; padding-bottom: 0.1rem;
    }
    .nav a:hover, .nav a.active { border-bottom-color: var(--accent); }
    .cards { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
    .card {
      background: var(--panel); border: 1px solid var(--border);
      padding: 1rem 1.25rem; min-width: 8rem;
    }
    .card .n { font-size: 1.75rem; font-family: var(--mono); color: var(--accent); }
    .card .l { color: var(--muted); font-size: 0.85rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 2rem; }
    th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
    th { color: var(--muted); font-weight: 500; }
    tr.open td:nth-child(5) { color: var(--open); }
    code { font-family: var(--mono); font-size: 0.8rem; color: #b8c7d9; }
    a { color: var(--accent); }
    .badge {
      display: inline-block; font-size: 0.75rem; font-family: var(--mono);
      padding: 0.15rem 0.45rem; border: 1px solid var(--border); border-radius: 3px;
      color: var(--muted); margin-right: 0.35rem;
    }
    .badge.ok { color: var(--open); border-color: #2a5a3a; }
    .badge.warn { color: var(--warn); border-color: #5a4a2a; }
    .actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0 1.5rem; }
    button, .btn {
      background: var(--panel); color: var(--text); border: 1px solid var(--border);
      padding: 0.45rem 0.85rem; font-family: var(--sans); font-size: 0.9rem;
      cursor: pointer; border-radius: 3px;
    }
    button:hover, .btn:hover { border-color: var(--accent); color: var(--accent); }
    button.primary { background: #1e3a55; border-color: var(--accent); color: var(--accent); }
    .panel {
      background: var(--panel); border: 1px solid var(--border);
      padding: 1rem 1.25rem; margin-bottom: 1.5rem;
    }
    pre.out {
      background: #0c1015; border: 1px solid var(--border);
      padding: 0.75rem 1rem; overflow: auto; max-height: 20rem;
      font-family: var(--mono); font-size: 0.78rem; color: #b8c7d9;
      white-space: pre-wrap;
    }
    .review {
      white-space: pre-wrap; font-size: 0.9rem; line-height: 1.45;
      color: #c5d0dc; max-height: 24rem; overflow: auto;
    }
    .muted { color: var(--muted); }
    .flash {
      background: #163024; border: 1px solid #2a5a3a; color: var(--open);
      padding: 0.75rem 1rem; margin-bottom: 1.25rem;
    }
    .flash.warn {
      background: #2a2410; border-color: #5a4a2a; color: var(--warn);
    }
    .flash.err {
      background: #2a1515; border-color: #5a2a2a; color: #e88;
    }
    .empty {
      background: var(--panel); border: 1px dashed var(--border);
      padding: 1.25rem 1.5rem; margin: 1rem 0 2rem; color: var(--muted);
    }
    .empty code { color: #b8c7d9; }
    .empty ol { margin: 0.5rem 0 0 1.2rem; padding: 0; }
    .empty li { margin: 0.35rem 0; }
    details.raw {
      margin-top: 1rem; color: var(--muted); font-size: 0.85rem;
    }
    select {
      background: var(--panel); color: var(--text); border: 1px solid var(--border);
      padding: 0.4rem 0.6rem; font-family: var(--sans);
    }
    button:disabled { opacity: 0.5; cursor: wait; }
    .toggle-row { display: flex; align-items: center; gap: 0.6rem; margin: 0.4rem 0; }
    .toggle-row label { cursor: pointer; }
    .evidence-block { margin-bottom: 1rem; border: 1px solid var(--border); background: #121820; }
    .evidence-block.excluded { opacity: 0.55; }
    .evidence-head {
      display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center;
      padding: 0.65rem 0.85rem; border-bottom: 1px solid var(--border);
      background: var(--panel);
    }
    .evidence-head .meta { color: var(--muted); font-size: 0.85rem; }
    .event-table { width: 100%; margin: 0; font-size: 0.82rem; }
    .event-table td { border-bottom-color: #222a33; }
    .event-table tr.excluded td { opacity: 0.45; text-decoration: line-through; }
    input[type=text], textarea {
      width: 100%; box-sizing: border-box; margin-top: 0.35rem;
      background: #0c1015; border: 1px solid var(--border); color: var(--text);
      padding: 0.5rem 0.65rem; font-family: var(--mono); font-size: 0.85rem;
    }
    textarea { min-height: 4rem; resize: vertical; }
    .edit-grid { display: grid; gap: 1rem; grid-template-columns: 1fr; }
    @media (min-width: 900px) {
      .edit-grid { grid-template-columns: 1fr 1fr; }
    }
    .track-toggle {
      display: flex; align-items: center; justify-content: space-between;
      gap: 1rem; flex-wrap: wrap;
      background: var(--panel); border: 1px solid var(--border);
      padding: 1rem 1.25rem; margin-bottom: 1.25rem;
    }
    .track-toggle .label-block strong { display: block; font-size: 1.05rem; }
    .track-toggle .label-block span { color: var(--muted); font-size: 0.85rem; }
    .switch {
      position: relative; width: 3.4rem; height: 1.85rem; flex-shrink: 0;
    }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider {
      position: absolute; cursor: pointer; inset: 0;
      background: #3a4450; border-radius: 999px; transition: 0.15s;
      border: 1px solid var(--border);
    }
    .slider:before {
      position: absolute; content: ""; height: 1.4rem; width: 1.4rem;
      left: 0.18rem; top: 0.15rem; background: #c5d0dc; border-radius: 50%;
      transition: 0.15s;
    }
    .switch input:checked + .slider { background: #1e5a38; border-color: #2ecc71; }
    .switch input:checked + .slider:before { transform: translateX(1.45rem); background: #2ecc71; }
    .switch.busy { opacity: 0.5; pointer-events: none; }
    tr.clickable { cursor: pointer; }
    tr.clickable:hover td { background: #1a2838; }
    .chips { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.5rem 0 0.75rem; }
    .chip {
      display: inline-flex; gap: 0.35rem; align-items: center;
      font-family: var(--mono); font-size: 0.75rem;
      padding: 0.2rem 0.5rem; border-radius: 3px;
      border: 1px solid var(--border); background: #121820; color: #b8c7d9;
    }
    .chip.site { border-color: #3d6a9a; color: #7eb6e8; }
    .chip.symbol { border-color: #3a6a4a; color: #6dce8a; }
    .chip.section { border-color: #6a5a3a; color: #e6c06d; }
    .chip.surface { border-color: #5a4a6a; color: #c4a8e0; }
    .chip .n { color: var(--muted); }
    .saw { color: var(--muted); font-size: 0.85rem; max-width: 28rem; }
    .chip.buttony {
      cursor: pointer; appearance: none; font: inherit;
    }
    .chip.buttony:hover { border-color: var(--accent); color: var(--accent); }
    .modal-backdrop {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.55);
      z-index: 40; align-items: center; justify-content: center; padding: 1.5rem;
    }
    .modal-backdrop.open { display: flex; }
    .modal {
      background: var(--panel); border: 1px solid var(--border);
      width: min(720px, 100%); max-height: 80vh; overflow: auto;
      padding: 1.25rem 1.4rem; box-shadow: 0 12px 40px rgba(0,0,0,0.45);
    }
    .modal h3 { margin: 0 0 0.35rem; color: var(--text); }
    .modal .sub { margin-bottom: 1rem; }
    .modal table { margin-bottom: 0; }
    .modal-close {
      float: right; background: transparent; border: 1px solid var(--border);
      color: var(--muted); cursor: pointer; padding: 0.25rem 0.55rem;
    }
    .modal-close:hover { color: var(--accent); border-color: var(--accent); }
    """


def _nav(active: str = "home") -> str:
    links = [
        ("/", "Timeline", "home"),
        ("/automations", "Automations", "automations"),
        ("/review", "Claude review", "review"),
        ("/tracking", "Turn on tracking", "tracking"),
    ]
    parts = []
    for href, label, key in links:
        cls = "active" if key == active else ""
        parts.append(f'<a class="{cls}" href="{href}">{_h(label)}</a>')
    return f'<nav class="nav">{"".join(parts)}</nav>'


def _flash_from_qs(qs: str) -> str:
    params = parse_qs(qs or "")
    msg = (params.get("msg") or [""])[0]
    kind = (params.get("kind") or ["ok"])[0]
    if not msg:
        return ""
    cls = "flash"
    if kind == "warn":
        cls += " warn"
    elif kind == "err":
        cls += " err"
    return f'<div class="{cls}">{_h(msg)}</div>'


def _shell(title: str, body: str, active: str = "home") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{_h(title)}</title>
  <style>{_css()}</style>
</head>
<body>
  <h1>Analyst Ledger</h1>
  {_nav(active)}
  {body}
</body>
</html>"""


def _chips_html(chips: list, session_id: str = "") -> str:
    parts = []
    for c in chips:
        kind = _h(c.get("kind") or "surface")
        label = _h(c.get("label") or "")
        detail = _h(c.get("detail") or "")
        detail_html = f'<span class="n">{detail}</span>' if detail else ""
        if session_id:
            parts.append(
                f'<button type="button" class="chip buttony {kind}" '
                f'data-session="{_h(session_id)}" data-kind="{kind}" data-label="{label}" '
                f'title="Show events for {label}">'
                f"{label}{detail_html}</button>"
            )
        else:
            parts.append(f'<span class="chip {kind}">{label}{detail_html}</span>')
    return f'<div class="chips">{"".join(parts)}</div>' if parts else ""


def _confidence_reasons_html(cand: Optional[dict]) -> str:
    if not cand:
        return ""
    reasons = cand.get("confidence_reasons") or []
    recur = cand.get("recurring_symbols") or []
    bits = list(reasons)
    if recur:
        bits.append("watchlist seed: " + ", ".join(recur[:8]))
    if not bits:
        return ""
    items = "".join(f"<li>{_h(b)}</li>" for b in bits)
    return f'<div class="panel"><h3>Why this confidence</h3><ul>{items}</ul></div>'


def _event_popup_script() -> str:
    """Shared modal + chip click handler (filters events for a tag)."""
    return r"""
<div class="modal-backdrop" id="event-modal" role="dialog" aria-modal="true">
  <div class="modal">
    <button type="button" class="modal-close" id="event-modal-close">Close</button>
    <h3 id="event-modal-title">Events</h3>
    <p class="sub" id="event-modal-sub"></p>
    <table>
      <thead><tr><th>Time</th><th>Type</th><th>Surface</th><th>Detail</th></tr></thead>
      <tbody id="event-modal-body"></tbody>
    </table>
  </div>
</div>
<script>
(function () {
  const modal = document.getElementById('event-modal');
  const titleEl = document.getElementById('event-modal-title');
  const subEl = document.getElementById('event-modal-sub');
  const bodyEl = document.getElementById('event-modal-body');
  const closeBtn = document.getElementById('event-modal-close');

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function eventDetail(ev) {
    const p = ev.payload || {};
    if (ev.type === 'url_focus') {
      return (p.host || '') + (p.path || '') + (p.symbol ? (' · ' + p.symbol) : '');
    }
    if (ev.type === 'symbol_focus' || ev.type === 'interval_change' || ev.type === 'drawing_meta') {
      return 'symbol=' + (p.symbol || '') + (p.interval ? (' interval=' + p.interval) : '');
    }
    if (ev.type === 'note') return (p.text || '').slice(0, 280);
    if (ev.type === 'inbox_file') return p.name || p.path || '';
    if (ev.type === 'artifact_attach') return (p.path || '').split(/[\\/]/).pop() || '';
    try { return JSON.stringify(p).slice(0, 280); } catch (e) { return ''; }
  }

  function matches(ev, kind, label) {
    const p = ev.payload || {};
    if (kind === 'site') {
      const host = (p.host || '').toLowerCase();
      return ev.type === 'url_focus' && host === label.toLowerCase();
    }
    if (kind === 'symbol') {
      const sym = String(p.symbol || '').toUpperCase();
      return sym === String(label || '').toUpperCase();
    }
    if (kind === 'section') {
      const sec = String(p.section || '');
      const want = String(label || '').replace(/^\//, '');
      return sec === want;
    }
    if (kind === 'surface') {
      return String(ev.surface || '') === label;
    }
    return false;
  }

  async function openPopup(sessionId, kind, label) {
    titleEl.textContent = label;
    subEl.textContent = 'Loading…';
    bodyEl.innerHTML = '';
    modal.classList.add('open');
    try {
      const res = await fetch('/api/events?session_id=' + encodeURIComponent(sessionId) + '&limit=500');
      const events = await res.json();
      // API returns newest-first; show chronological in popup
      const chrono = Array.isArray(events) ? events.slice().reverse() : [];
      const filtered = chrono.filter(ev => matches(ev, kind, label));
      // Collapse consecutive identical URLs
      const rows = [];
      for (const ev of filtered) {
        const p = ev.payload || {};
        const key = (ev.type === 'url_focus')
          ? String(p.url || '').replace(/\/+$/, '').toLowerCase()
          : (ev.type + '|' + eventDetail(ev));
        const last = rows[rows.length - 1];
        if (last && last._key === key) {
          last.visit_count = (last.visit_count || 1) + 1;
          last.ts = ev.ts;
          continue;
        }
        rows.push({ ...ev, _key: key, visit_count: 1 });
      }
      subEl.textContent = rows.length + ' unique · ' + filtered.length + ' raw · ' + kind;
      if (!rows.length) {
        bodyEl.innerHTML = '<tr><td colspan="4">No matching events</td></tr>';
        return;
      }
      bodyEl.innerHTML = rows.map(ev => {
        let detail = eventDetail(ev);
        if ((ev.visit_count || 1) > 1) detail += ' · ' + ev.visit_count + '×';
        return (
        '<tr>' +
        '<td>' + esc(ev.ts) + '</td>' +
        '<td>' + esc(ev.type) + '</td>' +
        '<td>' + esc(ev.surface) + '</td>' +
        '<td><code>' + esc(detail) + '</code></td>' +
        '</tr>'
      );}).join('');
    } catch (e) {
      subEl.textContent = String(e);
    }
  }

  document.addEventListener('click', (e) => {
    const chip = e.target.closest('button.chip.buttony');
    if (chip) {
      e.preventDefault();
      e.stopPropagation();
      openPopup(chip.dataset.session, chip.dataset.kind, chip.dataset.label);
      return;
    }
    if (e.target === modal || e.target === closeBtn) {
      modal.classList.remove('open');
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') modal.classList.remove('open');
  });
})();
</script>
"""


def _page(ledger: Ledger) -> str:
    from .session_insights import summarize_session_events

    summary = ledger.summary()
    sessions = ledger.list_sessions(limit=30)
    active = summary.get("active_session_id")
    events = ledger.list_events(session_id=active, limit=80) if active else ledger.list_events(limit=80)

    session_rows = []
    for s in sessions:
        cls = "open" if s["status"] == "open" else "closed"
        sid = s["session_id"]
        sess_events = ledger.list_events(session_id=sid, limit=200)
        insight = summarize_session_events(sess_events)
        saw = insight.get("summary_line") or "—"
        chips = _chips_html((insight.get("chips") or [])[:6], session_id=sid)
        outcome = ", ".join(s.get("tags") or []) or "—"
        session_rows.append(
            f"<tr class='{cls} clickable' onclick=\"location.href='/sessions/{_h(sid)}'\">"
            f"<td><a href='/sessions/{_h(sid)}'><code>{_h(sid)}</code></a></td>"
            f"<td><a href='/sessions/{_h(sid)}'>{_h(s['title'])}</a>"
            f"<div class='saw'>{_h(saw)}</div>{chips}</td>"
            f"<td>{_h(s['surface'])}</td>"
            f"<td>{_h(s['sensitivity'])}</td>"
            f"<td>{_h(s['status'])}</td>"
            f"<td>{_h(outcome)}</td>"
            f"<td>{_h(s['started_at'])}</td>"
            f"</tr>"
        )

    event_rows = []
    from .session_insights import collapse_events

    for e in collapse_events(events):
        payload = e.get("payload") or {}
        et = e.get("type")
        vc = int(e.get("visit_count") or 1)
        if et == "url_focus":
            line = f"{payload.get('host') or ''}{payload.get('path') or ''}"
            if payload.get("symbol"):
                line += f" · {payload.get('symbol')}"
            if vc > 1:
                line += f" · {vc}×"
        elif et == "note":
            line = str(payload.get("text") or "")[:180]
        else:
            line = json.dumps(payload, ensure_ascii=False)[:160]
        event_rows.append(
            f"<tr>"
            f"<td>{_h(e['ts'])}</td>"
            f"<td>{_h(et)}</td>"
            f"<td>{_h(e['surface'])}</td>"
            f"<td><code>{_h(line)}</code></td>"
            f"</tr>"
        )

    body = f"""
  <p class="sub">Local system of record · Claude is not in the hot path ·
    <a href="/automations">Automations</a> · <a href="/tracking">Turn on tracking</a>
  </p>
  <div class="cards">
    <div class="card"><div class="n">{summary['sessions']}</div><div class="l">sessions</div></div>
    <div class="card"><div class="n">{summary['open_sessions']}</div><div class="l">open</div></div>
    <div class="card"><div class="n">{summary['events']}</div><div class="l">events</div></div>
    <div class="card"><div class="n">{summary['egress_audits']}</div><div class="l">egress audits</div></div>
    <div class="card"><div class="n">{summary['feedback']}</div><div class="l">feedback</div></div>
  </div>
  <p class="sub">Active session: <code>{_h(active or '(none)')}</code>
    {f"· <a href='/sessions/{_h(active)}'>open active</a>" if active else ""}
    · DB: <code>{_h(summary['db_path'])}</code></p>
  <h2>Sessions</h2>
  <p class="muted">Click a session to see everything it captured (sites, symbols, notes, events).</p>
  <table>
    <thead><tr><th>ID</th><th>Title / what we saw</th><th>Surface</th><th>Sensitivity</th><th>Status</th><th>Outcome</th><th>Started</th></tr></thead>
    <tbody>{''.join(session_rows) or '<tr><td colspan="7">No sessions yet</td></tr>'}</tbody>
  </table>
  <h2>Recent activity {('(active session)' if active else '(all)')}</h2>
  <table>
    <thead><tr><th>Time</th><th>Type</th><th>Surface</th><th>Detail</th></tr></thead>
    <tbody>{''.join(event_rows) or '<tr><td colspan="4">No events yet</td></tr>'}</tbody>
  </table>
"""
    return _shell("Analyst Ledger", body + _event_popup_script(), active="home")


def _session_detail_page(ledger: Ledger, session_id: str) -> str:
    from .session_insights import collapse_events, summarize_session_events

    session = ledger.get_session(session_id)
    if not session:
        body = f'<p class="sub">Session not found.</p><p><a href="/">← Timeline</a></p>'
        return _shell("Session · Analyst Ledger", body, active="home")

    events = list(reversed(ledger.list_events(session_id=session_id, limit=500)))
    insight = summarize_session_events(events)
    chips = _chips_html(insight.get("chips") or [], session_id=session_id)

    notes = insight.get("notes") or []
    pages = insight.get("pages") or []
    note_list = "".join(f"<li>{_h(n)}</li>" for n in notes) or "<li class='muted'>(no notes)</li>"

    page_rows = []
    for p in pages:
        visits = int(p.get("visits") or 1)
        visit_label = f"{visits}×" if visits > 1 else "1×"
        sym = p.get("symbol") or "—"
        path = p.get("path") or p.get("url") or ""
        sec = p.get("section") or "—"
        q = p.get("quote") or {}
        if q.get("price") is not None:
            chg = q.get("change_pct")
            qline = f"{q['price']}"
            if chg is not None:
                qline += f" ({chg:+.2f}%)" if isinstance(chg, (int, float)) else f" ({chg})"
            if q.get("earnings"):
                qline += f" · {q['earnings']}"
        else:
            qline = "—"
        page_rows.append(
            f"<tr>"
            f"<td><code>{_h(visit_label)}</code></td>"
            f"<td><strong>{_h(sym)}</strong></td>"
            f"<td><code>{_h(sec)}</code></td>"
            f"<td><code>{_h(path)}</code></td>"
            f"<td class='muted'>{_h(qline)}</td>"
            f"</tr>"
        )

    collapsed = collapse_events(list(reversed(events)))  # newest-first display
    event_rows = []
    for e in collapsed:
        payload = e.get("payload") or {}
        et = e.get("type")
        vc = int(e.get("visit_count") or 1)
        if et == "url_focus":
            line = f"{payload.get('host') or ''}{payload.get('path') or ''}"
            if payload.get("symbol"):
                line += f" · {payload.get('symbol')}"
            if payload.get("section"):
                line += f" · /{payload.get('section')}"
            q = payload.get("quote") or {}
            if q.get("price") is not None:
                line += f" · {q.get('price')}"
                if q.get("change_pct") is not None:
                    line += f" ({q.get('change_pct')}%)"
            if vc > 1:
                line += f" · {vc}×"
        elif et == "symbol_focus":
            line = f"symbol={payload.get('symbol')} interval={payload.get('interval')}"
        elif et == "note":
            line = str(payload.get("text") or "")[:300]
        elif et == "tag":
            line = f"outcome → {payload.get('tag')}"
        elif et == "session_start":
            line = str(payload.get("title") or "session started")
        else:
            line = json.dumps(payload, ensure_ascii=False)[:300]
        event_rows.append(
            f"<tr>"
            f"<td>{_h(e.get('ts'))}</td>"
            f"<td>{_h(et)}</td>"
            f"<td><code>{_h(line)}</code></td>"
            f"</tr>"
        )

    outcome = ", ".join(session.tags) or "—"
    unique_n = insight.get("unique_pages", 0)
    spam_n = insight.get("deduped_visits", 0)
    spam_note = (
        f"<p class='muted'>{spam_n} repeat visit(s) folded into unique pages.</p>"
        if spam_n
        else ""
    )
    body = f"""
  <p class="sub"><a href="/">← Timeline</a></p>
  <h2>{_h(session.title)}</h2>
  <p class="sub"><code>{_h(session.session_id)}</code> · {_h(session.status)} ·
    outcome <strong id="outcome-label">{_h(outcome)}</strong> · started {_h(session.started_at)}</p>

  <div class="panel">
    <h3>Outcome (optional — no notes required)</h3>
    <p class="muted">One click marks how this session ended for pipeline mining.</p>
    <div class="actions" id="outcome-actions">
      <button type="button" data-tag="idea">idea</button>
      <button type="button" data-tag="followup">followup</button>
      <button type="button" data-tag="reject">reject</button>
      <button type="button" data-tag="neutral">neutral</button>
    </div>
    <div id="outcome-status" class="muted"></div>
  </div>

  <div class="panel">
    <h3>What this session saw</h3>
    <p><strong>{_h(insight.get('summary_line'))}</strong></p>
    {chips}
    {spam_note}
    <h3 style="margin-top:1.1rem">Pages ({unique_n} unique)</h3>
    <table>
      <thead><tr><th>Visits</th><th>Symbol</th><th>Tab</th><th>Path</th><th>Quote scrape</th></tr></thead>
      <tbody>{''.join(page_rows) or '<tr><td colspan="5" class="muted">No pages yet</td></tr>'}</tbody>
    </table>
    <h3 style="margin-top:1.1rem">Notes <span class="muted">(optional)</span></h3>
    <ul>{note_list}</ul>
  </div>

  <h2>Activity ({len(collapsed)} rows · {insight.get('event_count', 0)} raw events)</h2>
  <table>
    <thead><tr><th>Time</th><th>Type</th><th>Detail</th></tr></thead>
    <tbody>{''.join(event_rows) or '<tr><td colspan="3">No events</td></tr>'}</tbody>
  </table>
  <script>
  (function () {{
    const sid = {json.dumps(session_id)};
    const status = document.getElementById('outcome-status');
    const label = document.getElementById('outcome-label');
    document.getElementById('outcome-actions').addEventListener('click', async (e) => {{
      const btn = e.target.closest('button[data-tag]');
      if (!btn) return;
      const tag = btn.getAttribute('data-tag');
      status.textContent = 'Saving…';
      try {{
        const res = await fetch('/api/session/tag', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ tag, session_id: sid }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        const tags = (data.session && data.session.tags) || (data.event && data.event.payload && data.event.payload.tags) || [tag];
        label.textContent = tags.join(', ');
        status.textContent = 'Marked ' + tag;
      }} catch (err) {{
        status.textContent = String(err);
      }}
    }});
  }})();
  </script>
"""
    return _shell(f"{session.title} · Session", body + _event_popup_script(), active="home")


def _automations_page(ledger: Ledger, qs: str = "") -> str:
    from .rituals import list_automations

    items = list_automations(ledger)
    flash = _flash_from_qs(qs)
    rows = []
    for a in items:
        badges = []
        if a.get("has_candidate"):
            badges.append('<span class="badge">candidate</span>')
        if a.get("has_spec"):
            badges.append('<span class="badge ok">spec</span>')
        if a.get("approved"):
            badges.append('<span class="badge ok">approved</span>')
        else:
            badges.append('<span class="badge warn">unapproved</span>')
        if (a.get("build") or {}).get("built"):
            badges.append('<span class="badge ok">built</span>')
        wl = ", ".join((a.get("watchlist") or [])[:6]) or "—"
        run = a.get("last_run")
        if run:
            run_label = str(run.get("ts") or "")[:16].replace("T", " ")
            run_label += " (stub)" if run.get("stub") else ""
            if run.get("error_count"):
                run_label += f" · {run['error_count']} err"
        else:
            run_label = "never"
        rid = a["ritual_id"]
        rows.append(
            f"<tr>"
            f"<td><a href='/automations/{_h(rid)}'><code>{_h(rid)}</code></a></td>"
            f"<td>{_h(a.get('confidence'))}</td>"
            f"<td>{_h(a.get('evidence_count'))}</td>"
            f"<td>{_h(a.get('host_family') or '—')}</td>"
            f"<td>{_h(a.get('runner') or '—')}</td>"
            f"<td><code>{_h(wl)}</code></td>"
            f"<td class='muted'>{_h(run_label)}</td>"
            f"<td>{''.join(badges)}</td>"
            f"</tr>"
        )

    empty = ""
    if not items:
        empty = """
  <div class="empty">
    <strong>No automations yet.</strong> Mine looks for repeated research patterns
    (e.g. morning Yahoo checks). You need captured sessions first.
    <ol>
      <li>Open <a href="/tracking">Turn on tracking</a> and start a session / browser capture</li>
      <li>Do the workflow a few times (or lower min sessions below)</li>
      <li>Come back here and click <strong>Mine rituals</strong></li>
      <li>Click a ritual row → Suggest → Approve → Build → Integrate</li>
    </ol>
  </div>
"""

    body = f"""
  {flash}
  <p class="sub">Suggested automations from your local ledger.
    Flow: <strong>mine → open a ritual → suggest → approve → build → integrate / run</strong>.
    Builds never dump restricted notes — only allowlisted fields.
  </p>
  <div class="actions">
    <label class="muted">Min sessions
      <input id="min-sessions" type="number" min="1" max="30" value="3"
        style="width:3.5rem;margin-left:0.35rem;background:#1a222c;border:1px solid #2a3542;color:#e7ecf1;padding:0.35rem;" />
    </label>
    <button class="primary" id="btn-mine" type="button">Mine rituals</button>
  </div>
  <div id="status" class="muted"></div>
  {empty}
  <h2>Automations</h2>
  <table>
    <thead>
      <tr>
        <th>Ritual</th><th>Conf.</th><th>Evidence</th><th>Host</th>
        <th>Runner</th><th>Watchlist</th><th>Last run</th><th>Status</th>
      </tr>
    </thead>
    <tbody>{''.join(rows) or '<tr><td colspan="8">—</td></tr>'}</tbody>
  </table>
  <script>
  document.getElementById('btn-mine').addEventListener('click', async function () {{
    const btn = this;
    const status = document.getElementById('status');
    const minSessions = parseInt(document.getElementById('min-sessions').value, 10) || 2;
    btn.disabled = true;
    status.textContent = 'Mining local sessions…';
    try {{
      const res = await fetch('/api/automations/mine', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ days: 21, min_sessions: minSessions }}),
      }});
      const data = await res.json();
      if (!res.ok) {{
        status.textContent = 'Error: ' + (data.error || res.status);
        btn.disabled = false;
        return;
      }}
      const n = data.count || 0;
      const msg = n
        ? ('Found ' + n + ' automation candidate(s). Click a row to open it.')
        : ('No patterns yet (need ≥' + minSessions + ' similar sessions). Capture more research first.');
      const kind = n ? 'ok' : 'warn';
      location.href = '/automations?msg=' + encodeURIComponent(msg) + '&kind=' + kind;
    }} catch (e) {{
      status.textContent = String(e);
      btn.disabled = false;
    }}
  }});
  </script>
"""
    return _shell("Automations · Analyst Ledger", body, active="automations")


def _automation_detail_page(ritual_id: str, qs: str = "") -> str:
    from .rituals import get_automation_detail

    flash = _flash_from_qs(qs)
    try:
        detail = get_automation_detail(ritual_id)
    except RuntimeError as exc:
        body = f'{flash}<p class="sub">{_h(exc)}</p><p><a href="/automations">← Automations</a></p>'
        return _shell(f"{ritual_id} · Automations", body, active="automations")

    cand = detail.get("candidate") or {}
    spec = detail.get("spec") or {}
    review = detail.get("review_md") or "(no review yet — click Suggest)"
    build = detail.get("build") or {}
    watchlist = detail.get("watchlist") or []
    wl_text = ", ".join(watchlist)
    conf = cand.get("confidence")
    if conf is None:
        conf = (spec.get("source_candidate") or {}).get("confidence")
    ev_count = len(detail.get("active_evidence_sessions") or [])
    enabled = bool(detail.get("enabled", True))
    approved = bool(detail.get("approved"))

    badges = []
    badges.append(
        '<span class="badge ok">enabled</span>'
        if enabled
        else '<span class="badge warn">disabled</span>'
    )
    badges.append(
        '<span class="badge ok">approved</span>'
        if approved
        else '<span class="badge warn">unapproved</span>'
    )
    if build.get("built"):
        badges.append('<span class="badge ok">built</span>')

    evidence_html = []
    for block in detail.get("evidence") or []:
        sid = block["session_id"]
        included = block.get("included", True)
        excl_cls = "" if included else " excluded"
        event_rows = []
        for ev in block.get("events") or []:
            payload = json.dumps(ev.get("payload") or {}, ensure_ascii=False)[:220]
            ev_inc = ev.get("included", True)
            row_cls = "" if ev_inc else "excluded"
            checked = "checked" if ev_inc else ""
            eid = ev.get("event_id") or ""
            event_rows.append(
                f"<tr class='{row_cls}'>"
                f"<td><input type='checkbox' class='ev-toggle' data-event-id='{_h(eid)}' {checked} /></td>"
                f"<td>{_h(ev.get('ts'))}</td>"
                f"<td>{_h(ev.get('type'))}</td>"
                f"<td>{_h(ev.get('surface'))}</td>"
                f"<td><code>{_h(payload)}</code></td>"
                f"</tr>"
            )
        sess_checked = "checked" if included else ""
        evidence_html.append(
            f"<div class='evidence-block{excl_cls}' data-session='{_h(sid)}'>"
            f"<div class='evidence-head'>"
            f"<label class='toggle-row'><input type='checkbox' class='sess-toggle' "
            f"data-session-id='{_h(sid)}' {sess_checked} /> "
            f"<strong>Include session</strong></label>"
            f"<code>{_h(sid)}</code>"
            f"<span class='meta'>{_h(block.get('title'))} · {_h(block.get('status'))} · "
            f"{_h(block.get('started_at'))}</span>"
            f"<span class='meta'>{len(block.get('events') or [])} events</span>"
            f"</div>"
            f"<table class='event-table'><thead><tr>"
            f"<th>On</th><th>Time</th><th>Type</th><th>Surface</th><th>Payload</th>"
            f"</tr></thead><tbody>"
            f"{''.join(event_rows) or '<tr><td colspan=5>No events</td></tr>'}"
            f"</tbody></table></div>"
        )

    if not evidence_html:
        evidence_html.append(
            "<div class='empty'>No evidence sessions linked yet. "
            "Mine rituals after capturing research, or this spec has no candidate.</div>"
        )

    body = f"""
  {flash}
  <p class="sub"><a href="/automations">← Automations</a> · View events, toggle what counts, edit watchlist</p>
  <h2><code>{_h(ritual_id)}</code> {''.join(badges)}</h2>
  <div class="cards">
    <div class="card"><div class="n">{_h(conf if conf is not None else '—')}</div><div class="l">confidence</div></div>
    <div class="card"><div class="n">{_h(ev_count)}</div><div class="l">included sessions</div></div>
    <div class="card"><div class="n">{_h(spec.get('runner') or '—')}</div><div class="l">runner</div></div>
  </div>
  {_confidence_reasons_html(cand)}

  <div class="panel">
    <h3>Edit automation</h3>
    <div class="edit-grid">
      <div>
        <label class="muted">Watchlist (comma-separated)
          <input id="watchlist" type="text" value="{_h(wl_text)}" />
        </label>
        <div class="toggle-row" style="margin-top:0.85rem">
          <input id="enabled" type="checkbox" {"checked" if enabled else ""} />
          <label for="enabled">Automation enabled</label>
        </div>
        <div class="toggle-row">
          <input id="approved" type="checkbox" {"checked" if approved else ""} />
          <label for="approved">Approved for build / integrate</label>
        </div>
      </div>
      <div>
        <p class="muted">Toggle sessions/events below, then Save. Excluded items stay in the ledger but are ignored for suggest/build context.</p>
        <div class="actions" style="margin:0.5rem 0 0">
          <button class="primary" type="button" id="btn-save">Save edits</button>
        </div>
      </div>
    </div>
  </div>

  <h3>Evidence events</h3>
  {''.join(evidence_html)}

  <div class="actions">
    <button type="button" data-act="suggest">1. Suggest</button>
    <button type="button" data-act="approve">2. Approve</button>
    <button class="primary" type="button" data-act="build">3. Build</button>
    <button type="button" data-act="run">4. Run (stub)</button>
    <select id="integrate-target">
      <option value="claude-skill">Claude Skill</option>
      <option value="local">Local environment</option>
      <option value="windows-task">Windows Task Scheduler</option>
    </select>
    <button type="button" data-act="integrate">5. Integrate</button>
  </div>
  <div id="status" class="muted"></div>
  <h3>Review narrative</h3>
  <div class="panel review">{_h(review)}</div>
  <details class="raw">
    <summary>Show raw spec JSON</summary>
    <pre class="out">{_h(json.dumps(spec, indent=2, ensure_ascii=False) if spec else '(none)')}</pre>
  </details>
  <script>
  const RITUAL_ID = {json.dumps(ritual_id)};

  function collectEdits() {{
    const allSessions = [...document.querySelectorAll('.sess-toggle')].map(el => el.dataset.sessionId);
    const includedSessions = [...document.querySelectorAll('.sess-toggle:checked')].map(el => el.dataset.sessionId);
    const excluded_sessions = allSessions.filter(s => !includedSessions.includes(s));
    const excluded_event_ids = [...document.querySelectorAll('.ev-toggle:not(:checked)')]
      .map(el => el.dataset.eventId).filter(Boolean);
    const watchlist = document.getElementById('watchlist').value
      .split(',').map(s => s.trim()).filter(Boolean);
    return {{
      ritual_id: RITUAL_ID,
      watchlist,
      excluded_sessions,
      excluded_event_ids,
      enabled: document.getElementById('enabled').checked,
      approved: document.getElementById('approved').checked,
    }};
  }}

  document.getElementById('btn-save').addEventListener('click', async () => {{
    const status = document.getElementById('status');
    status.textContent = 'Saving…';
    try {{
      const res = await fetch('/api/automations/update', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(collectEdits()),
      }});
      const data = await res.json();
      if (!res.ok) {{
        status.textContent = 'Error: ' + (data.error || res.status);
        return;
      }}
      location.href = '/automations/' + encodeURIComponent(RITUAL_ID)
        + '?msg=' + encodeURIComponent('Saved edits') + '&kind=ok';
    }} catch (e) {{
      status.textContent = String(e);
    }}
  }});

  // Visual feedback when toggling
  document.querySelectorAll('.sess-toggle').forEach(el => {{
    el.addEventListener('change', () => {{
      const block = el.closest('.evidence-block');
      if (block) block.classList.toggle('excluded', !el.checked);
    }});
  }});
  document.querySelectorAll('.ev-toggle').forEach(el => {{
    el.addEventListener('change', () => {{
      const row = el.closest('tr');
      if (row) row.classList.toggle('excluded', !el.checked);
    }});
  }});

  async function act(action) {{
    const status = document.getElementById('status');
    const buttons = document.querySelectorAll('[data-act]');
    buttons.forEach(b => b.disabled = true);
    status.textContent = 'Working on ' + action + '…';
    const body = {{ ritual_id: RITUAL_ID }};
    if (action === 'run') body.stub = true;
    if (action === 'integrate') {{
      body.target = document.getElementById('integrate-target').value;
    }}
    try {{
      const res = await fetch('/api/automations/' + action, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body),
      }});
      const data = await res.json();
      if (!res.ok) {{
        status.textContent = 'Error: ' + (data.error || res.status);
        buttons.forEach(b => b.disabled = false);
        return;
      }}
      if (data.status === 'error' || data.status === 'needs_config') {{
        status.textContent = data.message || data.status;
        buttons.forEach(b => b.disabled = false);
        return;
      }}
      let msg = 'Done: ' + action;
      if (action === 'build' && data.build_dir) msg = 'Built at ' + data.build_dir;
      if (action === 'integrate' && data.dest) msg = 'Integrated → ' + data.dest;
      if (action === 'integrate' && data.task_name) msg = 'Scheduled task registered: ' + data.task_name;
      if (action === 'run' && data.session_id) msg = 'Ran stub scan → session ' + data.session_id;
      if (action === 'suggest') msg = 'Suggestion written. Review the narrative below.';
      if (action === 'approve') msg = 'Approved. You can Build now.';
      location.href = '/automations/' + encodeURIComponent(RITUAL_ID)
        + '?msg=' + encodeURIComponent(msg) + '&kind=ok';
    }} catch (e) {{
      status.textContent = String(e);
      buttons.forEach(b => b.disabled = false);
    }}
  }}
  document.querySelectorAll('[data-act]').forEach(btn => {{
    btn.addEventListener('click', () => act(btn.getAttribute('data-act')));
  }});
  </script>
"""
    return _shell(f"{ritual_id} · Automations", body, active="automations")


def _review_page(ledger: Ledger, qs: str = "") -> str:
    import os as _os

    from .review import list_reviews, read_review
    from .rituals import list_specs

    flash = _flash_from_qs(qs)
    params = parse_qs(qs or "")
    memos = list_reviews()
    selected = (params.get("memo") or [""])[0] or (memos[0]["name"] if memos else "")
    memo_text = read_review(selected) if selected else None

    has_key = bool(_os.environ.get("ANTHROPIC_API_KEY", "").strip())
    mode_note = (
        "Reviews run with <strong>Claude</strong> (ANTHROPIC_API_KEY is set)."
        if has_key
        else "No ANTHROPIC_API_KEY — reviews run with a <strong>local stub</strong> "
        "so you can test the flow; set the key for a real Claude review."
    )

    proposal_rows = []
    for s in list_specs():
        spec = s.get("spec") or {}
        if spec.get("proposed_by") != "claude_review" or s.get("approved"):
            continue
        rid = s["ritual_id"]
        proposal_rows.append(
            f"<tr>"
            f"<td><a href='/automations/{_h(rid)}'><code>{_h(rid)}</code></a></td>"
            f"<td>{_h(spec.get('runner') or '—')}</td>"
            f"<td><code>{_h(', '.join((spec.get('watchlist') or [])[:6]) or '—')}</code></td>"
            f"<td class='muted'>{_h(spec.get('rationale') or '')}</td>"
            f"<td><button type='button' class='approve-btn primary' data-rid='{_h(rid)}'>Approve</button></td>"
            f"</tr>"
        )

    memo_links = []
    for m in memos[:12]:
        cls = " style='font-weight:600'" if m["name"] == selected else ""
        memo_links.append(
            f"<li><a href='/review?memo={_h(m['name'])}'{cls}>{_h(m['name'])}</a></li>"
        )

    body = f"""
  {flash}
  <p class="sub">The review agent reads your recent sessions and run outcomes, judges the
    existing automations, and proposes new ones — as <strong>drafts you approve</strong>.
    Nothing restricted or confidential is included; every model call is egress-audited.</p>
  <div class="panel">
    <p class="muted">{mode_note}</p>
    <div class="actions">
      <label class="muted">Look back
        <select id="review-days">
          <option value="7">7 days</option>
          <option value="14" selected>14 days</option>
          <option value="30">30 days</option>
        </select>
      </label>
      <button class="primary" type="button" id="btn-review">Run review now</button>
    </div>
    <div id="status" class="muted"></div>
  </div>

  <h2>Proposed automations (awaiting your approval)</h2>
  <table>
    <thead><tr><th>Ritual</th><th>Runner</th><th>Watchlist</th><th>Why</th><th></th></tr></thead>
    <tbody>{''.join(proposal_rows) or '<tr><td colspan="5" class="muted">No open proposals — run a review.</td></tr>'}</tbody>
  </table>

  <h2>Review memo</h2>
  <div class="panel review">{_h(memo_text or '(no reviews yet — click Run review now)')}</div>
  {f'<h3>Past reviews</h3><ul>{"".join(memo_links)}</ul>' if memo_links else ''}
  <script>
  document.getElementById('btn-review').addEventListener('click', async function () {{
    const btn = this;
    const status = document.getElementById('status');
    const days = parseInt(document.getElementById('review-days').value, 10) || 14;
    btn.disabled = true;
    status.textContent = 'Reviewing the ledger… (this can take a minute with Claude)';
    try {{
      const res = await fetch('/api/review/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ days }}),
      }});
      const data = await res.json();
      if (!res.ok) {{
        status.textContent = 'Error: ' + (data.error || res.status);
        btn.disabled = false;
        return;
      }}
      const n = (data.proposals_written || []).length;
      const msg = 'Review done via ' + data.destination + ' — ' + n + ' new proposal(s).';
      location.href = '/review?msg=' + encodeURIComponent(msg) + '&kind=ok';
    }} catch (e) {{
      status.textContent = String(e);
      btn.disabled = false;
    }}
  }});
  document.querySelectorAll('.approve-btn').forEach(btn => {{
    btn.addEventListener('click', async () => {{
      const rid = btn.getAttribute('data-rid');
      btn.disabled = true;
      try {{
        const res = await fetch('/api/automations/approve', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ ritual_id: rid }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        location.href = '/review?msg=' + encodeURIComponent('Approved ' + rid +
          ' — open Automations to build & schedule it.') + '&kind=ok';
      }} catch (e) {{
        btn.disabled = false;
        document.getElementById('status').textContent = String(e);
      }}
    }});
  }});
  </script>
"""
    return _shell("Claude review · Analyst Ledger", body, active="review")


def _tracking_page(ledger: Ledger, qs: str = "") -> str:
    flash = _flash_from_qs(qs)
    active_id = ledger.get_active_session_id()
    active = ledger.get_session(active_id) if active_id else None
    is_on = bool(active and active.status == "open")

    if is_on:
        toggle_sub = f"Session <code>{_h(active.session_id)}</code> · {_h(active.title)}"
        detail_panel = f"""
  <div class="panel">
    <h3>While tracking is on</h3>
    <p class="muted">Browser captures attach automatically. Notes are optional.</p>
    <h3>Outcome</h3>
    <p class="muted">Tag anytime — used when mining pipelines (no note required).</p>
    <div class="actions" id="outcome-actions">
      <button type="button" data-tag="idea">idea</button>
      <button type="button" data-tag="followup">followup</button>
      <button type="button" data-tag="reject">reject</button>
      <button type="button" data-tag="neutral">neutral</button>
    </div>
    <label class="muted" style="margin-top:0.75rem;display:block">Add note (optional)
      <textarea id="note-text" placeholder="Optional — only if you want free text"></textarea>
    </label>
    <div class="actions">
      <button class="primary" type="button" id="btn-note">Save note</button>
      <label class="muted">End tag (when you turn the switch off)
        <select id="end-tag">
          <option value="neutral">neutral</option>
          <option value="idea">idea</option>
          <option value="followup">followup</option>
          <option value="reject">reject</option>
        </select>
      </label>
    </div>
    <div id="status" class="muted"></div>
  </div>
"""
        start_fields = ""
    else:
        toggle_sub = "Flip the switch on to begin a research session"
        detail_panel = '<div id="status" class="muted"></div>'
        start_fields = """
  <div class="panel" id="start-options">
    <h3>New session settings</h3>
    <p class="muted">Used the next time you flip tracking <strong>on</strong>.</p>
    <label class="muted">Title
      <input id="session-title" type="text" value="AM research" />
    </label>
    <label class="muted" style="display:block;margin-top:0.75rem">Surface
      <select id="session-surface">
        <option value="notes">notes</option>
        <option value="browser">browser</option>
        <option value="tradingview">tradingview</option>
        <option value="cursor">cursor</option>
        <option value="inbox">inbox</option>
      </select>
    </label>
  </div>
"""

    checked = "checked" if is_on else ""
    on_label = "On" if is_on else "Off"

    body = f"""
  {flash}
  <div class="track-toggle">
    <div class="label-block">
      <strong>Session tracking: <span id="toggle-state">{on_label}</span></strong>
      <span>{toggle_sub}</span>
    </div>
    <label class="switch" id="track-switch" title="Toggle session tracking on/off">
      <input type="checkbox" id="tracking-toggle" {checked} />
      <span class="slider"></span>
    </label>
  </div>
  {start_fields}
  {detail_panel}
  <div class="panel">
    <h3>Yahoo Finance Chrome extension</h3>
    <p class="muted">Chrome cannot one-click install private extensions. We copy it to a simple folder and open the install screen for you.</p>
    <div class="actions">
      <button class="primary" type="button" id="btn-install-ext">Install Yahoo extension…</button>
    </div>
    <p id="ext-status" class="muted" style="margin-top:0.75rem"></p>
    <ol>
      <li>Click the button (Finder highlights <code>~/AnalystLedger/Yahoo Capture Extension</code>)</li>
      <li>In Chrome: <strong>Developer mode</strong> → <strong>Load unpacked</strong> → choose that folder</li>
      <li>Keep this dashboard running · open a Yahoo quote · click the extension → Capture</li>
    </ol>
  </div>
  <p class="sub"><a href="/">Timeline</a> · <a href="/automations">Automations</a></p>
  <script>
  const statusEl = document.getElementById('status');
  const toggle = document.getElementById('tracking-toggle');
  const switchEl = document.getElementById('track-switch');
  const stateEl = document.getElementById('toggle-state');
  const extStatus = document.getElementById('ext-status');

  async function post(path, body) {{
    const res = await fetch(path, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body || {{}}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    return data;
  }}

  const installBtn = document.getElementById('btn-install-ext');
  if (installBtn) {{
    installBtn.addEventListener('click', async () => {{
      installBtn.disabled = true;
      extStatus.textContent = 'Staging extension…';
      try {{
        const data = await post('/api/install-extension', {{}});
        extStatus.textContent = 'Folder ready: ' + (data.staged || '') +
          ' — in Chrome: Developer mode → Load unpacked → pick that folder.';
      }} catch (e) {{
        extStatus.textContent = String(e);
      }} finally {{
        installBtn.disabled = false;
      }}
    }});
  }}

  toggle.addEventListener('change', async () => {{
    switchEl.classList.add('busy');
    const wantOn = toggle.checked;
    stateEl.textContent = wantOn ? 'On…' : 'Off…';
    try {{
      if (wantOn) {{
        const titleEl = document.getElementById('session-title');
        const surfaceEl = document.getElementById('session-surface');
        const title = (titleEl && titleEl.value.trim()) || 'Research session';
        const surface = (surfaceEl && surfaceEl.value) || 'notes';
        await post('/api/session/start', {{ title, surface }});
        location.href = '/tracking?msg=' + encodeURIComponent('Tracking ON') + '&kind=ok';
      }} else {{
        const tagEl = document.getElementById('end-tag');
        const tag = (tagEl && tagEl.value) || 'neutral';
        await post('/api/session/end', {{ tags: [tag] }});
        location.href = '/tracking?msg=' + encodeURIComponent('Tracking OFF') + '&kind=ok';
      }}
    }} catch (e) {{
      toggle.checked = !wantOn;
      stateEl.textContent = toggle.checked ? 'On' : 'Off';
      if (statusEl) statusEl.textContent = String(e);
      switchEl.classList.remove('busy');
    }}
  }});

  const noteBtn = document.getElementById('btn-note');
  if (noteBtn) {{
    noteBtn.addEventListener('click', async () => {{
      try {{
        const text = document.getElementById('note-text').value.trim();
        if (!text) {{ statusEl.textContent = 'Write a note first (or skip — notes are optional)'; return; }}
        await post('/api/session/note', {{ text }});
        location.href = '/tracking?msg=' + encodeURIComponent('Note saved') + '&kind=ok';
      }} catch (e) {{ statusEl.textContent = String(e); }}
    }});
  }}

  const outcomeActions = document.getElementById('outcome-actions');
  if (outcomeActions) {{
    outcomeActions.addEventListener('click', async (e) => {{
      const btn = e.target.closest('button[data-tag]');
      if (!btn) return;
      try {{
        const tag = btn.getAttribute('data-tag');
        await post('/api/session/tag', {{ tag }});
        const endTag = document.getElementById('end-tag');
        if (endTag) endTag.value = tag;
        if (statusEl) statusEl.textContent = 'Outcome: ' + tag;
      }} catch (err) {{
        if (statusEl) statusEl.textContent = String(err);
      }}
    }});
  }}
  </script>
"""
    return _shell("Turn on tracking · Analyst Ledger", body, active="tracking")


def _api_session_action(ledger: Ledger, action: str, data: dict) -> dict:
    from .schema import Sensitivity, Surface

    if action == "start":
        title = str(data.get("title") or "Research session").strip()
        surface = str(data.get("surface") or Surface.NOTES.value)
        sensitivity = str(data.get("sensitivity") or Sensitivity.INTERNAL.value)
        # End existing open session first so Start always works from UI
        active = ledger.get_active_session_id()
        if active:
            existing = ledger.get_session(active)
            if existing and existing.status == "open":
                ledger.end_session(session_id=active, tags=["neutral"])
        session = ledger.start_session(
            title=title, surface=surface, sensitivity=sensitivity
        )
        return {"status": "ok", "session": session.to_dict()}
    if action == "note":
        text = str(data.get("text") or "").strip()
        if not text:
            raise ValueError("text required")
        event = ledger.add_note(text)
        return {"status": "ok", "event": event.to_dict()}
    if action == "end":
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        session = ledger.end_session(tags=tags)
        return {"status": "ok", "session": session.to_dict()}
    if action == "tag":
        tag = str(data.get("tag") or "").strip()
        if not tag:
            raise ValueError("tag required")
        event = ledger.add_tag(tag, session_id=data.get("session_id"))
        sid = event.session_id
        session = ledger.get_session(sid) if sid else None
        return {
            "status": "ok",
            "event": event.to_dict(),
            "session": session.to_dict() if session else None,
        }
    raise ValueError(f"unknown session action: {action}")


def _read_body(environ) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return b""
    return environ["wsgi.input"].read(length)


def _parse_json_body(environ) -> dict:
    raw = _read_body(environ).decode("utf-8") or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _cors_headers() -> list:
    # Intentionally no CORS headers: the Chrome extensions reach this server
    # through their manifest host_permissions (exempt from CORS), and ordinary
    # websites must NOT be able to read or write the ledger from the browser.
    return []


def _json_response(
    start_response: Callable,
    payload: Any,
    status: str = "200 OK",
) -> list:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(
        status,
        [("Content-Type", "application/json; charset=utf-8")] + _cors_headers(),
    )
    return [body]


def _html_response(start_response: Callable, html_body: str, status: str = "200 OK") -> list:
    body = html_body.encode("utf-8")
    start_response(status, [("Content-Type", "text/html; charset=utf-8")])
    return [body]


def _ingest_tv(ledger: Ledger, data: dict) -> dict:
    from .schema import Event, Sensitivity, Surface

    event_type = data.get("type")
    if not event_type:
        raise ValueError("missing type")
    payload = data.get("payload") or {}
    sensitivity = data.get("sensitivity", Sensitivity.INTERNAL.value)
    sid = data.get("session_id") or ledger.get_active_session_id()
    auto = bool(data.get("auto_session"))
    if not sid and auto:
        symbol = payload.get("symbol") or "session"
        session = ledger.start_session(
            title=data.get("title") or f"TV {symbol}",
            surface=Surface.TRADINGVIEW.value,
            sensitivity=sensitivity,
        )
        sid = session.session_id
    if event_type == "note" and sid:
        text = str(payload.get("text") or "")
        if not text:
            raise ValueError("note payload.text required")
        event = ledger.add_note(
            text=text,
            session_id=sid,
            sensitivity=sensitivity,
            surface=Surface.TRADINGVIEW.value,
        )
        return event.to_dict()
    event = Event(
        type=event_type,
        surface=Surface.TRADINGVIEW.value,
        session_id=sid,
        sensitivity=sensitivity,
        payload=payload,
    )
    return ledger.append_event(event).to_dict()


def _ingest_browser(ledger: Ledger, data: dict) -> dict:
    from .browser import merge_quote_scrape, normalize_url_key, parse_url
    from .schema import Event, Sensitivity, Surface
    from .session_insights import recent_url_focus_same

    url = data.get("url") or (data.get("payload") or {}).get("url")
    if not url:
        raise ValueError("url required")
    parsed = parse_url(url)
    # Allow client to attach title without overriding parse
    title = data.get("title") or (data.get("payload") or {}).get("title")
    if title:
        parsed["title"] = str(title)[:300]
    scrape = data.get("scrape") or data.get("quote")
    parsed = merge_quote_scrape(parsed, scrape if isinstance(scrape, dict) else None)
    sensitivity = data.get("sensitivity", Sensitivity.INTERNAL.value)
    sid = data.get("session_id") or ledger.get_active_session_id()
    auto = bool(data.get("auto_session"))
    if not sid and auto:
        label = parsed.get("symbol") or parsed.get("host") or "browser"
        session = ledger.start_session(
            title=data.get("session_title") or f"Browser {label}",
            surface=Surface.BROWSER.value,
            sensitivity=sensitivity,
        )
        sid = session.session_id

    # Server-side dedupe: same page within 90s → don't spam (unless richer quote scrape)
    force = bool(data.get("force"))
    if sid and not force:
        recent = ledger.list_events(session_id=sid, limit=40)
        hit = recent_url_focus_same(
            recent, normalize_url_key(parsed["url"]), within_seconds=90.0
        )
        if hit:
            old_q = (hit.get("payload") or {}).get("quote") or {}
            new_q = parsed.get("quote") or {}
            upgrading = bool(new_q.get("price") is not None and old_q.get("price") is None)
            if not upgrading:
                out = dict(hit)
                out["deduped"] = True
                return out

    event = Event(
        type="url_focus",
        surface=Surface.BROWSER.value,
        session_id=sid,
        sensitivity=sensitivity,
        payload=parsed,
    )
    return ledger.append_event(event).to_dict()


def _require_ritual_id(data: dict) -> str:
    from .rituals import _validate_ritual_id

    return _validate_ritual_id(str(data.get("ritual_id") or ""))


def _api_automations_action(ledger: Ledger, action: str, data: dict) -> dict:
    from . import rituals as rituals_mod
    from .runners import resolve_runner

    if action == "mine":
        days = int(data.get("days") or 21)
        min_sessions = int(data.get("min_sessions") or 3)
        candidates = rituals_mod.mine_rituals(
            ledger=ledger, days=days, min_sessions=min_sessions
        )
        return {"status": "ok", "count": len(candidates), "candidates": candidates}

    ritual_id = _require_ritual_id(data)

    if action == "suggest":
        dest = data.get("destination") or rituals_mod.default_suggest_destination()
        return rituals_mod.suggest_ritual(
            ritual_id, ledger=ledger, destination=dest, dry_run=bool(data.get("dry_run"))
        )
    if action == "approve":
        return {"status": "ok", "spec": rituals_mod.approve_spec(ritual_id)}
    if action == "update":
        watchlist = data.get("watchlist")
        if isinstance(watchlist, str):
            watchlist = [s.strip() for s in watchlist.split(",") if s.strip()]
        return rituals_mod.update_automation(
            ritual_id,
            watchlist=watchlist if watchlist is not None else None,
            excluded_sessions=data.get("excluded_sessions"),
            excluded_event_ids=data.get("excluded_event_ids"),
            approved=data.get("approved") if "approved" in data else None,
            enabled=data.get("enabled") if "enabled" in data else None,
            note_hints=data.get("note_hints"),
        )
    if action == "build":
        return rituals_mod.build_ritual(
            ritual_id,
            ledger=ledger,
            require_approved=bool(data.get("require_approved")),
        )
    if action == "integrate":
        target = str(data.get("target") or "claude-skill")
        return rituals_mod.integrate_ritual(ritual_id, target=target, ledger=ledger)
    if action == "run":
        stub = bool(data.get("stub", True))
        symbols = data.get("symbols")
        watchlist = None
        if isinstance(symbols, list):
            watchlist = [str(s).strip().upper() for s in symbols if str(s).strip()]
        elif isinstance(symbols, str) and symbols.strip():
            watchlist = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        runner_name, runner_fn = resolve_runner(
            ritual_id, explicit=(data.get("runner") or "").strip() or None
        )
        result = runner_fn(
            ledger=ledger,
            watchlist=watchlist,
            ritual_id=ritual_id,
            stub=stub,
            require_approved=bool(data.get("require_approved")),
        )
        result.setdefault("runner", runner_name)
        return result
    raise ValueError(f"unknown action: {action}")


def make_app(ledger: Optional[Ledger] = None):
    ledger = ledger or Ledger()

    def app(environ, start_response):
        path = environ.get("PATH_INFO") or "/"
        method = (environ.get("REQUEST_METHOD") or "GET").upper()

        if method == "OPTIONS":
            start_response("204 No Content", _cors_headers())
            return [b""]

        # --- Automations UI ---
        if path == "/automations" and method == "GET":
            qs = environ.get("QUERY_STRING") or ""
            return _html_response(start_response, _automations_page(ledger, qs))

        if path == "/tracking" and method == "GET":
            qs = environ.get("QUERY_STRING") or ""
            return _html_response(start_response, _tracking_page(ledger, qs))

        if path == "/review" and method == "GET":
            qs = environ.get("QUERY_STRING") or ""
            return _html_response(start_response, _review_page(ledger, qs))

        if path == "/api/review/run" and method == "POST":
            try:
                data = _parse_json_body(environ)
                from .review import run_review

                result = run_review(
                    ledger,
                    days=int(data.get("days") or 14),
                    destination=data.get("destination"),
                )
                return _json_response(start_response, result)
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path.startswith("/api/session/") and method == "POST":
            action = path.rsplit("/", 1)[-1]
            try:
                data = _parse_json_body(environ)
                result = _api_session_action(ledger, action, data)
                return _json_response(start_response, result)
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/install-extension" and method == "POST":
            try:
                from .install_extension import install_yahoo_extension

                return _json_response(
                    start_response, install_yahoo_extension(open_ui=True)
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        m = re.match(r"^/automations/([a-zA-Z0-9][a-zA-Z0-9_-]{0,120})$", path or "")
        if m and method == "GET":
            qs = environ.get("QUERY_STRING") or ""
            return _html_response(start_response, _automation_detail_page(m.group(1), qs))

        sm = re.match(r"^/sessions/(sess_[a-zA-Z0-9]+)$", path or "")
        if sm and method == "GET":
            return _html_response(
                start_response, _session_detail_page(ledger, sm.group(1))
            )

        # --- Automations API ---
        if path == "/api/automations" and method == "GET":
            from .rituals import list_automations

            return _json_response(start_response, list_automations(ledger))

        if path.startswith("/api/automations/") and method == "POST":
            action = path.rsplit("/", 1)[-1]
            try:
                data = _parse_json_body(environ)
                result = _api_automations_action(ledger, action, data)
                return _json_response(start_response, result)
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/ingest-tv" and method == "POST":
            try:
                data = _parse_json_body(environ)
                result = _ingest_tv(ledger, data)
                return _json_response(start_response, result)
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/ingest-browser" and method == "POST":
            try:
                data = _parse_json_body(environ)
                result = _ingest_browser(ledger, data)
                return _json_response(start_response, result)
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/rituals":
            from .rituals import load_candidates

            return _json_response(start_response, load_candidates())

        if path == "/api/summary":
            return _json_response(start_response, ledger.summary())
        if path == "/api/events":
            qs = parse_qs(environ.get("QUERY_STRING") or "")
            session_id = (qs.get("session_id") or [None])[0]
            try:
                limit = int((qs.get("limit") or ["200"])[0])
            except ValueError:
                limit = 200
            limit = max(1, min(limit, 1000))
            return _json_response(
                start_response,
                ledger.list_events(session_id=session_id or None, limit=limit),
            )
        if path == "/api/sessions":
            return _json_response(start_response, ledger.list_sessions(limit=100))
        if path in {"/", "/index.html"}:
            return _html_response(start_response, _page(ledger))
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]

    return app


def serve(host: str = "127.0.0.1", port: int = 8788) -> None:
    app = make_app()
    httpd = make_server(host, port, app)
    print(f"Analyst ledger dashboard: http://{host}:{port}/")
    print(f"Automations: http://{host}:{port}/automations")
    httpd.serve_forever()
