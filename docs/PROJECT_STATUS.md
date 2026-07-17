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

## Setup on a new machine

```powershell
git clone <this repo>
cd levin
python -m venv .venv
.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -q        # should report 38 passed
.venv\Scripts\analyst.exe dashboard   # UI at http://127.0.0.1:8788/
```

Each machine has its own private ledger — `data/` is gitignored on purpose, so
you start empty. To capture browsing, install the Chrome extension:
`analyst install-extension`, then Chrome → Developer mode → Load unpacked.

## What works today

- **Capture**: CLI notes/sessions, Yahoo Finance + TradingView Chrome
  extensions, inbox folder watcher, Obsidian / Google Docs / Apple Notes
  (macOS-only) sync, Cursor editor hooks (opt-in).
- **Automation pipeline**: mine candidates from your sessions → suggest a spec
  → human approves → build a package (Claude Skill + launchers) → run.
- **Runners** (the "action agents"): `morning_yf_scan` (Yahoo quotes),
  `sec_filings_check` (official SEC EDGAR — verified live), `note_digest`
  (weekly roll-up of your notes), `generic_watchlist_scan`.
- **Scheduling**: `analyst rituals integrate <id> --target windows-task`
  registers a Windows Task Scheduler job (cron/OpenClaw on Mac — see
  `docs/openclaw-cron-morning-yf.md`).
- **Claude review agent**: with Claude Code, run the `ledger-review` skill —
  it critiques your automations from real run outcomes and proposes new draft
  specs. Drafts are never auto-approved; a human approves in the dashboard.
- **Dashboard**: timeline, session detail, automations (with last-run status),
  tracking toggle — all local at `127.0.0.1:8788`.

## Recent changes (2026-07-16)

- Security: dashboard no longer sends wide-open CORS headers (websites could
  previously read the ledger through your browser).
- Windows support throughout: PowerShell launchers (`runner.ps1`), Task
  Scheduler integration, README/hooks fixes. Launchers pin the Python path and
  ledger folder at build time because scheduled tasks inherit no environment.
- Runner registry replaced the hardcoded yahoo-only dispatch; added the SEC and
  note-digest runners. `--require-approved` with a missing spec now refuses
  instead of silently passing.
- Added `CLAUDE.md` (rules + schema for AI assistants) and the
  `.claude/skills/ledger-review` review-agent skill.
- Test suite: 38 passing.

## Known limitations

- Yahoo's unofficial quote API often returns HTTP 401 now; the runner degrades
  gracefully (errors logged per symbol). SEC EDGAR is the reliable live source.
- Apple Notes sync requires macOS.
- Redaction is regex-based (emails, account numbers, SSNs) — treat it as a
  seatbelt, not a guarantee; keep truly sensitive notes labeled
  `confidential`/`restricted` so they never egress.

## Next steps (roadmap)

1. **Capture real sessions** for a few mornings, then mine → approve →
   schedule (this proves the loop on real data).
2. Run the `ledger-review` skill weekly; approve/reject its proposals.
3. Add runners for whatever the review agent finds (earnings calendar, etc.).
4. Deferred by choice: a local open-source model tier for summarizing
  confidential notes before anything reaches a cloud model.
