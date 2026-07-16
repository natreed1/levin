# OpenClaw cron example — morning Yahoo scan

After OpenClaw is onboarded (`openclaw onboard`), add a weekday job that runs the
local ledger runner. Adjust paths and watchlist for each analyst machine.

## One-shot test

```bash
cd "/Users/natreed/.cursor/projects/Finance Work Enviroment"
source .venv/bin/activate
export ANALYST_LEDGER_DATA="$PWD/data"

# Offline:
analyst rituals run morning_yf_scan --stub --symbols NVDA,AAPL,SPY

# Live Yahoo public quote API:
analyst rituals run morning_yf_scan --symbols NVDA,AAPL,SPY

# Also write into Obsidian:
analyst rituals run morning_yf_scan --stub \
  --obsidian "$HOME/Obsidian/Vault/Routines/Morning Scan.md"
```

## OpenClaw cron (conceptual)

```bash
# Example: every weekday at 07:05 local
openclaw cron add --name morning-yf-scan --cron "5 7 * * 1-5" -- \
  /Users/natreed/.cursor/projects/Finance\ Work\ Enviroment/.venv/bin/analyst \
  rituals run morning_yf_scan --symbols NVDA,AAPL,SPY
```

Exact `openclaw cron` flags vary by version — use `openclaw cron --help`.

## Full discovery loop

```bash
# 1) Capture mornings via browser extension + notes for ~1 week
analyst dashboard   # + load extensions/browser-capture
# UI: http://127.0.0.1:8788/automations

# 2) Mine
analyst rituals mine --days 21 --min-sessions 3

# 3) Review suggestion
analyst rituals list
analyst rituals suggest morning_yahoo_scan
# read data/rituals/specs/morning_yahoo_scan_review.md

# 4) Approve → build → integrate → run
analyst rituals approve morning_yahoo_scan
analyst rituals build morning_yahoo_scan
analyst rituals integrate morning_yahoo_scan --target local
analyst rituals run morning_yahoo_scan --require-approved
```

Human still reviews the generated note; use `analyst feedback` on that session.
