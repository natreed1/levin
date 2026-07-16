# Integrate: morning_yahoo_scan

Build directory: `/Users/natreed/.cursor/projects/Finance Work Enviroment/data-demo/rituals/builds/morning_yahoo_scan`

## Option A — Claude Skill

1. Set a skills directory (Claude Desktop / Claude Code skills folder):

   ```bash
   export ANALYST_CLAUDE_SKILLS_DIR="$ANALYST_CLAUDE_SKILLS_DIR"
   ```

2. Install from the dashboard **Integrate → Claude Skill**, or:

   ```bash
   analyst rituals integrate morning_yahoo_scan --target claude-skill
   ```

   This copies `SKILL.md` (and supporting files) to:
   `$ANALYST_CLAUDE_SKILLS_DIR/analyst-ritual-morning_yahoo_scan/`

3. Restart / reload Claude so the skill is picked up.
4. When the skill runs, it must call **local** `analyst rituals run` — do not paste restricted notes into the chat.

## Option B — Local environment (CLI + cron / OpenClaw)

1. Ensure the package is installed and `ANALYST_LEDGER_DATA` points at your ledger data dir.
2. Approve + run:

   ```bash
   analyst rituals approve morning_yahoo_scan
   analyst rituals run morning_yahoo_scan --require-approved --stub   # smoke
   analyst rituals run morning_yahoo_scan --require-approved          # live
   ```

3. Or use the package launcher:

   ```bash
   /Users/natreed/.cursor/projects/Finance Work Enviroment/data-demo/rituals/builds/morning_yahoo_scan/runner.sh --stub
   ```

4. Schedule with system crontab or OpenClaw — see `docs/openclaw-cron-morning-yf.md`.
   Example cron (`0 7 * * 1-5`):

   ```bash
   analyst rituals run morning_yahoo_scan --require-approved
   ```

5. Optional local integrate (writes a small launcher under builds):

   ```bash
   analyst rituals integrate morning_yahoo_scan --target local
   ```

## Runner notes

- Spec runner: `morning_yf_scan`
- Yahoo-family rituals use `morning_yf_scan`.
- Set `ANTHROPIC_API_KEY` only for suggest/build narratives that call Claude; default is `local_stub`.
