# Chat automation: the three-layer chat brain

_Added 2026-07-19. Plain-English guide to the deterministic chat layer built on
top of the Chats UI. For overall project status see [PROJECT_STATUS.md](PROJECT_STATUS.md)._

## The idea

When you type a message in the dashboard chat, it no longer goes straight to a
model. It passes through three layers, cheapest first:

```
your message
   │
   ├─ 1. FILE FINDER      "where are the quarterly reports for TSLA?"
   │      (strictest gate: needs a location verb AND a file noun)
   │
   ├─ 2. RITUAL ROUTER    "any TSLA filings?"
   │      (term-frequency match against your APPROVED automations)
   │
   └─ 3. MODEL            everything else (Claude or local Qwen)
```

Layers 1–2 are pure code: instant, free, reproducible, no API key, and every
reply explains its own reasoning ("Routed deterministically -> sec_filings_check
(score 7.0; ticker TSLA in watchlist (+3); …)"). Only genuinely novel messages
cost a model call. Kill-switch for both: `ANALYST_CHAT_ROUTER=off`.

## Layer 1 — File finder (`file_search.py`)

Searches folders you explicitly configure — never the whole drive:

```powershell
$env:ANALYST_FILE_SEARCH_ROOTS = "C:\Users\you\Documents\Research;D:\Reports"
```

(`;`-separated on Windows, `:` on macOS/Linux. Unset = feature off.)

- Ranks files by name/path terms, tickers, and periods (q2 / 2026 / quarterly).
- Replies show paths **relative to the root** — absolute paths (your username,
  folder layout) never enter chat threads, because models read threads later.
- Add "summarize" to the ask and the top md/txt/docx match is summarized by the
  **local Qwen model only** (file content never goes to Claude); if Ollama is
  not running you get a polite note instead of an error.
- Workflows can use the same search via the governed `find_files` action.

## Layer 2 — Ritual router (`router.py`)

Scores your message against every **approved + enabled** automation: watchlist
tickers (+3), company aliases like "tesla"→TSLA (+3), intent (+2), automation
name terms (+2), runner keywords (+2), plus a mismatch penalty (−2). It fires
only when the score clears 5.0 AND leads the runner-up by 2.0 — ties fall
through to the model rather than guessing. Matched runs execute the runner in a
background job and post the result into the thread with full provenance. Asking
about a ticker not on the watchlist ("any AMD filings?") overrides the
watchlist for that run.

## Layer 3 → back to Layer 2 — Chat mining (`chat_mining.py`)

The loop-closer. Every model reply in chat is a logged **automation gap** (the
deterministic layers missed it). The reviewer (`analyst review` or the
dashboard's **Claude review** page) now clusters your asks:

- 3+ similar asks over 2+ days with at least one gap → a **draft automation**
  appears on the review page tagged `[chat]`, e.g. "asked 3x over 3 day(s) in
  chat (3 handled by the model); e.g. \"any new AMD sec filings today?\"".
- One click approves it; from then on the router answers that ask for free.
- Repeated asks already covered by an approved automation are suppressed (no
  duplicates); Friend-room messages count toward repetition when the messenger
  is configured, and are skipped gracefully when it isn't.
- Drafts are never auto-approved and never overwrite human-edited specs.

So the system literally converts "things you keep asking the model" into
"things code does for free" — with you as the approval gate.

## Verifying it works

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q   # full suite (126 tests)
```

Live: `analyst dashboard`, approve a `sec_filings_check` spec, then in the
master chat try "any TSLA filings?" (router), "where are the quarterly reports
for TSLA?" (file finder, roots required), and after a few model-handled asks
run a review and look for `[chat]` proposals on the /review page.
