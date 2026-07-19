# Project status & onboarding

_Last updated: 2026-07-17_

New here? This page is the "where are we and how do I get running" summary.
The [README](../README.md) covers feature-by-feature usage.

## What this project is

Local-first capture of an analyst's research workflow (browsing, notes, files)
into a ledger on your own machine, which gets mined into **"rituals"** —
recurring workflows that can be approved and turned into scheduled automations
or Claude Skills. The end goal: agents review the captured data, propose
improvements, and run the repetitive parts of the workflow, with a human
approving everything. **Nothing leaves your machine except explicit, redacted
model calls, and each one is audit-logged.**

## Two machines, one repo

| | **macOS** (this stack) | **Windows** (other dev) |
|--|--|--|
| Activate venv | `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1` |
| CLI entry | `analyst` from venv | `.venv\Scripts\analyst.exe` |
| Schedule rituals | `integrate --target local` + cron/OpenClaw/`runner.sh` | `integrate --target windows-task` + `runner.ps1` |
| Apple Notes sync | yes | n/a |
| Cursor hooks | `bash .cursor/hooks/run_hook.sh …` (finds `.venv`) | same if Git Bash available; else call `.venv\Scripts\python.exe` on the hook scripts |

Each machine keeps its **own** private `data/` ledger (gitignored). Share code via git; do not sync SQLite between OS installs unless you intend to.

## Setup on a new machine

**macOS / Linux**

```bash
git clone <this repo>
cd levin
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export ANALYST_LEDGER_DATA="$PWD/data"
python -m pytest tests/ -q
analyst dashboard   # http://127.0.0.1:8788/
```

**Windows (PowerShell)**

```powershell
git clone <this repo>
cd levin
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
$env:ANALYST_LEDGER_DATA = "$PWD\data"
python -m pytest tests/ -q
.venv\Scripts\analyst.exe dashboard
```

Expect ~40 passing tests.
To capture browsing: `analyst install-extension`, then Chrome → Developer mode → Load unpacked.

## What works today

- **Capture**: CLI notes/sessions, Yahoo Finance + TradingView Chrome
  extensions (deep research / any-site toggle + denylist on macOS branch),
  inbox folder watcher, Obsidian / Google Docs / Apple Notes (macOS-only) sync,
  Cursor editor hooks (opt-in).
- **Automation pipeline**: mine candidates → suggest a spec → human approves →
  build a package (Claude Skill + `runner.sh` + `runner.ps1`) → run.
- **Runners**: `morning_yf_scan`, `sec_filings_check` (SEC EDGAR),
  `note_digest`, `generic_watchlist_scan`.
- **Scheduling**: Windows Task Scheduler **or** macOS/Linux cron/OpenClaw via
  the pinned `runner.sh` (see `docs/openclaw-cron-morning-yf.md`).
- **Claude review agent**: `.claude/skills/ledger-review` (drafts stay unapproved).
- **Dashboard**: timeline, session detail, automations (last-run status),
  tracking — local at `127.0.0.1:8788`.

## Recent changes (2026-07-17 merge)

From the Windows fork (`mattymitch499-sketch/Levin`), adapted for dual OS:

- Security: dashboard no longer sends wide-open CORS (`*`); Chrome extensions
  still work via `host_permissions`.
- Runner registry + SEC / note-digest runners; `--require-approved` refuses
  missing specs.
- Dual launchers: `runner.ps1` (Windows) and Python-pinned `runner.sh` (macOS/Linux).
- Onboarding: `docs/PROJECT_STATUS.md`, `CLAUDE.md`, ledger-review skill.
- Kept macOS deep-research capture (`allow_any` / denylist / extension toggles).

## Known limitations

- Yahoo's unofficial quote API often returns HTTP 401; runner degrades
  gracefully. SEC EDGAR is the reliable live source.
- Apple Notes sync requires macOS.
- `windows-task` integrate returns `needs_config` on non-Windows (use `local` + cron).
- Redaction is regex-based — seatbelt, not a guarantee; label sensitive notes
  `confidential`/`restricted`.

## Next steps (roadmap)

1. Capture real morning sessions → mine → approve → schedule.
2. Run the `ledger-review` skill weekly; approve/reject proposals.
3. Add runners the review agent finds (earnings calendar, etc.).
4. Deferred: local open-source model tier for confidential notes.
