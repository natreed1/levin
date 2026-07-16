"""Tests for browser ingest, ritual mining, and morning YF runner."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analyst_ledger.browser import host_allowed, parse_url
from analyst_ledger.dashboard import make_app
from analyst_ledger.ledger import Ledger
from analyst_ledger.morning_yf import render_morning_note, run_morning_yf_scan
from analyst_ledger.rituals import approve_spec, mine_rituals, suggest_ritual
from analyst_ledger.schema import Event, Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    return Ledger()


def test_parse_yahoo_url():
    p = parse_url("https://finance.yahoo.com/quote/NVDA/key-statistics")
    assert p["symbol"] == "NVDA"
    assert p["section"] == "statistics"
    assert host_allowed("finance.yahoo.com")
    with pytest.raises(ValueError):
        parse_url("https://evil.example/quote/NVDA")


def test_ingest_browser_api(ledger: Ledger):
    app = make_app(ledger)
    body = json.dumps(
        {
            "url": "https://finance.yahoo.com/quote/AAPL",
            "title": "Apple Inc. (AAPL)",
            "auto_session": True,
        }
    ).encode()

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/api/ingest-browser",
        "QUERY_STRING": "",
        "CONTENT_TYPE": "application/json",
        "wsgi.input": __import__("io").BytesIO(body),
        "CONTENT_LENGTH": str(len(body)),
    }
    status = []

    def start(s, _h):
        status.append(s)

    out = b"".join(app(environ, start))
    assert status[0].startswith("200")
    data = json.loads(out.decode())
    assert data["type"] == "url_focus"
    assert data["payload"]["symbol"] == "AAPL"


def _backdate_session(ledger: Ledger, session_id: str, hours_ago: int, morning: bool = True):
    """Rewrite session started_at + session_start event into a local morning slot."""
    local_now = datetime.now().astimezone()
    # Aim for a weekday morning ~7:15 local
    target = local_now.replace(hour=7, minute=15, second=0, microsecond=0)
    # Walk back to a weekday if needed
    while target.weekday() > 4:
        target -= timedelta(days=1)
    target = target - timedelta(days=hours_ago // 24)
    # Keep morning hour
    if morning:
        target = target.replace(hour=7, minute=15)
    iso = target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with ledger._connect() as conn:
        conn.execute(
            "UPDATE sessions SET started_at = ? WHERE session_id = ?",
            (iso, session_id),
        )
        conn.execute(
            "UPDATE events SET ts = ? WHERE session_id = ? AND type = 'session_start'",
            (iso, session_id),
        )


def test_mine_suggest_approve_run(ledger: Ledger, tmp_path):
    # Seed 3 morning Yahoo sessions
    for i, sym in enumerate(["NVDA", "AAPL", "SPY"]):
        s = ledger.start_session(f"Morning check {sym}", surface=Surface.BROWSER.value)
        ledger.append_event(
            Event(
                type="url_focus",
                surface=Surface.BROWSER.value,
                session_id=s.session_id,
                sensitivity=Sensitivity.INTERNAL.value,
                payload=parse_url(f"https://finance.yahoo.com/quote/{sym}"),
            )
        )
        ledger.add_note(
            f"{sym} pct change and news check",
            session_id=s.session_id,
            surface=Surface.BROWSER.value,
        )
        ledger.end_session(session_id=s.session_id, tags=["neutral"])
        _backdate_session(ledger, s.session_id, hours_ago=24 * (i + 1))

    candidates = mine_rituals(ledger, days=30, min_sessions=3)
    assert candidates
    yahoo = next(c for c in candidates if c["host_family"] == "yahoo")
    assert yahoo["evidence_count"] >= 3
    assert set(yahoo["watchlist"]) >= {"NVDA", "AAPL", "SPY"}
    assert yahoo["confidence"] >= 0.5
    assert yahoo.get("confidence_reasons")
    assert "≥3 morning sessions" in " ".join(yahoo["confidence_reasons"])

    suggested = suggest_ritual(yahoo["ritual_id"], ledger=ledger, destination="local_stub")
    assert suggested["status"] == "ok"
    assert Path(suggested["spec_path"]).exists()

    approve_spec(yahoo["ritual_id"])
    result = run_morning_yf_scan(
        ledger=ledger,
        ritual_id=yahoo["ritual_id"],
        stub=True,
        require_approved=True,
        write_obsidian=tmp_path / "Morning.md",
    )
    assert result["status"] == "ok"
    assert (tmp_path / "Morning.md").exists()
    assert "Morning scan" in result["note"]


def test_render_morning_note():
    md = render_morning_note(
        [
            {
                "symbol": "NVDA",
                "name": "NVIDIA",
                "price": 100.0,
                "pct_change": 1.5,
                "volume": 1,
                "next_earnings": "2026-08-01",
                "headlines": ["Chip demand"],
            }
        ]
    )
    assert "NVDA" in md
    assert "+1.50%" in md


def test_build_artifact_and_integrate(ledger: Ledger, tmp_path, monkeypatch):
    from analyst_ledger.rituals import (
        approve_spec,
        build_ritual,
        integrate_ritual,
        suggest_ritual,
    )
    from analyst_ledger.browser import parse_url

    for i, sym in enumerate(["NVDA", "AAPL", "SPY"]):
        s = ledger.start_session(f"Morning check {sym}", surface=Surface.BROWSER.value)
        ledger.append_event(
            Event(
                type="url_focus",
                surface=Surface.BROWSER.value,
                session_id=s.session_id,
                sensitivity=Sensitivity.INTERNAL.value,
                payload=parse_url(f"https://finance.yahoo.com/quote/{sym}"),
            )
        )
        ledger.add_note(
            f"{sym} pct change and news check",
            session_id=s.session_id,
            surface=Surface.BROWSER.value,
        )
        ledger.end_session(session_id=s.session_id, tags=["neutral"])
        _backdate_session(ledger, s.session_id, hours_ago=24 * (i + 1))

    candidates = mine_rituals(ledger, days=30, min_sessions=3)
    yahoo = next(c for c in candidates if c["host_family"] == "yahoo")
    rid = yahoo["ritual_id"]
    suggest_ritual(rid, ledger=ledger, destination="local_stub")
    approve_spec(rid)

    built = build_ritual(rid, ledger=ledger)
    assert built["status"] == "ok"
    bdir = Path(built["build_dir"])
    for name in (
        "SKILL.md",
        "workflow.json",
        "runner.sh",
        "INTEGRATE.md",
        "sample_context.json",
        "manifest.json",
    ):
        assert (bdir / name).exists(), name

    skill = (bdir / "SKILL.md").read_text(encoding="utf-8")
    assert "Hard rules" in skill or "hard rules" in skill.lower()
    assert "restricted" in skill.lower()
    assert rid in skill

    sample = json.loads((bdir / "sample_context.json").read_text(encoding="utf-8"))
    assert "allowlisted_fields" in sample
    assert "restricted" in json.dumps(sample).lower()
    # No raw note dumps from sessions
    assert "pct change and news check" not in json.dumps(sample)

    workflow = json.loads((bdir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow["ritual_id"] == rid
    assert workflow.get("steps")

    # Local integrate
    local = integrate_ritual(rid, target="local", ledger=ledger)
    assert local["status"] == "ok"
    assert Path(local["launcher"]).exists()

    # Claude skill without dir configured
    needs = integrate_ritual(rid, target="claude-skill", ledger=ledger)
    assert needs["status"] == "needs_config"

    # Claude skill with dir
    skills = tmp_path / "claude_skills"
    monkeypatch.setenv("ANALYST_CLAUDE_SKILLS_DIR", str(skills))
    # Re-import path helper via fresh call (env is read at call time)
    ok = integrate_ritual(rid, target="claude-skill", ledger=ledger)
    assert ok["status"] == "ok"
    skill_text = Path(ok["skill_path"]).read_text(encoding="utf-8")
    assert "Hard rules" in skill_text or "hard rules" in skill_text.lower()
    assert rid in skill_text


def test_automations_api_happy_path(ledger: Ledger):
    from io import BytesIO
    from typing import Any, Dict, Optional, Tuple

    from analyst_ledger.browser import parse_url
    from analyst_ledger.rituals import suggest_ritual

    for i, sym in enumerate(["NVDA", "AAPL", "MSFT"]):
        s = ledger.start_session(f"AM {sym}", surface=Surface.BROWSER.value)
        ledger.append_event(
            Event(
                type="url_focus",
                surface=Surface.BROWSER.value,
                session_id=s.session_id,
                sensitivity=Sensitivity.INTERNAL.value,
                payload=parse_url(f"https://finance.yahoo.com/quote/{sym}"),
            )
        )
        ledger.add_note(f"{sym} check", session_id=s.session_id)
        ledger.end_session(session_id=s.session_id, tags=["neutral"])
        _backdate_session(ledger, s.session_id, hours_ago=24 * (i + 1))

    app = make_app(ledger)

    def call(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[str, Any]:
        raw = b""
        if body is not None:
            raw = json.dumps(body).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_TYPE": "application/json",
            "wsgi.input": BytesIO(raw),
            "CONTENT_LENGTH": str(len(raw)),
        }
        status = []

        def start(s, _h):
            status.append(s)

        out = b"".join(app(environ, start))
        return status[0], json.loads(out.decode()) if out else {}

    st, mined = call("POST", "/api/automations/mine", {"days": 30, "min_sessions": 3})
    assert st.startswith("200")
    assert mined["count"] >= 1
    rid = mined["candidates"][0]["ritual_id"]

    st, listed = call("GET", "/api/automations", None)
    assert st.startswith("200")
    assert any(a["ritual_id"] == rid for a in listed)

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/automations",
        "QUERY_STRING": "",
        "wsgi.input": BytesIO(b""),
        "CONTENT_LENGTH": "0",
    }
    status = []

    def start(s, _h):
        status.append(s)

    html = b"".join(app(environ, start)).decode()
    assert status[0].startswith("200")
    assert "Automations" in html

    suggest_ritual(rid, ledger=ledger, destination="local_stub")
    st, sug = call(
        "POST", "/api/automations/suggest", {"ritual_id": rid, "destination": "local_stub"}
    )
    assert st.startswith("200")
    assert sug.get("status") == "ok"

    st, appr = call("POST", "/api/automations/approve", {"ritual_id": rid})
    assert st.startswith("200")
    assert appr["spec"]["approved"] is True

    st, built = call("POST", "/api/automations/build", {"ritual_id": rid})
    assert st.startswith("200")
    assert built["status"] == "ok"
    assert Path(built["build_dir"]).exists()

    st, ran = call(
        "POST",
        "/api/automations/run",
        {"ritual_id": rid, "stub": True, "require_approved": True},
    )
    assert st.startswith("200")
    assert ran.get("status") == "ok"

    st, integ = call(
        "POST",
        "/api/automations/integrate",
        {"ritual_id": rid, "target": "local"},
    )
    assert st.startswith("200")
    assert integ.get("status") == "ok"

    # Detail HTML should include evidence events section
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": f"/automations/{rid}",
        "QUERY_STRING": "",
        "wsgi.input": BytesIO(b""),
        "CONTENT_LENGTH": "0",
    }
    status = []

    def start(s, _h):
        status.append(s)

    html = b"".join(app(environ, start)).decode()
    assert status[0].startswith("200")
    assert "Evidence events" in html
    assert "Edit automation" in html

    # Exclude first evidence session via update API
    from analyst_ledger.rituals import get_automation_detail, get_candidate

    detail = get_automation_detail(rid, ledger=ledger)
    assert detail.get("evidence")
    sid0 = detail["evidence"][0]["session_id"]
    st, upd = call(
        "POST",
        "/api/automations/update",
        {
            "ritual_id": rid,
            "watchlist": ["NVDA", "META"],
            "excluded_sessions": [sid0],
            "excluded_event_ids": [],
            "enabled": True,
            "approved": True,
        },
    )
    assert st.startswith("200")
    assert upd["status"] == "ok"
    assert "META" in upd["candidate"]["watchlist"]
    assert sid0 in (upd["candidate"].get("excluded_sessions") or [])
    assert sid0 not in upd["active_evidence_sessions"]

    st, err = call("POST", "/api/automations/build", {"ritual_id": "../evil"})
    assert st.startswith("400")
    assert "error" in err

