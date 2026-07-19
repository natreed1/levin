"""Tests for the runner registry, new runners, and Windows scheduling helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyst_ledger.ledger import Ledger
from analyst_ledger.rituals import (
    build_ritual,
    list_automations,
    parse_cron_schedule,
    schtasks_create_args,
)
from analyst_ledger.runners import (
    RUNNERS,
    resolve_runner,
    run_note_digest,
    run_sec_filings_check,
)
from analyst_ledger.schema import Event, Sensitivity, Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    return Ledger()


def _write_spec(tmp_path, ritual_id: str, runner: str, approved: bool = True) -> Path:
    from analyst_ledger.paths import ritual_specs_dir

    spec = {
        "name": ritual_id,
        "version": 1,
        "approved": approved,
        "runner": runner,
        "schedule": "0 7 * * 1-5",
        "watchlist": ["NVDA"],
        "steps": [{"draft_note": "t"}],
    }
    path = ritual_specs_dir() / f"{ritual_id}.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_parse_cron_schedule():
    weekdays = parse_cron_schedule("0 7 * * 1-5")
    assert weekdays == {
        "time": "07:00",
        "days": ["MON", "TUE", "WED", "THU", "FRI"],
        "daily": False,
    }
    daily = parse_cron_schedule("30 9 * * *")
    assert daily["time"] == "09:30" and daily["daily"] is True
    pair = parse_cron_schedule("15 16 * * 1,3")
    assert pair["days"] == ["MON", "WED"]
    fallback = parse_cron_schedule("not a cron string")
    assert fallback["days"] == ["MON", "TUE", "WED", "THU", "FRI"]
    assert parse_cron_schedule(None)["time"] == "07:00"


def test_schtasks_create_args():
    args = schtasks_create_args("AnalystLedger_x", Path("C:/b/runner.ps1"), "0 7 * * 1-5")
    assert args[0] == "schtasks" and "/Create" in args
    assert "AnalystLedger_x" in args
    assert "WEEKLY" in args and "MON,TUE,WED,THU,FRI" in args
    tr = args[args.index("/TR") + 1]
    assert "runner.ps1" in tr and "powershell.exe" in tr
    daily = schtasks_create_args("t", Path("r.ps1"), "0 8 * * *")
    assert "DAILY" in daily and "/D" not in daily


def test_build_includes_runner_ps1(ledger: Ledger, tmp_path):
    _write_spec(tmp_path, "test_yahoo_scan", "morning_yf_scan")
    built = build_ritual("test_yahoo_scan", ledger=ledger)
    bdir = Path(built["build_dir"])
    ps1 = (bdir / "runner.ps1").read_text(encoding="utf-8")
    assert "test_yahoo_scan" in ps1
    assert "-m analyst_ledger.cli" in ps1
    sh = (bdir / "runner.sh").read_text(encoding="utf-8")
    assert "-m analyst_ledger.cli" in sh
    assert "ANALYST_LEDGER_DATA" in sh
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    assert "runner.ps1" in manifest["files"]
    assert "runner.sh" in manifest["files"]


def test_resolve_runner_explicit_and_spec(ledger: Ledger, tmp_path):
    name, fn = resolve_runner("anything", explicit="note_digest")
    assert name == "note_digest" and fn is run_note_digest

    _write_spec(tmp_path, "custom_ritual", "sec_filings_check")
    name, fn = resolve_runner("custom_ritual")
    assert name == "sec_filings_check" and fn is run_sec_filings_check

    with pytest.raises(RuntimeError):
        resolve_runner("anything", explicit="no_such_runner")


def test_resolve_runner_heuristics(ledger: Ledger):
    assert resolve_runner("morning_yahoo_scan")[0] == "morning_yf_scan"
    assert resolve_runner("weekly_sec_check")[0] == "sec_filings_check"
    assert resolve_runner("friday_note_digest")[0] == "note_digest"
    with pytest.raises(RuntimeError):
        resolve_runner("mystery_workflow")


def test_sec_filings_check_stub(ledger: Ledger):
    result = run_sec_filings_check(ledger=ledger, watchlist=["NVDA", "AAPL"], stub=True)
    assert result["status"] == "ok"
    assert "NVDA" in result["note"] and "8-K" in result["note"]
    events = ledger.list_events(session_id=result["session_id"], limit=50)
    types = {e["type"] for e in events}
    assert {"session_start", "note", "ritual_run", "session_end"} <= types


def test_note_digest_filters_sensitivity_and_ritual_notes(ledger: Ledger):
    s = ledger.start_session("research", surface="notes")
    ledger.add_note("alpha thesis on margins")
    ledger.append_event(
        Event(
            type="note",
            surface="notes",
            session_id=s.session_id,
            sensitivity=Sensitivity.RESTRICTED.value,
            payload={"text": "MNPI deal room content"},
        )
    )
    ledger.append_event(
        Event(
            type="note",
            surface=Surface.RITUAL.value,
            session_id=s.session_id,
            sensitivity=Sensitivity.INTERNAL.value,
            payload={"text": "generated morning scan body"},
        )
    )
    ledger.end_session(session_id=s.session_id, tags=["idea"])

    result = run_note_digest(ledger=ledger, ritual_id="note_digest")
    assert result["status"] == "ok"
    assert "alpha thesis" in result["note"]
    assert "MNPI" not in result["note"]
    assert "generated morning scan" not in result["note"]
    assert result["note_count"] == 1


def test_require_approved_without_spec_refuses(ledger: Ledger):
    from analyst_ledger.morning_yf import run_morning_yf_scan

    with pytest.raises(RuntimeError):
        run_morning_yf_scan(ledger=ledger, ritual_id="ghost_yahoo_scan", stub=True, require_approved=True)
    with pytest.raises(RuntimeError):
        run_sec_filings_check(ledger=ledger, ritual_id="ghost_sec", stub=True, require_approved=True)


def test_list_automations_last_run(ledger: Ledger, tmp_path):
    _write_spec(tmp_path, "test_yahoo_scan", "morning_yf_scan")
    from analyst_ledger.morning_yf import run_morning_yf_scan

    run_morning_yf_scan(ledger=ledger, ritual_id="test_yahoo_scan", stub=True)
    items = list_automations(ledger)
    row = next(a for a in items if a["ritual_id"] == "test_yahoo_scan")
    assert row["last_run"] is not None
    assert row["last_run"]["stub"] is True
    # Without a ledger the field is simply absent/None
    items_no_ledger = list_automations()
    row2 = next(a for a in items_no_ledger if a["ritual_id"] == "test_yahoo_scan")
    assert row2.get("last_run") is None


def test_registry_contents():
    assert {"morning_yf_scan", "generic_watchlist_scan", "sec_filings_check", "note_digest"} <= set(
        RUNNERS
    )
