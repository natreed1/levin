# Analyst Ledger — guide for Claude Code

Local-first capture of an analyst's research workflow. **The local store is the
system of record**; models are only called on explicit, redacted synthesis jobs.
Read this before touching the ledger or proposing automations.

## Hard rules (non-negotiable)

1. **Never** read, quote, or summarize events/artifacts labeled `restricted`.
2. **Never** send `confidential` or `restricted` content to any external model
   or paste it into chat. Egress ceiling is `internal` unless a human raises it.
3. Never approve automations yourself. Write specs with `"approved": false` and
   let the human approve via the dashboard or `analyst rituals approve`.
4. Append to the ledger only through the CLI / runners — never write to the
   SQLite file or the JSONL event files directly.

## Layout

- `src/analyst_ledger/` — package. Key modules:
  - `ledger.py` — sessions/events/artifacts store (JSONL canonical + SQLite index)
  - `schema.py` — event types, `Sensitivity` (public < internal < confidential < restricted)
  - `rituals.py` — mine candidates → suggest spec → approve → build → integrate
  - `runners.py` — runner registry (`RUNNERS`) + `sec_filings_check`, `note_digest`
  - `morning_yf.py` — Yahoo watchlist scan runner
  - `redact.py` — redaction + egress filtering; `synthesize.py` — model calls + audit
  - `dashboard.py` — local UI at http://127.0.0.1:8788/
- `data/` (gitignored) — `ledger.sqlite3`, `events/*.jsonl`, `artifacts/`,
  `rituals/candidates.json`, `rituals/specs/*.json`, `rituals/builds/<id>/`
- `tests/` — pytest; run with `python -m pytest tests/ -q`
  (Windows: `.venv\Scripts\python.exe -m pytest tests/ -q`)

## CLI cheat sheet

```
# macOS/Linux: source .venv/bin/activate
# Windows:     .venv\Scripts\Activate.ps1  (or call .venv\Scripts\analyst.exe)

analyst summary                      # counts + active session
analyst session list --limit 20
analyst events --session-id <sid> --limit 200   # newest-first JSON
analyst events --type note,url_focus,ritual_run
analyst rituals list                 # mined candidates
analyst rituals mine --days 21 --min-sessions 3
analyst rituals show <ritual_id>     # spec JSON
analyst rituals run <ritual_id> --stub          # offline smoke run
analyst rituals integrate <ritual_id> --target local          # macOS/Linux
analyst rituals integrate <ritual_id> --target windows-task   # Windows only
```

## Reading the ledger for review work

- Sessions have outcome tags: `idea` / `followup` / `reject` / `neutral`.
- `ritual_run` events carry `{ritual_id, runner, errors, stub}` — use them to
  judge whether an automation is actually being used and succeeding.
- `feedback` events (accept/reject/edit) grade synthesis drafts.
- Event timestamps are UTC ISO strings; JSON `payload` varies by `type`.

## Proposing a new automation (the review agent's output)

Write a draft spec to `data/rituals/specs/<ritual_id>.json`:

```json
{
  "name": "<ritual_id>",
  "version": 1,
  "approved": false,
  "runner": "morning_yf_scan | generic_watchlist_scan | sec_filings_check | note_digest",
  "schedule": "0 7 * * 1-5",
  "watchlist": ["NVDA"],
  "steps": [{"fetch_quote": ["price", "pct_change"]}, {"draft_note": "template"}],
  "outputs": {"ledger_session": true},
  "source_candidate": {"confidence": null, "evidence_count": null},
  "proposed_by": "claude_review"
}
```

`ritual_id` must match `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,120}$`. Only name runners
that exist in `runners.RUNNERS`. The human approves, builds, and schedules.

## Adding a new runner (when asked to implement one)

1. Write `run_<name>(ledger=None, watchlist=None, ritual_id=..., stub=False,
   require_approved=False, **_)` in `runners.py`, using `_spec_for` and
   `_record_run` so approval gating and ledger bookkeeping stay consistent.
2. Register it in `RUNNERS`; extend the heuristics in `resolve_runner` if the
   ritual naming implies it. Add a `--stub` path so it works offline and in tests.
3. Add a test in `tests/test_runners.py`; run the full suite before finishing.
