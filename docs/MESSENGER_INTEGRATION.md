# Messenger integration — make the tagging show up in Tracking

_Handoff package for deploying the tagging/classifier into the hosted messenger._

## The insight
The messenger's **Tracking tab is `analyst_ledger` per user** (`tenancy.py`;
`/data/users/{uid}/ledger.sqlite3`; the Summary panel is literally
`Ledger.summary()`). So everything built in `analyst_ledger` — the `labels` axis,
the `kind` classifier, the correction loop — **surfaces in Tracking automatically**
once the deployed messenger runs this version. It's not a rebuild; it's a deploy
plus one capture hook.

## 1. Deploy the updated `analyst_ledger`
Pull the additive changes (nothing removed; existing behavior unchanged; ~200
tests green): `labels.py` (now `topic/entity/project/intent/state/`**`kind`**),
`classify.py`, `label_suggest.py`, `actionable.py`, `research_action.py`,
`messenger_sync.py`, `classify_export.py`, and the `ledger.py`/`schema.py`
additions (`Session.labels`, `label` + `label_feedback` events, `add_labels` /
`record_ask_labels` / `correct_message_kind` / `latest_kind_for` /
`confirmed_kind_examples`).

## 2. Capture People-room chat into the ledger (one line)
Today People messages only hit the `messages` table, so room chat never reaches
the ledger/Tracking — only Agent chats do. Add this to the message-post path
(`_create_message` / `post_message` in `app.py`), after the message is stored:

```python
try:
    from analyst_ledger.tenancy import user_context
    from analyst_ledger.messenger_sync import capture_room_message
    if owner_user_id:  # the room owner's user_id
        with user_context(owner_user_id) as led:
            capture_room_message(led, room_id, name, text, messenger_id=msg.get("id"))
except Exception:
    pass  # capture must never break chat
```

`capture_room_message` does everything: get-or-create the per-user
`chat:messenger:{room}` thread, append the message, classify it **deterministically**
(kind/entity/topic), tie the label to the message, and dedupe by `messenger_id`.
It's non-blocking; the Qwen refinement is the background sweep in step 3.

## 3. Background classification sweep (Qwen for the fuzzy ones)
On a light interval (or a button), run `classify.classify_pending(led, limit=…)`
per active user — or `analyst classify-pending`. Messages the rules couldn't
place get a `kind` from the user's linked model. Offline-graceful; kill-switch
`ANALYST_CLASSIFY_QWEN=off`.

## 4. Correction UI in the Tracking tab (the training loop)
Add a small "fix kind" control on messages/timeline that POSTs to a correction
endpoint calling `led.correct_message_kind(session_id, event_id, kind)`. The
local dashboard already exposes `POST /api/label/correct` as a template. A
correction supersedes the auto label, is saved as a training example, few-shots
the classifier, and exports via `analyst classify-export`.

## 5. Two fixes found while wiring up local Qwen
- **Companion Windows bug** — `companion_app.py` spawns the gateway with
  `os.environ.get("PYTHON", "python3")`; `python3` doesn't exist on Windows, so
  "Starting secure gateway" hangs forever. Fix: `os.environ.get("PYTHON") or
  sys.executable` (add `import sys`).
- **Sonnet 5** — `model_link.py` anthropic default is still
  `claude-sonnet-4-20250514`. Bump default + list to `claude-sonnet-5` (plus
  `claude-opus-4-8`, `claude-haiku-4-5-20251001`).

## Deploy checklist
- [ ] Pull the updated `analyst_ledger`
- [ ] Add the `capture_room_message` hook (step 2)
- [ ] Schedule the `classify_pending` sweep (step 3)
- [ ] Add the correction control to Tracking (step 4)
- [ ] Companion `sys.executable` + Sonnet-5 fixes (step 5)
- [ ] Verify: chat in a room → Tracking shows the messages tagged with `kind:`

**Result:** everything you and a teammate talk about lands in Tracking, classified
by kind — and your corrections train the classifier on your own taxonomy.
