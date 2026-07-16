# Analyst Ledger

Local-first workflow capture for hedge-fund research sessions.  
**The local store is the system of record.** Claude (API / Bedrock) is only called on explicit, redacted synthesis jobs.

## Capture timeline

| Phase | What happens |
|-------|----------------|
| Idle | Nothing written |
| `analyst session start` | Session row + `session_start` event |
| Live events | Notes, Cursor hooks, inbox drops, TradingView symbol/interval/drawings |
| Artifacts | File copied under `data/artifacts/` with sha256 metadata |
| `analyst session end` | Tags (`idea` / `reject` / `followup`) + `session_end` |
| `analyst synthesize` | Redacted prompt → model (or stub); egress audit logged |
| `analyst feedback` | accept / reject / edit for later SFT/DPO |

## Quick start

```bash
cd "/Users/natreed/.cursor/projects/Finance Work Enviroment"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

export ANALYST_LEDGER_DATA="$PWD/data"

analyst session start "AM research — NVDA" --surface notes --desk-tag tech
analyst note "Checking earnings revision vs QQQ"
analyst attach ./some-chart.png   # optional
analyst tag idea
analyst synthesize --destination local_stub
analyst feedback accept --synthesis-event-id <event_id from synthesize>
analyst session end --tag followup

analyst summary
analyst dashboard   # http://127.0.0.1:8788/
```

Dry-run a redacted Claude prompt without calling the API:

```bash
analyst synthesize --dry-run
```

Real Claude (prefer commercial API + ZDR, or Bedrock):

```bash
export ANTHROPIC_API_KEY=...
pip install -e ".[anthropic]"
analyst synthesize --destination anthropic
# or
analyst synthesize --destination bedrock   # needs boto3 + AWS creds
```

## Cursor hooks

Hooks are wired in [`.cursor/hooks.json`](.cursor/hooks.json). They are **opt-in**:

```bash
export ANALYST_CURSOR_HOOK=1
# optional: auto-open a cursor session on stop if none active
export ANALYST_CURSOR_HOOK_AUTO_SESSION=1
```

## Analyst inbox

Drop research exports into `~/AnalystInbox` (or `$ANALYST_INBOX`):

```bash
analyst watch-inbox --once
# or keep running:
analyst watch-inbox
```

## Obsidian / Apple Notes / Google Docs

```bash
# Obsidian — only notes with `ledger: true` or #ledger
export ANALYST_OBSIDIAN_VAULT="$HOME/path/to/vault"
analyst sync obsidian --once

# Apple Notes — folder "AnalystLedger" in Notes.app
analyst sync apple-notes --once

# Google Docs — drop .md/.txt/.docx exports into ~/AnalystGDocs
analyst sync gdocs --once

analyst sync all   # one-shot all configured sources
```

Details: [docs/external-notes-sync.md](docs/external-notes-sync.md)

## TradingView extension

1. Start the dashboard: `analyst dashboard`
2. Chrome → Extensions → Load unpacked → `extensions/tradingview-capture`
3. Open TradingView; use the popup to **Snapshot chart** or **Log note**

Captures symbol, interval, drawing *counts*, and notes — **not** mouse trajectories.

## Yahoo Finance Chrome extension

Chrome will not one-click install a private local extension (Web Store only). Easiest path:

```bash
analyst install-extension
# or double-click: Install Yahoo Extension.command
# or dashboard → Turn on tracking → Install Yahoo extension…
```

That copies the extension to **`~/AnalystLedger/Yahoo Capture Extension`**, highlights it in Finder, and opens Chrome’s extensions page. Then: **Developer mode** → **Load unpacked** → select that folder (once).

Keep `analyst dashboard` running, open a Yahoo quote, click the extension → **Capture**.

Or without the extension:

```bash
analyst ingest-browser "https://finance.yahoo.com/quote/TSM" --auto-session
```

## Rituals: mine → suggest → approve → build → integrate → run

Dashboard UI (recommended):

```bash
analyst dashboard   # http://127.0.0.1:8788/automations
```

From **Automations**: mine candidates → open a ritual → **Suggest** → **Approve** → **Build** → **Integrate** (Claude Skill or Local) → **Run (stub)**.

CLI equivalent:

```bash
# After several morning sessions with Yahoo URLs + notes:
analyst rituals mine --days 21 --min-sessions 3
analyst rituals list
analyst rituals suggest morning_yahoo_scan          # writes data/rituals/specs/
# With ANTHROPIC_API_KEY set, suggest defaults to anthropic; else local_stub
analyst rituals approve morning_yahoo_scan

# Build Claude Skill + local runner package:
analyst rituals build morning_yahoo_scan
# → data/rituals/builds/morning_yahoo_scan/{SKILL.md,workflow.json,runner.sh,INTEGRATE.md,…}

# Integrate:
export ANALYST_CLAUDE_SKILLS_DIR="$HOME/.claude/skills"   # optional
analyst rituals integrate morning_yahoo_scan --target claude-skill
analyst rituals integrate morning_yahoo_scan --target local

# Run the Yahoo morning agent (stub or live quotes):
analyst rituals run morning_yf_scan --stub --symbols NVDA,AAPL,SPY
analyst rituals run morning_yahoo_scan --require-approved --stub
# or: data/rituals/builds/morning_yahoo_scan/runner.sh --stub

# Optional Obsidian write-back:
analyst rituals run morning_yf_scan --stub \
  --obsidian "$HOME/Obsidian/Vault/Routines/Morning Scan.md"
```

Build packages never include restricted/confidential raw notes — only allowlisted fields and redacted sample context. See each package’s `INTEGRATE.md`.

See [docs/openclaw-cron-morning-yf.md](docs/openclaw-cron-morning-yf.md) for scheduling.

## Sensitivity

| Label | Egress |
|-------|--------|
| `public` | Allowed |
| `internal` | Default max for synthesis |
| `confidential` | Local only unless you raise `--max-sensitivity` |
| `restricted` | **Never** leaves the machine |

## SFT / process rewards (P4)

```bash
analyst sft-export
# writes data/sft/context_memo_pairs.jsonl
# and data/sft/dpo_reject_stubs.jsonl
```

Pairs are `(session context → memo)` from **accept/edit** feedback. Reward family is `analyst_process`, not market PnL.

## Layout

```
src/analyst_ledger/   # ledger, CLI, rituals, morning_yf, obsidian/apple/gdocs sync
.cursor/hooks/
extensions/tradingview-capture/
extensions/browser-capture/
templates/morning_yf_scan.json
docs/external-notes-sync.md
docs/openclaw-cron-morning-yf.md
data/                 # JSONL + SQLite (gitignored)
tests/
```

## Compliance notes

- Not legal advice; firm policy wins.
- Do not point this at OMS/Bloomberg scrapes or MNPI rooms without approval.
- Keep FileVault on; treat `data/` as confidential.
- Consumer Claude.ai is the wrong path for fund work — use API ZDR or cloud-hosted Claude under the firm DPA.
