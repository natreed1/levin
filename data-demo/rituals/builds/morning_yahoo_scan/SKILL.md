---
name: analyst-ritual-morning_yahoo_scan
description: Run the Analyst Ledger ritual `morning_yahoo_scan` via local CLI. Never exfiltrate restricted data.
---

# Analyst Ritual: morning_yahoo_scan

## Purpose

Execute the approved research ritual **morning_yahoo_scan** using the local Analyst Ledger.
Prefer the deterministic local runner over inventing market data.

## Hard rules (non-negotiable)

1. **Never** include `restricted` or confidential raw note dumps in prompts, skills context, or chat.
2. Only use **allowlisted** market fields: `symbol, price, pct_change, volume, market_cap, next_earnings, headlines`.
3. Prefer calling the local CLI / tools below instead of scraping arbitrary sites.
4. Do **not** recommend trades or invent prices — use `analyst rituals run` output.
5. Max egress sensitivity is `internal` unless a human raises it explicitly.

## Inputs

- Ritual id: `morning_yahoo_scan`
- Runner: `morning_yf_scan`
- Default watchlist: NVDA, AAPL, SPY
- Schedule hint: `0 7 * * 1-5`

## Workflow steps

1. `{"fetch_quote": ["price", "pct_change", "volume", "market_cap"]}`
2. `{"fetch_calendar": ["next_earnings"]}`
3. `{"fetch_headlines": {"limit": 3, "source": "yahoo"}}`
4. `{"draft_note": "morning_scan_template"}`

## How to run (local)

```bash
# Stub (offline / CI):
analyst rituals run morning_yahoo_scan --stub --require-approved

# Live Yahoo public quotes (morning_yf_scan runner):
analyst rituals run morning_yahoo_scan --require-approved
```

Or from this build package:

```bash
./runner.sh --stub
```

## Outputs

- Ledger session + morning scan note (ritual surface)
- Artifact under `data/artifacts/<session_id>/`
- Optional Obsidian path if `--obsidian` is passed

## Integrate

See `INTEGRATE.md` in this package. Claude Skill install uses `ANALYST_CLAUDE_SKILLS_DIR`.
