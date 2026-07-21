"""Local timeline dashboard (stdlib only, binds to localhost by default)."""

from __future__ import annotations

import html
import json
import os
import re
import threading
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
    .chat-layout {
      display: grid; grid-template-columns: 16rem minmax(0, 1fr);
      min-height: 68vh; border: 1px solid var(--border); background: #0c1015;
    }
    .chat-layout.friend-research {
      grid-template-columns: 16rem minmax(0, 1fr) 15rem;
    }
    .chat-sidebar { border-right: 1px solid var(--border); padding: 0.75rem; }
    .qwen-activity {
      border-left: 1px solid var(--border); padding: 0.9rem 1rem;
      background: #0a0e13; display: flex; flex-direction: column; gap: 0.65rem;
    }
    .qwen-activity h3 {
      margin: 0; font-size: 0.78rem; text-transform: uppercase;
      letter-spacing: 0.06em; color: var(--muted); font-weight: 600;
    }
    .qwen-activity .act-status {
      font-size: 0.95rem; color: var(--text); line-height: 1.35;
    }
    .qwen-activity .act-status.researching { color: var(--accent); }
    .qwen-activity .act-status.failed { color: #f07178; }
    .qwen-activity .act-status.researching::before {
      content: ""; display: inline-block; width: 0.55rem; height: 0.55rem;
      border-radius: 50%; background: var(--accent); margin-right: 0.4rem;
      vertical-align: middle; animation: qwen-pulse 1.2s ease-in-out infinite;
    }
    @keyframes qwen-pulse {
      0%, 100% { opacity: 0.35; }
      50% { opacity: 1; }
    }
    .qwen-activity .act-meta {
      color: var(--muted); font-size: 0.82rem; line-height: 1.4;
      white-space: pre-wrap; word-break: break-word;
    }
    .chat-link {
      display: block; padding: 0.7rem; margin-bottom: 0.35rem; text-decoration: none;
      color: var(--text); border: 1px solid transparent; border-radius: 4px;
    }
    .chat-link:hover, .chat-link.active { background: var(--panel); border-color: var(--border); }
    .chat-main { display: flex; flex-direction: column; min-width: 0; }
    .chat-title { padding: 0.9rem 1.1rem; border-bottom: 1px solid var(--border); }
    .chat-messages { flex: 1; overflow: auto; padding: 1rem; }
    .message { max-width: 52rem; padding: 0.8rem 1rem; margin: 0 auto 0.8rem; white-space: pre-wrap; line-height: 1.45; }
    .message.user { background: #1e3a55; margin-right: 0; max-width: 75%; border-radius: 8px; }
    .message.assistant { background: var(--panel); border: 1px solid var(--border); }
    .message.tool, .message.system { color: var(--muted); font-size: 0.86rem; border-left: 2px solid var(--border); }
    .chat-compose { padding: 0.9rem; border-top: 1px solid var(--border); display: flex; gap: 0.6rem; flex-wrap: wrap; position: relative; }
    .chat-compose textarea { min-height: 3rem; margin: 0; flex: 1 1 16rem; }
    .mention-autofill {
      position: absolute; left: 0.9rem; bottom: calc(100% - 0.45rem); z-index: 20;
      min-width: 15rem; padding: 0.3rem; background: #111820;
      border: 1px solid var(--border); border-radius: 6px; box-shadow: 0 10px 30px #0008;
    }
    .mention-autofill[hidden] { display: none; }
    .mention-autofill button {
      display: block; width: 100%; padding: 0.5rem 0.65rem; text-align: left;
      color: var(--text); background: transparent; border: 0;
    }
    .mention-autofill button:hover, .mention-autofill button.active { background: #1e3a55; }
    .arena-opt {
      width: 100%; border: 1px solid var(--border); background: #0a0e13;
      padding: 0.65rem 0.8rem; border-radius: 4px;
    }
    .arena-opt summary {
      cursor: pointer; color: var(--muted); font-size: 0.9rem; list-style: none;
    }
    .arena-opt summary::-webkit-details-marker { display: none; }
    .arena-opt[open] summary { color: var(--accent); margin-bottom: 0.55rem; }
    .arena-opt .row { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: end; }
    .arena-opt label { color: var(--muted); font-size: 0.82rem; display: block; }
    .arena-opt select {
      display: block; margin-top: 0.25rem; background: #1a222c;
      border: 1px solid var(--border); color: var(--text); padding: 0.4rem 0.5rem;
    }
    .arena-opt .hint { color: var(--muted); font-size: 0.8rem; margin: 0.45rem 0 0; }
    .arena-shell { border: 1px solid var(--border); background: #0c1015; min-height: 72vh; }
    .arena-bar {
      display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center;
      justify-content: space-between; padding: 0.85rem 1rem;
      border-bottom: 1px solid var(--border);
    }
    .arena-split {
      display: grid; grid-template-columns: 1fr 1fr; min-height: 48vh;
    }
    .arena-pane {
      display: flex; flex-direction: column; min-width: 0;
      border-right: 1px solid var(--border);
    }
    .arena-pane:last-child { border-right: 0; }
    .arena-pane-title {
      padding: 0.7rem 1rem; border-bottom: 1px solid var(--border);
      display: flex; justify-content: space-between; gap: 0.5rem; align-items: baseline;
    }
    .arena-pane .chat-messages { flex: 1; }
    .grade-panel {
      display: none; border-top: 1px solid var(--border); padding: 1rem 1.1rem;
      background: #0a0e13;
    }
    .grade-panel.open { display: block; }
    .grade-grid {
      display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 0.75rem;
    }
    .grade-grid label { display: block; color: var(--muted); font-size: 0.82rem; margin-top: 0.45rem; }
    .grade-grid input[type=range] { width: 100%; }
    .winner-row { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; margin-top: 0.5rem; }
    .winner-row label { color: var(--text); font-size: 0.9rem; }
    .chat-group {
      color: var(--muted); font-size: 0.72rem; text-transform: uppercase;
      letter-spacing: 0.06em; padding: 0.85rem 0.9rem 0.35rem;
    }
    .chat-group:first-child { padding-top: 0.35rem; }
    .chat-link.friend { border-left: 2px solid var(--accent); }
    .chat-title-row {
      display: flex; flex-wrap: wrap; gap: 0.65rem; align-items: center;
      justify-content: space-between;
    }
    .chat-title-row .chat-title { border-bottom: 0; flex: 1; }
    .qwen-room-actions { display: flex; flex-wrap: wrap; gap: 0.45rem; align-items: center; }
    .qwen-room-actions button {
      background: transparent; color: var(--text); border: 1px solid var(--border);
      border-radius: 6px; padding: 0.35rem 0.7rem; cursor: pointer; font: inherit;
    }
    .qwen-room-actions button.primary {
      background: var(--accent); color: #061018; border-color: var(--accent); font-weight: 600;
    }
    .qwen-room-actions button.danger {
      color: #f07178; border-color: color-mix(in srgb, #f07178 45%, var(--border));
    }
    .qwen-room-actions .pill {
      color: var(--muted); font-size: 0.8rem; border: 1px solid var(--border);
      border-radius: 999px; padding: 0.2rem 0.55rem;
    }
    .qwen-room-actions .pill.on { color: var(--open); border-color: color-mix(in srgb, var(--open) 50%, var(--border)); }
    @media (max-width: 760px) {
      .chat-layout, .chat-layout.friend-research { grid-template-columns: 1fr; }
      .chat-sidebar { border-right: 0; border-bottom: 1px solid var(--border); }
      .qwen-activity { border-left: 0; border-top: 1px solid var(--border); }
      .arena-split, .grade-grid { grid-template-columns: 1fr; }
      .arena-pane { border-right: 0; border-bottom: 1px solid var(--border); }
    }
    """


def _nav(active: str = "home") -> str:
    links = [
        ("/", "Timeline", "home"),
        ("/automations", "Automations", "automations"),
        ("/review", "Claude review", "review"),
        ("/chats", "Chats", "chats"),
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
        can_run = bool(a.get("approved") and a.get("enabled", True))
        has_model = bool(a.get("model"))
        if can_run and has_model:
            run_button = (
                f"<button type='button' onclick='runAutomation({json.dumps(rid)})'>Run</button>"
            )
        elif can_run:
            run_button = (
                f"<a href='/automations/{_h(rid)}'><button type='button' "
                f"title='Choose Claude or Qwen before first run'>Choose model</button></a>"
            )
        else:
            run_button = (
                "<button type='button' disabled title='Approve and enable before running'>Run</button>"
            )
        model_label = a.get("model") or "—"
        if model_label in {"qwen3-8b", "qwen2.5-7b"}:
            model_label = "Qwen3 8B"
        elif model_label == "claude":
            model_label = "Claude"
        rows.append(
            f"<tr>"
            f"<td><a href='/automations/{_h(rid)}'><code>{_h(rid)}</code></a></td>"
            f"<td>{_h(a.get('confidence'))}</td>"
            f"<td>{_h(a.get('evidence_count'))}</td>"
            f"<td>{_h(a.get('host_family') or '—')}</td>"
            f"<td>{_h(a.get('runner') or '—')}</td>"
            f"<td>{_h(model_label)}</td>"
            f"<td><code>{_h(wl)}</code></td>"
            f"<td class='muted'>{_h(run_label)}</td>"
            f"<td>{''.join(badges)}</td>"
            f"<td>{run_button}</td>"
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
    <button class="primary" id="btn-create" type="button">Create new automation</button>
  </div>
  <div id="status" class="muted"></div>
  {empty}
  <h2>Automations</h2>
  <table>
    <thead>
      <tr>
        <th>Ritual</th><th>Conf.</th><th>Evidence</th><th>Host</th>
        <th>Runner</th><th>Model</th><th>Watchlist</th><th>Last run</th><th>Status</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>{''.join(rows) or '<tr><td colspan="10">—</td></tr>'}</tbody>
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

  async function pollJob(jobId, done) {{
    for (;;) {{
      const res = await fetch('/api/jobs/' + encodeURIComponent(jobId));
      const job = await res.json();
      document.getElementById('status').textContent = job.progress || job.status;
      if (['completed', 'failed', 'cancelled'].includes(job.status)) {{
        if (job.status !== 'completed') throw new Error(job.error || job.status);
        done(job);
        return;
      }}
      await new Promise(resolve => setTimeout(resolve, 800));
    }}
  }}

  document.getElementById('btn-create').addEventListener('click', async function () {{
    const btn = this;
    const status = document.getElementById('status');
    btn.disabled = true;
    status.textContent = 'Claude is reviewing recent redacted research…';
    try {{
      const res = await fetch('/api/automations/create', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'
      }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.status);
      await pollJob(data.job_id, () => {{
        location.href = '/automations?msg=' + encodeURIComponent(
          'Claude created draft automation(s). Review and approve before running.'
        ) + '&kind=ok';
      }});
    }} catch (e) {{
      status.textContent = 'Error: ' + e;
      btn.disabled = false;
    }}
  }});

  async function runAutomation(ritualId) {{
    const status = document.getElementById('status');
    status.textContent = 'Starting research…';
    try {{
      const res = await fetch('/api/automations/run', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ritual_id: ritualId, stub: false}})
      }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.status);
      if (data.status === 'needs_config') {{
        location.href = '/automations/' + encodeURIComponent(ritualId)
          + '?msg=' + encodeURIComponent(data.message || 'Choose an agent model first')
          + '&kind=warn';
        return;
      }}
      location.href = '/chats?ritual_id=' + encodeURIComponent(ritualId)
        + '&job_id=' + encodeURIComponent(data.job_id);
    }} catch (e) {{ status.textContent = 'Error: ' + e; }}
  }}
  </script>
"""
    return _shell("Automations · Analyst Ledger", body, active="automations")


def _automation_detail_page(ritual_id: str, qs: str = "") -> str:
    from .models import list_agent_models, model_label, normalize_agent_model
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
    agent_model = normalize_agent_model(spec.get("model"))
    model_options = []
    model_options.append(
        f'<option value="" {"selected" if not agent_model else ""}>'
        "Choose before first run…</option>"
    )
    for m in list_agent_models():
        sel = "selected" if agent_model == m["id"] else ""
        model_options.append(
            f'<option value="{_h(m["id"])}" {sel}>{_h(m["label"])} — {_h(m["description"])}</option>'
        )
    model_hint = (
        f"Using <strong>{_h(model_label(agent_model))}</strong> for research runs. "
        "Change anytime and Save."
        if agent_model
        else "Pick <strong>Claude</strong> or <strong>Qwen3 8B</strong> before the first run."
    )

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
    if agent_model:
        badges.append(f'<span class="badge ok">{_h(model_label(agent_model))}</span>')
    else:
        badges.append('<span class="badge warn">model unset</span>')
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
        <label class="muted">Agent model
          <select id="agent-model" style="display:block;width:100%;margin-top:0.35rem;background:#1a222c;border:1px solid #2a3542;color:#e7ecf1;padding:0.45rem;">
            {''.join(model_options)}
          </select>
        </label>
        <p class="muted" style="margin-top:0.5rem">{model_hint}</p>
        <label class="muted" style="display:block;margin-top:0.85rem">Watchlist (comma-separated)
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
    <button type="button" data-act="run" {"disabled" if not approved or not enabled else ""}>4. Run with agent</button>
    <select id="integrate-target">
      <option value="claude-skill">Claude Skill</option>
      <option value="local">Local (cron / OpenClaw / launchd)</option>
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
      model: document.getElementById('agent-model').value,
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

  const AGENT_MODEL = {json.dumps(agent_model)};

  async function act(action) {{
    const status = document.getElementById('status');
    const buttons = document.querySelectorAll('[data-act]');
    buttons.forEach(b => b.disabled = true);
    status.textContent = 'Working on ' + action + '…';
    const body = {{ ritual_id: RITUAL_ID }};
    if (action === 'run') {{
      body.stub = false;
      const chosen = document.getElementById('agent-model').value;
      if (!chosen && !AGENT_MODEL) {{
        status.textContent = 'Choose Claude or Qwen3 8B above, then Save, before the first run.';
        document.getElementById('agent-model').focus();
        buttons.forEach(b => b.disabled = false);
        return;
      }}
      if (chosen && chosen !== AGENT_MODEL) {{
        status.textContent = 'Save the agent model choice first, then Run.';
        buttons.forEach(b => b.disabled = false);
        return;
      }}
    }}
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
        if (data.status === 'needs_config') {{
          document.getElementById('agent-model').focus();
        }}
        return;
      }}
      if (action === 'run' && data.job_id) {{
        location.href = '/chats?ritual_id=' + encodeURIComponent(RITUAL_ID)
          + '&job_id=' + encodeURIComponent(data.job_id);
        return;
      }}
      let msg = 'Done: ' + action;
      if (action === 'build' && data.build_dir) msg = 'Built at ' + data.build_dir;
      if (action === 'integrate' && data.dest) msg = 'Integrated → ' + data.dest;
      if (action === 'integrate' && data.task_name) msg = 'Scheduled task registered: ' + data.task_name;
      if (action === 'run' && data.session_id) msg = 'Ran stub scan → session ' + data.session_id;
      if (action === 'suggest') msg = 'Suggestion written. Review the narrative below.';
      if (action === 'approve') msg = 'Approved. Choose an agent model, then Build / Run.';
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
        if spec.get("proposed_by") not in {"claude_review", "chat_mining"} or s.get(
            "approved"
        ):
            continue
        rid = s["ritual_id"]
        why = ("[chat] " if spec.get("proposed_by") == "chat_mining" else "") + str(
            spec.get("rationale") or ""
        )
        proposal_rows.append(
            f"<tr>"
            f"<td><a href='/automations/{_h(rid)}'><code>{_h(rid)}</code></a></td>"
            f"<td>{_h(spec.get('runner') or '—')}</td>"
            f"<td><code>{_h(', '.join((spec.get('watchlist') or [])[:6]) or '—')}</code></td>"
            f"<td class='muted'>{_h(why)}</td>"
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


def _render_chat_bubbles(messages: list) -> str:
    rendered = []
    for event in messages:
        payload = event.get("payload") or {}
        role = str(payload.get("role") or "assistant")
        content = str(payload.get("content") or "")
        rendered.append(
            f"<div class='message {_h(role)}'><div>{_h(content)}</div></div>"
        )
    return "".join(rendered)


def _qwen_panel_html() -> str:
    from .friend_qwen import qwen_status

    st = qwen_status()
    enabled = bool(st.get("enabled"))
    personalities = st.get("personalities") or []
    mentions = ", ".join(
        f"<code>{_h(str(p.get('mention') or ''))}</code>"
        for p in personalities
        if p.get("mention")
    )
    status_line = (
        f"In the Friend conversation — call {mentions}. Add "
        "<code>research …</code> after a mention for background research."
        if enabled
        else "Not in the conversation yet."
    )
    btn = "Remove from conversation" if enabled else "Add to Friend conversation"
    return f"""
      <div class="chat-title"><strong>Qwen personalities</strong>
        <span id="job-status" class="muted"></span>
      </div>
      <div class="chat-messages" id="chat-messages" style="padding:1.25rem">
        <div class="message system">
          <p style="margin:0 0 0.75rem">Add your local Qwen model as distinct voices in
            <a href="/chats?thread_id=friend">Friend</a>. Use <code>@Qwen</code> for a
            balanced answer or <code>@Qwen-Contrarian</code> to challenge assumptions.
            Either personality can run background research.</p>
          <p style="margin:0 0 0.75rem" id="qwen-status-line">{status_line}</p>
          <p class="hint" style="margin:0 0 1rem;color:var(--muted);font-size:0.85rem">
            Requires Ollama/vLLM (<code>ANALYST_QWEN_BASE_URL</code>) and Friend messenger
            env vars. Research uses public web search from room context only — not the ledger.</p>
          <button type="button" class="primary" id="qwen-toggle"
            data-enabled="{1 if enabled else 0}" data-stay="qwen">{_h(btn)}</button>
        </div>
      </div>
      <form class="chat-compose" id="chat-form" style="display:none"></form>
"""


def _chats_page(ledger: Ledger, qs: str = "") -> str:
    from .friend_qwen import QWEN_THREAD_ID, qwen_status
    from .messenger_bridge import (
        FRIEND_THREAD_ID,
        MessengerBridgeError,
        friend_thread_meta,
        list_friend_messages,
        messenger_configured,
    )
    from .rituals import list_automations

    params = parse_qs(qs or "")
    master = ledger.get_or_create_chat_thread(master=True)
    for automation in list_automations(ledger):
        if automation.get("has_spec"):
            ledger.get_or_create_chat_thread(automation["ritual_id"])
    agent_threads = ledger.list_chat_threads()
    friend = friend_thread_meta()
    qwen_meta = {"session_id": QWEN_THREAD_ID, "title": "Qwen", "qwen": True}
    requested_thread = (params.get("thread_id") or [""])[0]
    requested_ritual = (params.get("ritual_id") or [""])[0]
    is_friend = requested_thread == FRIEND_THREAD_ID
    is_qwen = requested_thread == QWEN_THREAD_ID
    selected: dict
    if is_friend:
        selected = friend
    elif is_qwen:
        selected = qwen_meta
    else:
        selected = next(
            (
                t
                for t in agent_threads
                if t["session_id"] == requested_thread
                or (requested_ritual and t.get("ritual_id") == requested_ritual)
            ),
            next((t for t in agent_threads if t.get("master")), None),
        ) or {
            "session_id": master.session_id,
            "title": master.title,
            "master": True,
            "ritual_id": None,
        }

    qwen_state = qwen_status()
    qwen_on = bool(qwen_state.get("enabled"))
    friend_active = " active" if is_friend else ""
    qwen_active = " active" if is_qwen else ""
    qwen_label = "Qwen personalities · in chat" if qwen_on else "Qwen personalities"
    people_links = [
        f"<div class='chat-group'>People</div>"
        f"<a class='chat-link friend{friend_active}' "
        f"href='/chats?thread_id={_h(FRIEND_THREAD_ID)}'>Friend</a>"
        f"<a class='chat-link{qwen_active}' "
        f"href='/chats?thread_id={_h(QWEN_THREAD_ID)}'>{_h(qwen_label)}</a>"
    ]
    agent_links = ["<div class='chat-group'>Agents</div>"]
    for thread in agent_threads:
        active = (
            " active"
            if (
                not is_friend
                and not is_qwen
                and thread["session_id"] == selected["session_id"]
            )
            else ""
        )
        pin = "Master · " if thread.get("master") else ""
        agent_links.append(
            f"<a class='chat-link{active}' href='/chats?thread_id={_h(thread['session_id'])}'>"
            f"{_h(pin + thread['title'])}</a>"
        )
    links = people_links + agent_links

    friend_error = ""
    messages: list = []
    activity_aside = ""
    if is_qwen:
        empty_hint = ""
        main_inner = _qwen_panel_html()
    elif is_friend:
        if not messenger_configured():
            friend_error = (
                "Friend chat needs ANALYST_MESSENGER_URL and ANALYST_MESSENGER_INVITE "
                "in the environment (invite = Fly MESSENGER_INVITE_TOKEN)."
            )
        else:
            try:
                messages = list_friend_messages()
            except MessengerBridgeError as exc:
                friend_error = str(exc)
        empty_hint = (
            friend_error
            or "No messages yet. Try @Qwen or @Qwen-Contrarian."
        )
        bubbles = _render_chat_bubbles(messages)
        if not bubbles:
            bubbles = f"<div class='message system'>{_h(empty_hint)}</div>"
        qwen_btn = (
            "Remove Qwen"
            if qwen_on
            else "Add Qwen to room"
        )
        qwen_pill = (
            '<span class="pill on" id="qwen-room-pill">Qwen voices · @Qwen / @Qwen-Contrarian</span>'
            if qwen_on
            else '<span class="pill" id="qwen-room-pill">Qwen off</span>'
        )
        qwen_st = qwen_state
        act_status = str(qwen_st.get("research_status") or "idle")
        act_progress = str(qwen_st.get("research_progress") or "")
        act_query = str(qwen_st.get("research_query") or "")
        act_error = str(qwen_st.get("research_error") or "")
        main_inner = f"""
      <div class="chat-title-row">
        <div class="chat-title"><strong>Friend</strong>
          <span id="job-status" class="muted"></span>
        </div>
        <div class="qwen-room-actions">
          {qwen_pill}
          <button type="button" class="{'primary' if not qwen_on else ''}" id="qwen-toggle"
            data-enabled="{1 if qwen_on else 0}" data-stay="friend">{_h(qwen_btn)}</button>
          <button type="button" id="friend-clear-chat" class="danger">Delete chat</button>
        </div>
      </div>
      <div class="chat-messages" id="chat-messages">{bubbles}</div>
      <form class="chat-compose" id="chat-form">
        <textarea id="chat-input" placeholder="Message… @Qwen, @Qwen-Contrarian, or @workflow ritual_id"
          required></textarea>
        <button class="primary" type="submit">Send</button>
        <button id="cancel-job" type="button" style="display:none">Cancel</button>
      </form>
"""
        activity_aside = f"""
    <aside class="qwen-activity" id="qwen-activity"
      data-status="{_h(act_status)}"
      data-progress="{_h(act_progress)}"
      data-query="{_h(act_query)}"
      data-error="{_h(act_error)}">
      <h3>Qwen personality activity</h3>
      <div class="act-status {_h(act_status)}" id="qwen-act-status">Loading…</div>
      <div class="act-meta" id="qwen-act-meta"></div>
    </aside>
"""
    else:
        messages = ledger.list_chat_messages(selected["session_id"])
        empty_hint = "No messages yet. Ask a question or run this workflow."
        bubbles = _render_chat_bubbles(messages)
        if not bubbles:
            bubbles = f"<div class='message system'>{_h(empty_hint)}</div>"
        is_master = bool(selected.get("master"))
        ritual_id = selected.get("ritual_id")
        placeholder = (
            "Ask the master to coordinate approved workflows…"
            if is_master
            else f"Ask {_h(ritual_id)} to research…"
        )
        from .models import list_agent_models

        model_opts = "".join(
            f'<option value="{_h(m["id"])}">{_h(m["label"])}</option>'
            for m in list_agent_models()
        )
        arena_block = ""
        if not is_master and ritual_id:
            arena_block = f"""
        <details class="arena-opt" id="arena-opt">
          <summary>Compare two agents (run simultaneously → split view → grade)</summary>
          <div class="row">
            <label>Lane A
              <select id="arena-model-a">{model_opts}</select>
            </label>
            <label>Lane B
              <select id="arena-model-b">{model_opts}</select>
            </label>
            <button type="button" class="primary" id="arena-run">Run simultaneously</button>
          </div>
          <p class="hint">Opt-in evaluation only. Uses disposable arena lanes — does not write into
            this workflow chat, the coding environment, or master handoffs.</p>
        </details>
"""
        main_inner = f"""
      <div class="chat-title"><strong>{_h(selected['title'])}</strong>
        <span id="job-status" class="muted"></span>
      </div>
      <div class="chat-messages" id="chat-messages">{bubbles}</div>
      <form class="chat-compose" id="chat-form">
        <textarea id="chat-input" placeholder="{placeholder}" required></textarea>
        <button class="primary" type="submit">Send</button>
        <button id="cancel-job" type="button" style="display:none">Cancel</button>
        {arena_block}
      </form>
"""

    job_id = (params.get("job_id") or [""])[0]
    is_master = bool(selected.get("master")) and not is_friend and not is_qwen
    ritual_id = selected.get("ritual_id") if not is_friend and not is_qwen else None
    layout_class = "chat-layout friend-research" if is_friend else "chat-layout"

    body = f"""
  <p class="sub">Chat with your friend, call a Qwen personality, or talk to workflow agents.
    Friend/Qwen traffic stays on the cloud messenger; agent threads stay local.
    Try <code>@Qwen</code> or <code>@Qwen-Contrarian</code>; add
    <code>research …</code> for background web research.</p>
  <div class="{layout_class}">
    <aside class="chat-sidebar">{''.join(links)}</aside>
    <section class="chat-main">
      {main_inner}
    </section>
    {activity_aside}
  </div>
  <script>
  const THREAD_ID = {json.dumps(selected["session_id"])};
  const RITUAL_ID = {json.dumps(ritual_id)};
  const IS_MASTER = {json.dumps(is_master)};
  const IS_FRIEND = {json.dumps(is_friend)};
  const IS_QWEN = {json.dumps(is_qwen)};
  const QWEN_MENTIONS = {json.dumps([p.get("mention") for p in qwen_state.get("personalities", []) if p.get("mention")])};
  let activeJob = {json.dumps(job_id)};
  let lastFriendSig = '';
  let lastResearchSig = '';

  function escapeHtml(s) {{
    return String(s).replace(/[&<>"']/g, (c) => ({{
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }})[c]);
  }}

  function renderMessages(list, emptyHint) {{
    const el = document.getElementById('chat-messages');
    if (!el) return;
    if (!list || !list.length) {{
      el.innerHTML = '<div class="message system">' + escapeHtml(emptyHint || 'No messages yet.') + '</div>';
      return;
    }}
    el.innerHTML = list.map((event) => {{
      const payload = event.payload || {{}};
      const role = payload.role || 'assistant';
      const content = payload.content || '';
      return '<div class="message ' + escapeHtml(role) + '"><div>' + escapeHtml(content) + '</div></div>';
    }}).join('');
    el.scrollTop = el.scrollHeight;
  }}

  async function tickQwen() {{
    try {{
      await fetch('/api/chats/friend/qwen/tick', {{method: 'POST'}});
    }} catch (e) {{ /* ignore */ }}
  }}

  function renderQwenActivity(st) {{
    const statusEl = document.getElementById('qwen-act-status');
    const metaEl = document.getElementById('qwen-act-meta');
    if (!statusEl || !metaEl) return;
    const status = (st && st.research_status) || 'idle';
    const progress = (st && st.research_progress) || '';
    const query = (st && st.research_query) || '';
    const error = (st && st.research_error) || '';
    statusEl.className = 'act-status ' + status;
    if (status === 'researching') {{
      statusEl.textContent = 'A Qwen personality is researching';
      metaEl.textContent = [query ? ('Topic: ' + query) : '', progress].filter(Boolean).join('\\n');
    }} else if (status === 'failed') {{
      statusEl.textContent = 'Research failed';
      metaEl.textContent = error || 'Try @Qwen research … again.';
    }} else {{
      statusEl.textContent = 'Qwen personalities idle';
      metaEl.textContent = 'Mention @Qwen or @Qwen-Contrarian.';
    }}
  }}

  async function pollQwenActivity() {{
    if (!IS_FRIEND) return;
    try {{
      const res = await fetch('/api/chats/friend/qwen');
      const data = await res.json();
      if (!res.ok) return;
      const sig = JSON.stringify([
        data.research_status, data.research_progress,
        data.research_query, data.research_error
      ]);
      if (sig !== lastResearchSig) {{
        lastResearchSig = sig;
        renderQwenActivity(data);
      }}
    }} catch (e) {{ /* ignore */ }}
  }}

  async function pollFriend() {{
    if (!IS_FRIEND) return;
    try {{
      await tickQwen();
      await pollQwenActivity();
      const res = await fetch('/api/chats/messages?thread_id=' + encodeURIComponent(THREAD_ID));
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('job-status').textContent = ' · ' + (data.error || res.status);
        return;
      }}
      const sig = JSON.stringify((data || []).map((m) => m.event_id));
      if (sig !== lastFriendSig) {{
        lastFriendSig = sig;
        renderMessages(data, 'No messages yet. Try @Qwen or @Qwen-Contrarian.');
        document.getElementById('job-status').textContent = '';
      }}
    }} catch (e) {{
      document.getElementById('job-status').textContent = ' · ' + e;
    }}
  }}

  async function pollJob(doneHref) {{
    if (!activeJob) return;
    const status = document.getElementById('job-status');
    const cancel = document.getElementById('cancel-job');
    if (cancel) cancel.style.display = '';
    for (;;) {{
      const res = await fetch('/api/jobs/' + encodeURIComponent(activeJob));
      const job = await res.json();
      if (status) status.textContent = ' · ' + (job.progress || job.status);
      if (['completed', 'failed', 'cancelled'].includes(job.status)) {{
        if (cancel) cancel.style.display = 'none';
        if (job.status === 'completed') {{
          location.href = doneHref || ('/chats?thread_id=' + encodeURIComponent(THREAD_ID));
        }} else if (status) {{
          status.textContent = ' · ' + (job.error || job.status);
        }}
        return;
      }}
      await new Promise(resolve => setTimeout(resolve, 800));
    }}
  }}

  function addMentionAutofill(input) {{
    if (!input || !IS_FRIEND || !QWEN_MENTIONS.length) return;
    const menu = document.createElement('div');
    menu.className = 'mention-autofill';
    menu.hidden = true;
    input.parentElement.appendChild(menu);
    let matches = [];
    let active = 0;
    let tokenStart = -1;

    function closeMenu() {{
      menu.hidden = true;
      matches = [];
      tokenStart = -1;
    }}

    function choose(index) {{
      const mention = matches[index];
      if (!mention || tokenStart < 0) return;
      const cursor = input.selectionStart;
      input.value = input.value.slice(0, tokenStart) + mention + ' ' + input.value.slice(cursor);
      const next = tokenStart + mention.length + 1;
      input.setSelectionRange(next, next);
      closeMenu();
      input.focus();
    }}

    function refresh() {{
      const cursor = input.selectionStart;
      const before = input.value.slice(0, cursor);
      const found = before.match(/(^|\\s)(@[\\w-]*)$/);
      if (!found) {{
        closeMenu();
        return;
      }}
      const query = found[2].toLowerCase();
      tokenStart = cursor - found[2].length;
      matches = QWEN_MENTIONS.filter((mention) => mention.toLowerCase().startsWith(query));
      if (!matches.length) {{
        closeMenu();
        return;
      }}
      active = Math.min(active, matches.length - 1);
      menu.innerHTML = matches.map((mention, index) =>
        '<button type="button" class="' + (index === active ? 'active' : '') +
        '" data-index="' + index + '">' + escapeHtml(mention) + '</button>'
      ).join('');
      menu.hidden = false;
    }}

    input.addEventListener('input', refresh);
    input.addEventListener('blur', () => setTimeout(closeMenu, 120));
    input.addEventListener('keydown', (event) => {{
      if (menu.hidden) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {{
        event.preventDefault();
        active = (active + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
        refresh();
      }} else if (event.key === 'Enter' || event.key === 'Tab') {{
        event.preventDefault();
        choose(active);
      }} else if (event.key === 'Escape') {{
        event.preventDefault();
        closeMenu();
      }}
    }});
    menu.addEventListener('mousedown', (event) => {{
      event.preventDefault();
      const button = event.target.closest('button[data-index]');
      if (button) choose(Number(button.dataset.index));
    }});
  }}

  const chatForm = document.getElementById('chat-form');
  if (chatForm && !IS_QWEN) {{
    const chatInput = document.getElementById('chat-input');
    if (chatInput) {{
      addMentionAutofill(chatInput);
      chatInput.addEventListener('keydown', (event) => {{
        if (!event.defaultPrevented && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {{
          event.preventDefault();
          chatForm.requestSubmit();
        }}
      }});
    }}
    chatForm.addEventListener('submit', async (event) => {{
      event.preventDefault();
      const input = document.getElementById('chat-input');
      const content = input.value.trim();
      if (!content) return;
      input.disabled = true;
      try {{
        const res = await fetch('/api/chats/message', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{thread_id: THREAD_ID, content}})
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        input.value = '';
        if (IS_FRIEND) {{
          if (data.workflow && data.workflow.status === 'blocked') {{
            document.getElementById('job-status').textContent =
              ' · Workflow blocked: ' + (data.workflow.error || 'not approved');
          }} else if (data.workflow && data.workflow.job_id) {{
            document.getElementById('job-status').textContent = ' · Workflow running…';
            activeJob = data.workflow.job_id;
            const wfRitual = (data.workflow.ritual_id
              || (content.match(/@workflow\\s+([\\w-]+)/i) || [])[1]
              || '');
            await pollJob(wfRitual ? ('/chats?ritual_id=' + encodeURIComponent(wfRitual)) : null);
          }} else if (/@qwen\\b/i.test(content)) {{
            document.getElementById('job-status').textContent = ' · Qwen personality thinking…';
            await tickQwen();
          }}
          await pollFriend();
          input.disabled = false;
          input.focus();
          return;
        }}
        activeJob = data.job_id;
        await pollJob();
      }} catch (e) {{
        document.getElementById('job-status').textContent = ' · Error: ' + e;
        input.disabled = false;
      }}
    }});
  }}

  const cancelBtn = document.getElementById('cancel-job');
  if (cancelBtn) {{
    cancelBtn.addEventListener('click', async () => {{
      if (!activeJob) return;
      await fetch('/api/jobs/' + encodeURIComponent(activeJob) + '/cancel', {{method:'POST'}});
    }});
  }}

  const arenaBtn = document.getElementById('arena-run');
  if (arenaBtn) {{
    const modelB = document.getElementById('arena-model-b');
    if (modelB && modelB.options.length > 1) modelB.selectedIndex = 1;
    arenaBtn.addEventListener('click', async () => {{
      const input = document.getElementById('chat-input');
      const content = input.value.trim();
      if (!content) {{
        document.getElementById('job-status').textContent = ' · Enter a research request first';
        return;
      }}
      const modelA = document.getElementById('arena-model-a').value;
      const modelBVal = document.getElementById('arena-model-b').value;
      arenaBtn.disabled = true;
      input.disabled = true;
      try {{
        const res = await fetch('/api/arena/start', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            ritual_id: RITUAL_ID,
            request: content,
            model_a: modelA,
            model_b: modelBVal,
            source_thread_id: THREAD_ID,
          }})
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        location.href = '/chats/arena?trial_id=' + encodeURIComponent(data.trial_id);
      }} catch (e) {{
        document.getElementById('job-status').textContent = ' · Arena error: ' + e;
        arenaBtn.disabled = false;
        input.disabled = false;
      }}
    }});
  }}

  const qwenToggle = document.getElementById('qwen-toggle');
  if (qwenToggle) {{
    qwenToggle.addEventListener('click', async () => {{
      const enabled = qwenToggle.dataset.enabled !== '1';
      const stay = qwenToggle.dataset.stay || 'qwen';
      qwenToggle.disabled = true;
      const status = document.getElementById('job-status');
      if (status) status.textContent = enabled ? ' · Adding Qwen personalities…' : ' · Removing…';
      try {{
        const res = await fetch('/api/chats/friend/qwen', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{enabled}})
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        location.href = '/chats?thread_id=' + encodeURIComponent(stay);
      }} catch (e) {{
        if (status) status.textContent = ' · ' + e;
        qwenToggle.disabled = false;
      }}
    }});
  }}

  const friendClear = document.getElementById('friend-clear-chat');
  if (friendClear) {{
    friendClear.addEventListener('click', async () => {{
      if (!confirm('Delete the entire Friend room chat for everyone? This cannot be undone.')) {{
        return;
      }}
      friendClear.disabled = true;
      const status = document.getElementById('job-status');
      if (status) status.textContent = ' · Deleting chat…';
      try {{
        const res = await fetch('/api/chats/friend/clear', {{method: 'POST'}});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        renderMessages([], 'No messages yet. Try @Qwen or @Qwen-Contrarian.');
        lastFriendSig = '[]';
        if (status) status.textContent = ' · Chat deleted';
      }} catch (e) {{
        if (status) status.textContent = ' · ' + e;
      }} finally {{
        friendClear.disabled = false;
      }}
    }});
  }}

  if (IS_FRIEND) {{
    lastFriendSig = {json.dumps(json.dumps([m.get("event_id") for m in messages]))};
    const actPanel = document.getElementById('qwen-activity');
    if (actPanel) {{
      renderQwenActivity({{
        research_status: actPanel.dataset.status || 'idle',
        research_progress: actPanel.dataset.progress || '',
        research_query: actPanel.dataset.query || '',
        research_error: actPanel.dataset.error || ''
      }});
    }}
    setInterval(pollFriend, 2500);
  }} else if (!IS_QWEN) {{
    pollJob();
  }}
  const messagesEl = document.getElementById('chat-messages');
  if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
  </script>
"""
    return _shell("Chats · Analyst Ledger", body, active="chats")


def _render_arena_messages(ledger: Ledger, thread_id: str) -> str:
    messages = ledger.list_chat_messages(thread_id)
    rendered = []
    for event in messages:
        payload = event.get("payload") or {}
        role = str(payload.get("role") or "assistant")
        content = str(payload.get("content") or "")
        rendered.append(
            f"<div class='message {_h(role)}'><div>{_h(content)}</div></div>"
        )
    if not rendered:
        rendered.append(
            "<div class='message system'>Waiting for this lane to start…</div>"
        )
    return "".join(rendered)


def _score_sliders(prefix: str) -> str:
    dims = [
        ("helpfulness", "Helpfulness"),
        ("correctness", "Correctness"),
        ("research_quality", "Research quality"),
        ("concision", "Concision"),
    ]
    parts = []
    for key, label in dims:
        parts.append(
            f"<label>{_h(label)} <span id='{prefix}-{key}-val'>3</span>"
            f"<input type='range' id='{prefix}-{key}' min='1' max='5' step='1' value='3' "
            f"oninput=\"document.getElementById('{prefix}-{key}-val').textContent=this.value\"/>"
            f"</label>"
        )
    return "".join(parts)


def _arena_page(ledger: Ledger, qs: str = "") -> str:
    from .arena import load_trial

    params = parse_qs(qs or "")
    trial_id = (params.get("trial_id") or [""])[0].strip()
    if not trial_id:
        body = """
  <p class="sub">No arena trial selected.</p>
  <p><a href="/chats">← Back to Chats</a></p>
"""
        return _shell("Arena · Analyst Ledger", body, active="chats")
    try:
        trial = load_trial(trial_id)
    except RuntimeError as exc:
        body = f"""
  <p class="sub">{_h(str(exc))}</p>
  <p><a href="/chats">← Back to Chats</a></p>
"""
        return _shell("Arena · Analyst Ledger", body, active="chats")

    lane_a = trial.lanes["a"]
    lane_b = trial.lanes["b"]
    msgs_a = _render_arena_messages(ledger, lane_a.thread_id)
    msgs_b = _render_arena_messages(ledger, lane_b.thread_id)
    back = (
        f"/chats?thread_id={_h(trial.source_thread_id)}"
        if trial.source_thread_id
        else f"/chats?ritual_id={_h(trial.ritual_id)}"
    )
    grade_open = " open" if trial.grade else ""
    grade_saved = ""
    if trial.grade:
        grade_saved = (
            f"<p class='muted'>Grade saved · winner "
            f"<strong>{_h(trial.grade.get('winner'))}</strong> · "
            f"{_h(trial.grade.get('rated_at'))}</p>"
        )
    body = f"""
  <p class="sub">Disposable dual-run arena for <strong>{_h(trial.ritual_id)}</strong>.
    Isolated from the durable workflow chat — grade here to tune which agent config wins.</p>
  <div class="arena-shell">
    <div class="arena-bar">
      <div>
        <strong>Arena trial</strong>
        <span class="muted"> · {_h(trial_id)}</span>
        <div class="muted" style="margin-top:0.25rem;max-width:48rem">{_h(trial.request)}</div>
      </div>
      <div class="actions">
        <span id="arena-status" class="muted"></span>
        <button type="button" id="btn-grade">Open grading</button>
        <a href="{back}"><button type="button">Exit arena</button></a>
      </div>
    </div>
    <div class="arena-split">
      <section class="arena-pane" id="pane-a">
        <div class="arena-pane-title">
          <strong>A · {_h(lane_a.model_label)}</strong>
          <span class="muted" id="status-a">{_h(lane_a.status)}</span>
        </div>
        <div class="chat-messages" id="msgs-a">{msgs_a}</div>
      </section>
      <section class="arena-pane" id="pane-b">
        <div class="arena-pane-title">
          <strong>B · {_h(lane_b.model_label)}</strong>
          <span class="muted" id="status-b">{_h(lane_b.status)}</span>
        </div>
        <div class="chat-messages" id="msgs-b">{msgs_b}</div>
      </section>
    </div>
    <div class="grade-panel{grade_open}" id="grade-panel">
      <h3 style="margin-top:0">Grading mode</h3>
      <p class="muted">Score both lanes, pick a winner, and save. Writes to
        <code>data/arena/comparisons.jsonl</code> for later agent tuning — not into the coding workspace.</p>
      {grade_saved}
      <div class="winner-row">
        <span class="muted">Winner:</span>
        <label><input type="radio" name="winner" value="a"/> A · {_h(lane_a.model_label)}</label>
        <label><input type="radio" name="winner" value="b"/> B · {_h(lane_b.model_label)}</label>
        <label><input type="radio" name="winner" value="tie" checked/> Tie</label>
        <label><input type="radio" name="winner" value="neither"/> Neither</label>
      </div>
      <div class="grade-grid">
        <div>
          <strong>Lane A scores</strong>
          {_score_sliders("sa")}
          <label>Notes A<textarea id="notes-a" rows="3" placeholder="What worked or failed…"></textarea></label>
        </div>
        <div>
          <strong>Lane B scores</strong>
          {_score_sliders("sb")}
          <label>Notes B<textarea id="notes-b" rows="3" placeholder="What worked or failed…"></textarea></label>
        </div>
      </div>
      <label class="muted" style="display:block;margin-top:0.75rem">Training note
        <textarea id="training-note" rows="2"
          placeholder="What should future agent config learn from this comparison?"></textarea>
      </label>
      <div class="actions" style="margin-top:0.85rem">
        <button class="primary" type="button" id="btn-save-grade">Save grades</button>
        <span id="grade-status" class="muted"></span>
      </div>
    </div>
  </div>
  <script>
  const TRIAL_ID = {json.dumps(trial_id)};
  const JOB_A = {json.dumps(lane_a.job_id)};
  const JOB_B = {json.dumps(lane_b.job_id)};

  function renderMessages(events) {{
    if (!events || !events.length) {{
      return "<div class='message system'>No messages yet.</div>";
    }}
    return events.map(ev => {{
      const p = ev.payload || {{}};
      const role = p.role || 'assistant';
      const content = (p.content || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return `<div class="message ${{role}}"><div>${{content}}</div></div>`;
    }}).join('');
  }}

  async function refreshLane(lane, threadId, statusEl, msgsEl) {{
    const res = await fetch('/api/chats/messages?thread_id=' + encodeURIComponent(threadId));
    if (!res.ok) return;
    const events = await res.json();
    msgsEl.innerHTML = renderMessages(events);
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }}

  async function pollJob(jobId, statusEl) {{
    if (!jobId) return 'completed';
    const res = await fetch('/api/jobs/' + encodeURIComponent(jobId));
    const job = await res.json();
    statusEl.textContent = job.progress || job.status;
    return job.status;
  }}

  async function pollArena() {{
    const status = document.getElementById('arena-status');
    const gradeBtn = document.getElementById('btn-grade');
    for (;;) {{
      const [sa, sb] = await Promise.all([
        pollJob(JOB_A, document.getElementById('status-a')),
        pollJob(JOB_B, document.getElementById('status-b')),
      ]);
      await Promise.all([
        refreshLane('a', {json.dumps(lane_a.thread_id)},
          document.getElementById('status-a'), document.getElementById('msgs-a')),
        refreshLane('b', {json.dumps(lane_b.thread_id)},
          document.getElementById('status-b'), document.getElementById('msgs-b')),
      ]);
      const done = ['completed','failed','cancelled'];
      const aDone = done.includes(sa);
      const bDone = done.includes(sb);
      status.textContent = aDone && bDone
        ? 'Both lanes finished — open grading when ready'
        : 'Running both agents…';
      if (aDone && bDone) {{
        gradeBtn.classList.add('primary');
        return;
      }}
      await new Promise(r => setTimeout(r, 900));
    }}
  }}

  document.getElementById('btn-grade').addEventListener('click', () => {{
    document.getElementById('grade-panel').classList.add('open');
    document.getElementById('grade-panel').scrollIntoView({{behavior:'smooth'}});
  }});

  document.getElementById('btn-save-grade').addEventListener('click', async () => {{
    const winner = (document.querySelector('input[name=winner]:checked') || {{}}).value || 'tie';
    const dims = ['helpfulness','correctness','research_quality','concision'];
    const scores_a = {{}}, scores_b = {{}};
    dims.forEach(d => {{
      scores_a[d] = Number(document.getElementById('sa-' + d).value);
      scores_b[d] = Number(document.getElementById('sb-' + d).value);
    }});
    const statusEl = document.getElementById('grade-status');
    statusEl.textContent = 'Saving…';
    try {{
      const res = await fetch('/api/arena/' + encodeURIComponent(TRIAL_ID) + '/grade', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          winner,
          scores_a,
          scores_b,
          notes_a: document.getElementById('notes-a').value,
          notes_b: document.getElementById('notes-b').value,
          training_note: document.getElementById('training-note').value,
        }})
      }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.status);
      statusEl.textContent = 'Saved · winner ' + winner;
    }} catch (e) {{
      statusEl.textContent = 'Error: ' + e;
    }}
  }});

  pollArena();
  </script>
"""
    return _shell("Arena · Analyst Ledger", body, active="chats")


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
    # Deep research / explicit manual capture may leave the research allowlist
    allow_any = bool(
        data.get("allow_any")
        or data.get("deep_research")
        or data.get("manual")
    )
    parsed = parse_url(url, allow_any=allow_any)
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


def _api_automations_action(
    ledger: Ledger, action: str, data: dict, jobs: Any = None
) -> dict:
    from . import rituals as rituals_mod
    from .runners import resolve_runner
    from .workflow_engine import WorkflowEngine

    if action == "mine":
        days = int(data.get("days") or 21)
        min_sessions = int(data.get("min_sessions") or 3)
        candidates = rituals_mod.mine_rituals(
            ledger=ledger, days=days, min_sessions=min_sessions
        )
        return {"status": "ok", "count": len(candidates), "candidates": candidates}

    if action == "create":
        if jobs is None:
            return rituals_mod.create_automations_with_claude(ledger=ledger)

        def create(job):
            job.update("Reviewing recent redacted sessions with Claude")
            result = rituals_mod.create_automations_with_claude(ledger=ledger)
            job.update(f"Created {result['count']} draft automation(s)")
            return result

        return jobs.start("automation:create", "automation_create", create).public()

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
            model=data.get("model") if "model" in data else None,
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
        if not stub:
            from .models import normalize_agent_model
            from .rituals import load_spec

            try:
                spec = load_spec(ritual_id)
            except RuntimeError as exc:
                return {"status": "needs_config", "message": str(exc)}
            if not normalize_agent_model(spec.get("model")):
                return {
                    "status": "needs_config",
                    "message": (
                        "Choose Claude or Qwen3 8B in Edit automation, "
                        "then Save, before the first run."
                    ),
                }
            if jobs is None:
                return WorkflowEngine(ledger).run(
                    ritual_id, request=str(data.get("request") or ""), stub=False
                )

            def run(job):
                return WorkflowEngine(ledger).run(
                    ritual_id,
                    request=str(data.get("request") or ""),
                    stub=False,
                    job=job,
                )

            return jobs.start(
                f"workflow:{ritual_id}", "workflow_run", run
            ).public()
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


def _api_chat_message(ledger: Ledger, jobs: Any, data: dict) -> dict:
    from .messenger_bridge import FRIEND_THREAD_ID, send_friend_message
    from .rituals import _validate_ritual_id, list_automations
    from .router import execute_routed_run, route_message, router_enabled
    from .workflow_engine import MasterCoordinator, WorkflowEngine

    thread_id = str(data.get("thread_id") or "")
    content = str(data.get("content") or "").strip()
    if not content:
        raise ValueError("message content is required")
    if thread_id == FRIEND_THREAD_ID:
        sent = send_friend_message(content)
        # Fly/Friend chat can reference an approved workflow: @workflow <ritual_id>
        match = re.search(
            r"(?<!\w)@workflow\s+([a-zA-Z0-9][a-zA-Z0-9_-]{0,120})\b",
            content,
            flags=re.I,
        )
        if not match:
            return sent
        ritual_id = _validate_ritual_id(match.group(1))
        approved = {
            a["ritual_id"]
            for a in list_automations(ledger)
            if a.get("approved") and a.get("enabled", True)
        }
        if ritual_id not in approved:
            sent["workflow"] = {
                "status": "blocked",
                "ritual_id": ritual_id,
                "error": (
                    f"Workflow '{ritual_id}' is not approved/enabled. "
                    "Approve it in Automations first."
                ),
            }
            return sent
        job = jobs.start(
            f"workflow:{ritual_id}",
            "workflow_chat",
            lambda job: WorkflowEngine(ledger).run(
                ritual_id,
                request=content,
                stub=False,
                job=job,
            ),
        )
        public = job.public()
        public["ritual_id"] = ritual_id
        sent["workflow"] = public
        return sent
    session = ledger.get_session(thread_id)
    if not session or session.surface != "chat":
        raise RuntimeError(f"Chat thread '{thread_id}' not found.")
    ledger.append_chat_message(thread_id, role="user", content=content)
    if router_enabled():
        # File finder first: its gate (location verb + file noun) is stricter
        # than ritual routing, so it only wins clearly file-flavored asks.
        try:
            from .file_search import execute_file_search, match_file_request
            from .paths import file_search_roots

            fquery = match_file_request(content)
            roots = file_search_roots()
        except Exception:  # noqa: BLE001 — file finder must never break chat
            fquery, roots = None, []
        if fquery is not None and roots:
            fs_stub = bool(data.get("stub", False))
            fq = fquery
            return jobs.start(
                "file_search",
                "file_search",
                lambda job: execute_file_search(ledger, thread_id, fq, stub=fs_stub),
            ).public()
        decision = None
        try:
            restrict = None
            if session.desk_tag != "chat:master":
                restrict = str(session.desk_tag or "").removeprefix("chat:")
            decision = route_message(content, restrict_to=restrict)
        except Exception:  # noqa: BLE001 — router must never break chat
            decision = None
        if decision is not None and decision.matched:
            routed = decision
            stub = bool(data.get("stub", False))
            return jobs.start(
                f"workflow:{routed.ritual_id}",
                "workflow_run",
                lambda job: execute_routed_run(ledger, thread_id, routed, stub=stub),
            ).public()
    if session.desk_tag == "chat:master":
        return jobs.start(
            "chat:master",
            "master_chat",
            lambda job: MasterCoordinator(ledger).run(content, job=job),
        ).public()
    ritual_id = str(session.desk_tag or "").removeprefix("chat:")
    return jobs.start(
        f"workflow:{ritual_id}",
        "workflow_chat",
        lambda job: WorkflowEngine(ledger).run(
            ritual_id, request=content, stub=False, job=job
        ),
    ).public()


def _api_arena_start(ledger: Ledger, jobs: Any, data: dict) -> dict:
    """Start two simultaneous isolated workflow runs for side-by-side grading."""
    from .arena import attach_jobs, create_trial, sync_lane_from_job
    from .orchestration import ClaudeGateway
    from .rituals import _validate_ritual_id
    from .workflow_engine import WorkflowEngine

    ritual_id = _validate_ritual_id(str(data.get("ritual_id") or ""))
    request = str(data.get("request") or "").strip()
    model_a = str(data.get("model_a") or "")
    model_b = str(data.get("model_b") or "")
    source_thread_id = (data.get("source_thread_id") or None) or None
    stub = bool(data.get("stub", False))

    trial = create_trial(
        ledger,
        ritual_id=ritual_id,
        request=request,
        model_a=model_a,
        model_b=model_b,
        source_thread_id=str(source_thread_id) if source_thread_id else None,
    )

    def make_runner(lane_key: str):
        lane = trial.lanes[lane_key]

        def run(job):
            sync_lane_from_job(trial, lane_key, job_status="running")
            try:
                # Fresh gateway per lane so model overrides do not race.
                engine = WorkflowEngine(
                    ledger, ClaudeGateway(ledger, model=lane.model)
                )
                result = engine.run(
                    ritual_id,
                    request=request,
                    stub=stub,
                    job=job,
                    model_override=lane.model,
                    thread_id=lane.thread_id,
                    handoff=False,
                )
                sync_lane_from_job(
                    trial, lane_key, job_status="completed", result=result
                )
                return result
            except Exception as exc:  # noqa: BLE001
                sync_lane_from_job(
                    trial, lane_key, job_status="failed", error=str(exc)
                )
                raise

        return run

    job_a = jobs.start(
        f"arena:{trial.trial_id}:a", "arena_run", make_runner("a")
    )
    job_b = jobs.start(
        f"arena:{trial.trial_id}:b", "arena_run", make_runner("b")
    )
    attach_jobs(trial, job_a=job_a.job_id, job_b=job_b.job_id)
    return {
        "status": "ok",
        "trial_id": trial.trial_id,
        "job_a": job_a.job_id,
        "job_b": job_b.job_id,
        "trial": trial.public(),
    }


def _api_arena_grade(ledger: Ledger, trial_id: str, data: dict) -> dict:
    from .arena import save_grade

    return save_grade(
        ledger,
        trial_id,
        winner=str(data.get("winner") or ""),
        scores_a=data.get("scores_a") if isinstance(data.get("scores_a"), dict) else {},
        scores_b=data.get("scores_b") if isinstance(data.get("scores_b"), dict) else {},
        notes_a=str(data.get("notes_a") or ""),
        notes_b=str(data.get("notes_b") or ""),
        training_note=str(data.get("training_note") or ""),
    )


def make_app(ledger: Optional[Ledger] = None):
    from .workflow_engine import JobManager

    ledger = ledger or Ledger()
    jobs = JobManager()

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

        if path == "/chats" and method == "GET":
            qs = environ.get("QUERY_STRING") or ""
            return _html_response(start_response, _chats_page(ledger, qs))

        if path == "/chats/arena" and method == "GET":
            qs = environ.get("QUERY_STRING") or ""
            return _html_response(start_response, _arena_page(ledger, qs))

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
                result = _api_automations_action(ledger, action, data, jobs=jobs)
                return _json_response(start_response, result)
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/chats" and method == "GET":
            from .friend_qwen import qwen_thread_meta
            from .messenger_bridge import friend_thread_meta

            ledger.get_or_create_chat_thread(master=True)
            threads = [
                friend_thread_meta(),
                qwen_thread_meta(),
                *ledger.list_chat_threads(),
            ]
            return _json_response(start_response, threads)

        if path == "/api/chats/friend/qwen" and method == "GET":
            from .friend_qwen import qwen_status

            return _json_response(start_response, qwen_status())

        if path == "/api/chats/friend/qwen" and method == "POST":
            from .friend_qwen import set_qwen_in_conversation
            from .messenger_bridge import MessengerBridgeError

            try:
                data = _parse_json_body(environ)
                enabled = bool(data.get("enabled"))
                return _json_response(
                    start_response, set_qwen_in_conversation(enabled)
                )
            except MessengerBridgeError as exc:
                return _json_response(
                    start_response,
                    {"error": str(exc)},
                    status=f"{exc.status} Error",
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/chats/friend/qwen/tick" and method == "POST":
            from .friend_qwen import tick_qwen
            from .messenger_bridge import MessengerBridgeError

            try:
                return _json_response(start_response, tick_qwen())
            except MessengerBridgeError as exc:
                return _json_response(
                    start_response,
                    {"error": str(exc), "replied": False},
                    status=f"{exc.status} Error",
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response,
                    {"error": str(exc), "replied": False},
                    status="400 Bad Request",
                )

        if path == "/api/chats/friend/clear" and method == "POST":
            from .messenger_bridge import MessengerBridgeError, clear_friend_messages

            try:
                return _json_response(start_response, clear_friend_messages())
            except MessengerBridgeError as exc:
                return _json_response(
                    start_response,
                    {"error": str(exc)},
                    status=f"{exc.status} Error",
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/chats/messages" and method == "GET":
            from .messenger_bridge import (
                FRIEND_THREAD_ID,
                MessengerBridgeError,
                list_friend_messages,
            )

            qs = parse_qs(environ.get("QUERY_STRING") or "")
            thread_id = str((qs.get("thread_id") or [""])[0])
            try:
                if thread_id == FRIEND_THREAD_ID:
                    return _json_response(start_response, list_friend_messages())
                return _json_response(
                    start_response, ledger.list_chat_messages(thread_id)
                )
            except MessengerBridgeError as exc:
                return _json_response(
                    start_response,
                    {"error": str(exc)},
                    status=f"{exc.status} Error",
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/chats/message" and method == "POST":
            try:
                data = _parse_json_body(environ)
                return _json_response(
                    start_response, _api_chat_message(ledger, jobs, data)
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/arena/start" and method == "POST":
            try:
                data = _parse_json_body(environ)
                return _json_response(
                    start_response, _api_arena_start(ledger, jobs, data)
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        arena_get = re.match(r"^/api/arena/(arena_[a-zA-Z0-9]+)$", path or "")
        if arena_get and method == "GET":
            try:
                from .arena import load_trial

                trial = load_trial(arena_get.group(1))
                # Refresh lane status from live jobs when available.
                for key, lane in trial.lanes.items():
                    if not lane.job_id:
                        continue
                    job = jobs.get(lane.job_id)
                    if not job:
                        continue
                    from .arena import sync_lane_from_job

                    sync_lane_from_job(
                        trial,
                        key,
                        job_status=job.status,
                        result=job.result if isinstance(job.result, dict) else None,
                        error=job.error,
                    )
                trial = load_trial(arena_get.group(1))
                return _json_response(start_response, trial.public())
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        arena_grade = re.match(
            r"^/api/arena/(arena_[a-zA-Z0-9]+)/grade$", path or ""
        )
        if arena_grade and method == "POST":
            try:
                data = _parse_json_body(environ)
                return _json_response(
                    start_response,
                    _api_arena_grade(ledger, arena_grade.group(1), data),
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        if path == "/api/chats/run" and method == "POST":
            try:
                data = _parse_json_body(environ)
                data["stub"] = False
                return _json_response(
                    start_response,
                    _api_automations_action(ledger, "run", data, jobs=jobs),
                )
            except Exception as exc:  # noqa: BLE001
                return _json_response(
                    start_response, {"error": str(exc)}, status="400 Bad Request"
                )

        job_match = re.match(r"^/api/jobs/(job_[a-zA-Z0-9]+)(/cancel)?$", path or "")
        if job_match:
            job = jobs.get(job_match.group(1))
            if not job:
                return _json_response(
                    start_response, {"error": "job not found"}, status="404 Not Found"
                )
            if method == "GET" and not job_match.group(2):
                return _json_response(start_response, job.public())
            if method == "POST" and job_match.group(2):
                return _json_response(start_response, jobs.cancel(job.job_id).public())

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


_QWEN_POLLER_STARTED = False


def _start_qwen_poller(interval: float = 3.0) -> None:
    """Background thread that ticks Qwen across all rooms.

    This makes Qwen answer in any room without the Friend tab being open. It is a
    no-op unless the messenger is configured and Qwen has been enabled.
    """
    global _QWEN_POLLER_STARTED
    if _QWEN_POLLER_STARTED:
        return
    if os.environ.get("ANALYST_QWEN_POLLER", "1").strip() in {"0", "false", "no"}:
        return
    _QWEN_POLLER_STARTED = True

    debug = os.environ.get("ANALYST_QWEN_POLLER_DEBUG", "").strip() not in {
        "",
        "0",
        "false",
        "no",
    }

    def _loop() -> None:
        import time as _time
        import traceback as _tb

        from .friend_qwen import load_state, reset_inflight_research, tick_qwen
        from .messenger_bridge import messenger_configured

        try:
            reset_inflight_research()
        except Exception:  # noqa: BLE001
            pass
        if debug:
            print("qwen-poller: loop started", flush=True)
        beats = 0
        while True:
            try:
                if messenger_configured() and load_state().get("enabled"):
                    res = tick_qwen()
                    if debug and res.get("replied"):
                        print(
                            f"qwen-poller: replied in {res.get('room_id')}",
                            flush=True,
                        )
            except Exception:  # noqa: BLE001
                if debug:
                    print("qwen-poller: error\n" + _tb.format_exc(), flush=True)
            beats += 1
            if debug and beats % 20 == 0:
                print(f"qwen-poller: alive ({beats} ticks)", flush=True)
            _time.sleep(max(1.0, interval))

    thread = threading.Thread(target=_loop, name="qwen-poller", daemon=True)
    thread.start()


def serve(host: str = "127.0.0.1", port: int = 8788) -> None:
    app = make_app()
    _start_qwen_poller()
    httpd = make_server(host, port, app)
    print(f"Analyst ledger dashboard: http://{host}:{port}/")
    print(f"Automations: http://{host}:{port}/automations")
    print("Qwen room poller: on (answers @Qwen in every room)")
    httpd.serve_forever()
