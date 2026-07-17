"""CLI: session lifecycle, notes, synthesize, dashboard, inbox, sft export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .ledger import Ledger
from .schema import Sensitivity, Surface


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_session_start(args: argparse.Namespace) -> int:
    ledger = Ledger()
    session = ledger.start_session(
        title=args.title,
        surface=args.surface,
        sensitivity=args.sensitivity,
        desk_tag=args.desk_tag,
    )
    _print_json(session.to_dict())
    return 0


def cmd_session_end(args: argparse.Namespace) -> int:
    ledger = Ledger()
    tags = args.tag or []
    session = ledger.end_session(session_id=args.session_id, tags=tags)
    _print_json(session.to_dict())
    return 0


def cmd_session_status(args: argparse.Namespace) -> int:
    ledger = Ledger()
    sid = args.session_id or ledger.get_active_session_id()
    if not sid:
        print("No active session.", file=sys.stderr)
        return 1
    session = ledger.get_session(sid)
    if not session:
        print(f"Session '{sid}' not found.", file=sys.stderr)
        return 1
    _print_json(session.to_dict())
    return 0


def cmd_session_list(args: argparse.Namespace) -> int:
    ledger = Ledger()
    _print_json(ledger.list_sessions(limit=args.limit))
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    ledger = Ledger()
    event = ledger.add_note(
        text=args.text,
        session_id=args.session_id,
        sensitivity=args.sensitivity,
        surface=args.surface,
    )
    _print_json(event.to_dict())
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    ledger = Ledger()
    event = ledger.add_tag(args.tag, session_id=args.session_id)
    _print_json(event.to_dict())
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    ledger = Ledger()
    art = ledger.attach_artifact(
        Path(args.path),
        session_id=args.session_id,
        sensitivity=args.sensitivity,
        copy_into_store=not args.no_copy,
    )
    _print_json(art.to_dict())
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    ledger = Ledger()
    types = args.type.split(",") if args.type else None
    _print_json(
        ledger.list_events(session_id=args.session_id, limit=args.limit, types=types)
    )
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    _print_json(Ledger().summary())
    return 0


def cmd_install_extension(args: argparse.Namespace) -> int:
    from .install_extension import install_yahoo_extension

    result = install_yahoo_extension(open_ui=not args.no_open)
    _print_json(result)
    print(
        "\nIn Chrome: Developer mode → Load unpacked → select the staged folder above.",
        file=sys.stderr,
    )
    return 0


def cmd_synthesize(args: argparse.Namespace) -> int:
    from .synthesize import run_synthesis

    ledger = Ledger()
    sid = args.session_id or ledger.get_active_session_id()
    if not sid:
        print("No session_id and no active session.", file=sys.stderr)
        return 1
    result = run_synthesis(
        ledger,
        session_id=sid,
        instruction=args.instruction,
        max_sensitivity=Sensitivity(args.max_sensitivity),
        dry_run=args.dry_run,
        destination=args.destination,
    )
    _print_json(result)
    return 0 if result.get("status") in {"ok", "dry_run"} else 1


def cmd_feedback(args: argparse.Namespace) -> int:
    ledger = Ledger()
    edited = None
    if args.edited_file:
        edited = Path(args.edited_file).read_text(encoding="utf-8")
    fid = ledger.add_feedback(
        label=args.label,
        session_id=args.session_id,
        synthesis_event_id=args.synthesis_event_id,
        notes=args.notes or "",
        edited_output=edited,
    )
    _print_json({"feedback_id": fid, "label": args.label})
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import serve

    serve(host=args.host, port=args.port)
    return 0


def cmd_watch_inbox(args: argparse.Namespace) -> int:
    from .inbox_watcher import watch_inbox

    watch_inbox(once=args.once, poll_seconds=args.poll)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    source = args.sync_cmd
    if source == "obsidian":
        from .obsidian_sync import watch_obsidian

        watch_obsidian(
            once=args.once,
            poll_seconds=args.poll,
            vault=Path(args.vault).expanduser() if args.vault else None,
            subdir=args.subdir,
            require_opt_in=not args.all_notes,
        )
        return 0
    if source == "apple-notes":
        from .apple_notes_sync import scan_apple_notes, watch_apple_notes

        if args.export_only:
            from .apple_notes_sync import export_apple_notes

            paths = export_apple_notes(folder=args.folder)
            print(f"exported {len(paths)} file(s)")
            return 0
        if args.once:
            n = scan_apple_notes(folder=args.folder, skip_export=args.skip_export)
            print(f"done ({n} new note(s))")
            return 0
        watch_apple_notes(once=False, poll_seconds=args.poll, folder=args.folder)
        return 0
    if source == "gdocs":
        from .gdocs_sync import watch_gdocs

        watch_gdocs(
            once=args.once,
            poll_seconds=args.poll,
            export_dir=Path(args.dir).expanduser() if args.dir else None,
        )
        return 0
    if source == "all":
        total = 0
        # Obsidian (optional)
        try:
            from .obsidian_sync import scan_obsidian

            total += scan_obsidian(
                vault=Path(args.vault).expanduser() if getattr(args, "vault", None) else None,
                require_opt_in=not getattr(args, "all_notes", False),
            )
        except RuntimeError as exc:
            print(f"obsidian: skip ({exc})")
        # Apple Notes
        try:
            from .apple_notes_sync import scan_apple_notes

            total += scan_apple_notes(folder=getattr(args, "folder", None))
        except RuntimeError as exc:
            print(f"apple_notes: skip ({exc})")
        # GDocs folder
        from .gdocs_sync import scan_gdocs

        total += scan_gdocs(
            export_dir=Path(args.dir).expanduser() if getattr(args, "dir", None) else None
        )
        print(f"sync all: {total} new item(s)")
        return 0
    print(f"error: unknown sync source {source}", file=sys.stderr)
    return 1


def cmd_sft_export(args: argparse.Namespace) -> int:
    from .sft_export import export_pairs

    path = export_pairs(out_path=Path(args.out) if args.out else None)
    print(str(path))
    return 0


def cmd_ingest_tv(args: argparse.Namespace) -> int:
    """Ingest a TradingView capture event from stdin or --json."""
    from .schema import Event

    ledger = Ledger()
    if args.json:
        data = json.loads(args.json)
    else:
        data = json.loads(sys.stdin.read())
    sid = data.get("session_id") or ledger.get_active_session_id()
    if not sid and args.auto_session:
        title = data.get("title") or f"TV {data.get('payload', {}).get('symbol', 'session')}"
        session = ledger.start_session(
            title=title,
            surface=Surface.TRADINGVIEW.value,
            sensitivity=data.get("sensitivity", Sensitivity.INTERNAL.value),
        )
        sid = session.session_id
    event = Event(
        type=data["type"],
        surface=Surface.TRADINGVIEW.value,
        session_id=sid,
        sensitivity=data.get("sensitivity", Sensitivity.INTERNAL.value),
        payload=data.get("payload") or {},
    )
    stored = ledger.append_event(event)
    _print_json(stored.to_dict())
    return 0


def cmd_ingest_browser(args: argparse.Namespace) -> int:
    from .browser import parse_url
    from .schema import Event

    ledger = Ledger()
    url = args.url
    parsed = parse_url(url)
    if args.title:
        parsed["title"] = args.title
    sid = args.session_id or ledger.get_active_session_id()
    if not sid and args.auto_session:
        label = parsed.get("symbol") or parsed.get("host") or "browser"
        session = ledger.start_session(
            title=f"Browser {label}",
            surface=Surface.BROWSER.value,
            sensitivity=args.sensitivity,
        )
        sid = session.session_id
    event = Event(
        type="url_focus",
        surface=Surface.BROWSER.value,
        session_id=sid,
        sensitivity=args.sensitivity,
        payload=parsed,
    )
    _print_json(ledger.append_event(event).to_dict())
    return 0


def cmd_rituals(args: argparse.Namespace) -> int:
    from . import rituals as rituals_mod

    action = args.rituals_cmd
    if action == "mine":
        candidates = rituals_mod.mine_rituals(
            days=args.days,
            min_sessions=args.min_sessions,
        )
        _print_json(candidates)
        return 0
    if action == "list":
        _print_json(rituals_mod.load_candidates())
        return 0
    if action == "suggest":
        dest = args.destination
        if dest is None:
            dest = rituals_mod.default_suggest_destination()
        result = rituals_mod.suggest_ritual(
            args.ritual_id,
            destination=dest,
            dry_run=args.dry_run,
        )
        _print_json(result)
        return 0 if result.get("status") in {"ok", "dry_run"} else 1
    if action == "approve":
        _print_json(rituals_mod.approve_spec(args.ritual_id))
        return 0
    if action == "show":
        _print_json(rituals_mod.load_spec(args.ritual_id))
        return 0
    if action == "build":
        result = rituals_mod.build_ritual(
            args.ritual_id,
            require_approved=args.require_approved,
        )
        _print_json(result)
        return 0 if result.get("status") == "ok" else 1
    if action == "integrate":
        result = rituals_mod.integrate_ritual(
            args.ritual_id,
            target=args.target,
        )
        _print_json(result)
        return 0 if result.get("status") in {"ok", "needs_config"} else 1
    if action == "run":
        from .runners import resolve_runner

        ritual_id = args.ritual_id
        watchlist = None
        if args.symbols:
            watchlist = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        runner_name, runner_fn = resolve_runner(ritual_id, explicit=args.runner)
        result = runner_fn(
            watchlist=watchlist,
            ritual_id=ritual_id,
            stub=args.stub,
            require_approved=args.require_approved,
            write_obsidian=Path(args.obsidian) if args.obsidian else None,
        )
        result.setdefault("runner", runner_name)
        _print_json(result)
        return 0 if result.get("status") == "ok" else 1
    print(f"error: unknown rituals action {action}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyst",
        description="Local-first analyst workflow ledger (system of record on disk).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # session
    sess = sub.add_parser("session", help="Session lifecycle")
    sess_sub = sess.add_subparsers(dest="session_cmd", required=True)

    start = sess_sub.add_parser("start", help="Start a tagged research session")
    start.add_argument("title", help="Session title, e.g. 'AM research — NVDA'")
    start.add_argument(
        "--surface",
        default=Surface.NOTES.value,
        choices=[s.value for s in Surface],
    )
    start.add_argument(
        "--sensitivity",
        default=Sensitivity.INTERNAL.value,
        choices=[s.value for s in Sensitivity],
    )
    start.add_argument("--desk-tag", default=None)
    start.set_defaults(func=cmd_session_start)

    end = sess_sub.add_parser("end", help="End the active (or given) session")
    end.add_argument("--session-id", default=None)
    end.add_argument(
        "--tag",
        action="append",
        choices=["idea", "reject", "followup", "neutral"],
        help="Outcome tag (repeatable)",
    )
    end.set_defaults(func=cmd_session_end)

    status = sess_sub.add_parser("status", help="Show active or given session")
    status.add_argument("--session-id", default=None)
    status.set_defaults(func=cmd_session_status)

    lst = sess_sub.add_parser("list", help="List recent sessions")
    lst.add_argument("--limit", type=int, default=50)
    lst.set_defaults(func=cmd_session_list)

    # note
    note = sub.add_parser("note", help="Append a note to the active session")
    note.add_argument("text")
    note.add_argument("--session-id", default=None)
    note.add_argument("--sensitivity", default=None, choices=[s.value for s in Sensitivity])
    note.add_argument("--surface", default=Surface.NOTES.value)
    note.set_defaults(func=cmd_note)

    # tag
    tag = sub.add_parser("tag", help="Tag the active session")
    tag.add_argument("tag", choices=["idea", "reject", "followup", "neutral"])
    tag.add_argument("--session-id", default=None)
    tag.set_defaults(func=cmd_tag)

    # attach
    attach = sub.add_parser("attach", help="Attach an artifact file to the session")
    attach.add_argument("path")
    attach.add_argument("--session-id", default=None)
    attach.add_argument("--sensitivity", default=None, choices=[s.value for s in Sensitivity])
    attach.add_argument("--no-copy", action="store_true", help="Reference path without copying")
    attach.set_defaults(func=cmd_attach)

    # events
    events = sub.add_parser("events", help="List events")
    events.add_argument("--session-id", default=None)
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--type", default=None, help="Comma-separated event types")
    events.set_defaults(func=cmd_events)

    summary = sub.add_parser("summary", help="Ledger summary")
    summary.set_defaults(func=cmd_summary)

    # synthesize
    syn = sub.add_parser("synthesize", help="Draft memo from session (redacted egress)")
    syn.add_argument("--session-id", default=None)
    syn.add_argument(
        "--instruction",
        default="Draft a research memo and next-checks list from this session.",
    )
    syn.add_argument(
        "--max-sensitivity",
        default=Sensitivity.INTERNAL.value,
        choices=[s.value for s in Sensitivity if s != Sensitivity.RESTRICTED],
    )
    syn.add_argument(
        "--destination",
        default="anthropic",
        choices=["anthropic", "bedrock", "local_stub"],
    )
    syn.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and audit the prompt without calling a model",
    )
    syn.set_defaults(func=cmd_synthesize)

    # feedback
    fb = sub.add_parser("feedback", help="Accept / reject / edit a synthesis draft")
    fb.add_argument("label", choices=["accept", "reject", "edit"])
    fb.add_argument("--session-id", default=None)
    fb.add_argument("--synthesis-event-id", default=None)
    fb.add_argument("--notes", default="")
    fb.add_argument("--edited-file", default=None, help="Path to edited memo (for label=edit)")
    fb.set_defaults(func=cmd_feedback)

    # dashboard
    ext = sub.add_parser(
        "install-extension",
        help="Copy Yahoo Chrome extension to ~/AnalystLedger and open install UI",
    )
    ext.add_argument(
        "--no-open",
        action="store_true",
        help="Stage files only (do not open Finder / Chrome)",
    )
    ext.set_defaults(func=cmd_install_extension)

    dash = sub.add_parser("dashboard", help="Serve local timeline dashboard")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8788)
    dash.set_defaults(func=cmd_dashboard)

    # inbox
    inbox = sub.add_parser("watch-inbox", help="Watch ~/AnalystInbox for dropped files")
    inbox.add_argument("--once", action="store_true", help="Scan once and exit")
    inbox.add_argument("--poll", type=float, default=2.0)
    inbox.set_defaults(func=cmd_watch_inbox)

    # sync external notes
    sync = sub.add_parser("sync", help="Sync Obsidian / Apple Notes / Google Docs exports")
    sync_sub = sync.add_subparsers(dest="sync_cmd", required=True)

    ob = sync_sub.add_parser("obsidian", help="Watch/ingest Obsidian vault (opt-in notes)")
    ob.add_argument("--vault", default=None, help="Vault path (or ANALYST_OBSIDIAN_VAULT)")
    ob.add_argument("--subdir", default=None, help="Only scan a subfolder, e.g. Research")
    ob.add_argument(
        "--all-notes",
        action="store_true",
        help="Ingest all markdown (default: only ledger: true or #ledger)",
    )
    ob.add_argument("--once", action="store_true")
    ob.add_argument("--poll", type=float, default=3.0)
    ob.set_defaults(func=cmd_sync)

    an = sync_sub.add_parser(
        "apple-notes",
        help='Export+ingest Notes.app folder (default folder: AnalystLedger)',
    )
    an.add_argument(
        "--folder",
        default=None,
        help="Notes folder name (default ANALYST_APPLE_NOTES_FOLDER or AnalystLedger)",
    )
    an.add_argument("--once", action="store_true")
    an.add_argument("--poll", type=float, default=30.0)
    an.add_argument("--export-only", action="store_true", help="Only export to disk")
    an.add_argument(
        "--skip-export",
        action="store_true",
        help="Ingest existing export files without calling Notes.app",
    )
    an.set_defaults(func=cmd_sync)

    gd = sync_sub.add_parser(
        "gdocs",
        help="Watch Google Docs export folder (~/AnalystGDocs or ANALYST_GDOCS_EXPORT)",
    )
    gd.add_argument("--dir", default=None, help="Export folder path")
    gd.add_argument("--once", action="store_true")
    gd.add_argument("--poll", type=float, default=5.0)
    gd.set_defaults(func=cmd_sync)

    sall = sync_sub.add_parser("all", help="Run one-shot sync for all configured sources")
    sall.add_argument("--vault", default=None)
    sall.add_argument("--folder", default=None)
    sall.add_argument("--dir", default=None)
    sall.add_argument("--all-notes", action="store_true")
    sall.set_defaults(func=cmd_sync)

    # sft
    sft = sub.add_parser("sft-export", help="Export (context→memo) pairs from feedback")
    sft.add_argument("--out", default=None)
    sft.set_defaults(func=cmd_sft_export)

    # tv ingest
    tv = sub.add_parser("ingest-tv", help="Ingest TradingView capture event (JSON)")
    tv.add_argument("--json", default=None, help="JSON string (else stdin)")
    tv.add_argument(
        "--auto-session",
        action="store_true",
        help="Start a TradingView session if none active",
    )
    tv.set_defaults(func=cmd_ingest_tv)

    # browser ingest
    br = sub.add_parser("ingest-browser", help="Ingest allowlisted browser URL focus")
    br.add_argument("url", help="Full URL (must be allowlisted host)")
    br.add_argument("--title", default=None)
    br.add_argument("--session-id", default=None)
    br.add_argument("--auto-session", action="store_true")
    br.add_argument(
        "--sensitivity",
        default=Sensitivity.INTERNAL.value,
        choices=[s.value for s in Sensitivity],
    )
    br.set_defaults(func=cmd_ingest_browser)

    # rituals
    rit = sub.add_parser("rituals", help="Mine / suggest / run recurring workflows")
    rit_sub = rit.add_subparsers(dest="rituals_cmd", required=True)

    mine = rit_sub.add_parser("mine", help="Cluster ledger sessions into candidate rituals")
    mine.add_argument("--days", type=int, default=21)
    mine.add_argument("--min-sessions", type=int, default=3)
    mine.set_defaults(func=cmd_rituals)

    rlist = rit_sub.add_parser("list", help="List mined candidates")
    rlist.set_defaults(func=cmd_rituals)

    sug = rit_sub.add_parser("suggest", help="Review a ritual and write a workflow spec")
    sug.add_argument("ritual_id")
    sug.add_argument(
        "--destination",
        default=None,
        choices=["local_stub", "anthropic", "bedrock"],
        help="Default: anthropic if ANTHROPIC_API_KEY set, else local_stub",
    )
    sug.add_argument("--dry-run", action="store_true")
    sug.set_defaults(func=cmd_rituals)

    appr = rit_sub.add_parser("approve", help="Mark a workflow spec as approved")
    appr.add_argument("ritual_id")
    appr.set_defaults(func=cmd_rituals)

    show = rit_sub.add_parser("show", help="Show a workflow spec JSON")
    show.add_argument("ritual_id")
    show.set_defaults(func=cmd_rituals)

    bld = rit_sub.add_parser(
        "build",
        help="Generate Claude Skill + runner package under data/rituals/builds/",
    )
    bld.add_argument("ritual_id")
    bld.add_argument(
        "--require-approved",
        action="store_true",
        help="Refuse to build unless spec.approved is true",
    )
    bld.set_defaults(func=cmd_rituals)

    integ = rit_sub.add_parser(
        "integrate",
        help="Install build into Claude skills dir or prepare local launcher",
    )
    integ.add_argument("ritual_id")
    integ.add_argument(
        "--target",
        default="claude-skill",
        choices=["claude-skill", "local", "windows-task"],
        help=(
            "claude-skill copies to ANALYST_CLAUDE_SKILLS_DIR; local writes launcher; "
            "windows-task registers a Windows Task Scheduler job"
        ),
    )
    integ.set_defaults(func=cmd_rituals)

    run = rit_sub.add_parser("run", help="Execute a ritual runner (e.g. morning YF scan)")
    run.add_argument("ritual_id", help="e.g. morning_yahoo_scan or morning_yf_scan")
    run.add_argument("--symbols", default=None, help="Comma-separated watchlist override")
    run.add_argument(
        "--runner",
        default=None,
        choices=["morning_yf_scan", "generic_watchlist_scan", "sec_filings_check", "note_digest"],
        help="Force a runner (default: spec's runner, else guessed from the ritual name)",
    )
    run.add_argument("--stub", action="store_true", help="Offline stub quotes")
    run.add_argument(
        "--require-approved",
        action="store_true",
        help="Refuse to run unless spec.approved is true",
    )
    run.add_argument(
        "--obsidian",
        default=None,
        help="Optional path to write the morning note into an Obsidian vault",
    )
    run.set_defaults(func=cmd_rituals)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
