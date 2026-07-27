# Deprecated: model availability on local ledger dashboard

Do **not** merge or extend `origin/cursor/add-model-availability-display-d85c`.

That branch added an “Available Models” panel to `src/analyst_ledger/dashboard.py`
(`:8788`). Product chat UX lives on **Flyleaf** (`messenger/`, `levin.fly.dev`).

The same indicator is implemented in:

- `messenger/static/index.html` — `#model-status`
- `messenger/static/app.js` — `refreshModelStatus` / `renderModelStatus`
- `messenger/static/style.css` — `.model-status`

Copy: `{Model} is active` (green dot) in the room/agent chat stage.
