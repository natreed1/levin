---
name: ledger-review
description: Review the analyst ledger's recent sessions, critique mined automation candidates using real run outcomes, propose new draft automation specs (never auto-approved), and write a dated review memo for the human to act on.
---

# Ledger Review — the "review agent"

You are reviewing a research analyst's local workflow ledger to find automation
opportunities. You **propose**; the human **approves**. Read `CLAUDE.md` at the
repo root first and obey its hard rules (no `restricted` content, no approving,
egress ceiling `internal`).

## Step 1 — Gather (read-only)

Run these and read the output (Windows: prefix with `.venv\Scripts\`):

```
analyst summary
analyst session list --limit 40
analyst rituals list
analyst events --type ritual_run --limit 50
analyst events --type feedback,tag --limit 100
```

For the 10–15 most recent non-ritual sessions, pull their events
(`analyst events --session-id <sid> --limit 200`) and note: sites/symbols
visited, sections read, notes written, outcome tags, time of day.

## Step 2 — Analyze (beyond the heuristic miner)

The built-in miner only clusters by weekday/hour + website. Look for what it
cannot see:

- Repeated *intent* across different surfaces (e.g. same ticker researched via
  Yahoo one day, SEC filings the next).
- Manual steps that always follow one another (a candidate multi-step workflow).
- Sessions tagged `followup` that never got a follow-up session.
- Time sinks: many repeat visits to the same pages with no note produced.

## Step 3 — Judge the existing automations

For each candidate/spec: is it running (`ritual_run` events)? Erroring? Are its
output sessions tagged `reject`? Recommend one of: keep, fix (say what), retire.

## Step 4 — Propose (drafts only)

For each new opportunity, write a spec to `data/rituals/specs/<ritual_id>.json`
using the JSON shape documented in `CLAUDE.md`, with `"approved": false` and
`"proposed_by": "claude_review"`. Use only runners that exist in
`runners.RUNNERS`. Do not overwrite an existing spec the human has approved.

## Step 5 — Report

Write `data/reviews/<YYYY-MM-DD>_review.md` (create the folder if needed) with:

1. **What you looked at** — sessions/date range, one line.
2. **Existing automations** — keep / fix / retire, with the evidence.
3. **New proposals** — for each: what pattern you saw, the proposed spec file,
   what the human should do (`analyst rituals approve <id>`, then build/integrate).
4. **Open questions** — anything ambiguous the human should clarify.

Quote only redacted, non-sensitive snippets in the memo. End by telling the
user the memo path and the one-line approval commands.
