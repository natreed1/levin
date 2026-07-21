# Data tagging scheme — the `labels` axis

_Built 2026-07-20. Plain-English guide. Design was signed off first; this is the
implemented version. See [CHAT_AUTOMATION.md](CHAT_AUTOMATION.md) for the chat
brain this plugs into and [PROJECT_STATUS.md](PROJECT_STATUS.md) for the whole._

## Why this exists

Two jobs, one system:

1. **Organize** what you capture (sessions, notes, chat) so you and Nat can
   slice and mine it consistently over months.
2. **Capture for training** — recognize an explicit research request
   ("look into Acme AI") and *tag* it (`intent:research` / `entity:<slug>` /
   `state:open`) so the model can be trained on this kind of ask later. The
   agent does **not** act on it yet — this is framework, not automation.

It's a NEW axis that sits **alongside** the existing ones and never overloads
them: `sensitivity` (egress), `surface` (source), outcome `tags`
(idea/followup/reject/neutral — disposition), `desk_tag` (routing).

## The five boxes

A *label* is an `axis:value` string (same convention as `desk_tag`).

| Box | Example | Fixed list? | Lives on | Says |
|---|---|---|---|---|
| **topic** | `topic:semiconductors` | ✅ controlled | session | what field/theme |
| **entity** | `entity:acme-ai` | ❌ open (slug) | session / message | the specific subject |
| **project** | `project:q2-earnings` | ❌ open (slug) | session | which effort |
| **intent** | `intent:research` | ✅ controlled | chat message | **what to do** |
| **state** | `state:open` → `state:done` | ✅ controlled | chat message | **done yet** |

**Principle:** controlled where the set is small and stable (topic / intent /
state — unknown values are rejected, exactly like `SESSION_TAGS`); open but
slug-normalized where the set is unbounded (entity, project).

To grow the shared theme vocabulary, add a slug to `TOPICS` in
`src/analyst_ledger/labels.py` — that list is the single source of truth both
partners edit and commit.

## Applying labels to a session

```python
ledger.add_labels(["topic:Semiconductors", "project:Q2 Earnings"])
# -> stored as ["project:q2-earnings", "topic:semiconductors"]
# -> also recorded as a `label` event, mirroring add_tag()
```

Unknown controlled values raise `LabelError`. Values are validated + normalized
through `analyst_ledger.labels`.

**Auto-suggest (never auto-apply):** `label_suggest.propose(text)` returns
proposed `topic:` labels (from a keyword map + a small ticker→sector map) with a
reason each. The human confirms — nothing is applied automatically.

## Chat → tagging (Layer 0.5) — framework only, no agent action

Added to the chat brain **after** the file finder and ritual router. It only
**tags**; it does not act. This is the data framework — it makes actionable asks
legible so the model can be trained on them later.

```
your message
   ├─ 1. FILE FINDER   (unchanged)
   ├─ 2. RITUAL ROUTER (unchanged)
   ├─ 2.5 ACTIONABLE TAGGER   "look into Acme AI"   ← NEW
   │        explicit research asks only → record labels, then continue
   └─ 3. MODEL   (handles the message as usual)
```

When it fires (`actionable.detect_actionable`), it records a `label` event via
`ledger.record_ask_labels(...)` carrying `intent:research` / `entity:<slug>` /
`state:open`, then falls through to normal handling. It does **not** run
research, post a draft, or send anything to a model. `state` stays `open`
because nothing acted on the ask.

**Deliberately parked (opt-in future):** `research_action.execute_research_draft`
can run a deterministic public-web pass and post an unapproved draft
(`state:done`) — this is where the agent would *act*. It is fully built and
tested but **not wired in**, so the agent stays passive until you choose to
enable it.

## Config / kill-switches

| Env var | Default | Effect |
|---|---|---|
| `ANALYST_CHAT_ACTIONABLE` | `on` | `off` disables the detector only (router untouched) |
| `ANALYST_CHAT_ROUTER` | `on` | `off` disables the whole deterministic chat layer (incl. detector) |

## What was built (files)

- `src/analyst_ledger/labels.py` — vocabulary + `normalize_label(s)`.
- `src/analyst_ledger/label_suggest.py` — deterministic topic proposer.
- `src/analyst_ledger/actionable.py` — single-shot explicit-ask detector.
- `src/analyst_ledger/research_action.py` — draft-then-approve research action.
- `schema.py` — `Session.labels`, `label` event type.
- `ledger.py` — `labels_json` column (+ additive ALTER migration), `add_labels()`,
  and `record_ask_labels()` (records an ask's labels, takes no action).
- `dashboard.py` + `messenger/routers/agent_chats.py` — tagger wired into both
  chat handlers (records labels only; the research action is left **unwired**).
- Tests: `test_labels`, `test_session_labels`, `test_label_suggest`,
  `test_actionable`, `test_research_action` (+28 tests; full suite green).

## Deferred / next (not built)

- **Turning on the agent action** — `research_action.execute_research_draft` is
  built + tested but intentionally unwired; wiring it flips the system from
  "tag only" to "agent researches + drafts." (Deliberately off for now.)
- A model-synthesized research draft (behind the egress gate) — the parked
  action uses deterministic web results only.
- `symbol:` ticker box — deferred until missed (tickers are already in payloads).
- Dashboard UI to one-click confirm proposed labels
  (the data + API are in place; the buttons are not).
- A governed `research` action in `ALLOWED_ACTIONS` so workflows can call it too.

---

## Update — stream classifier + correction loop (2026-07-21)

The tagging moved from "detect research asks" to **classify every message**, and
gained a human-correction training loop. This is the current shape.

### `kind:` — the classifier's primary axis
Every captured message is classified into one **kind** — `research` / `build` /
`observation` / `idea` / `question` — plus `entity:` and `topic:` when
detectable. Two layers (`classify.classify_message`):
- **deterministic** rules — instant, high precision, run *at capture* so sending
  never blocks;
- **local Qwen** — fills in the fuzzy ones in the background
  (`classify.classify_pending`, CLI `analyst classify-pending`). Offline-graceful;
  kill-switch `ANALYST_CLASSIFY_QWEN=off`. Confirmed corrections are injected as
  few-shot examples so it learns your taxonomy.

Both chat handlers (`dashboard` + messenger `agent_chats`) classify at capture
and tie each label to its message via `target_event_id`.

### Correct-a-tag → training loop
- `ledger.correct_message_kind(session_id, event_id, kind)` records a superseding
  `source:human` label + a `label_feedback` example. `POST /api/label/correct`
  and `analyst label-correct` expose it.
- `ledger.latest_kind_for()` resolves a message's current kind (human > auto).
- `ledger.confirmed_kind_examples()` powers both few-shot and export.
- `analyst classify-export` writes `(message → kind)` JSONL for fine-tuning later.

Flywheel: **auto-tag → you correct → corrections few-shot the classifier now and
become fine-tune data later.**

### Capture into the ledger
- `messenger_sync.sync_messenger` pulls a hosted room's history into the ledger.
- `messenger_sync.capture_room_message` is the real-time hook for the messenger's
  message-post path (one line, non-blocking, idempotent) so live room chat lands
  in each user's Tracking ledger, tagged.

### Fits the messenger
The messenger's **Tracking tab is analyst_ledger per-user**, so all of this shows
up there once the deployed messenger runs this version. See
[MESSENGER_INTEGRATION.md](MESSENGER_INTEGRATION.md) for the deploy + hook package.
