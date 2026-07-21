# Unified Workflow Messenger

Cloud-hosted workflow system: **People chats**, **Agentic chats**, **Automations**,
and **Tracking** behind individual accounts. Per-user research ledgers live under
`/data/users/{user_id}/`. Automations run in-cloud via `CloudScheduler`.

## Local run

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r messenger/requirements.txt
pip install -e .

export MESSENGER_INVITE_TOKEN='pick-a-long-secret'
export MESSENGER_SESSION_SECRET='another-long-secret'
export MESSENGER_SCHEDULER=0   # optional: disable background scheduler in dev
export PYTHONPATH="$PWD/src:$PWD"

python -m messenger
# → http://127.0.0.1:8790/
```

Create an account in the UI (Create account), or hit the API:

```bash
curl -c cookies.txt -X POST http://127.0.0.1:8790/api/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"password1","display_name":"You"}'
```

Guest room join (invite link) still works for People chats without a full account.

## Email verification + password reset

Signup sends a verification email; login is blocked until verified.
Forgot password sends a one-hour reset link.

Configure outbound mail (Fly secrets recommended):

```bash
# Resend (preferred)
fly secrets set -a levin \
  MESSENGER_RESEND_API_KEY='re_...' \
  MESSENGER_EMAIL_FROM='Workflow <onboarding@yourdomain.com>' \
  MESSENGER_PUBLIC_BASE_URL='https://levin.fly.dev'

# Or SMTP
# MESSENGER_SMTP_HOST / PORT / USER / PASSWORD / TLS
```

Without a mail backend, local emails are logged to the server console and
signup/forgot responses include `dev_verify_url` / `dev_reset_url` for testing.
On Fly, account creation fails closed until Resend or SMTP is configured; the
server never exposes verification or reset links in production responses.

## Tabs

| Tab | What it is |
|-----|------------|
| **Chats → People** | Person-to-person rooms (WebSocket). Owner-scoped; share invite links. `@Qwen` / `@workflow` handled in-process. |
| **Chats → Agents** | Per-user workflow threads in that user's ledger (Master + automation threads). |
| **Automations** | Mine / approve / run ritual specs for the signed-in user (cloud scheduler fires approved+enabled specs). |
| **Tracking** | Start/end capture sessions, notes, timeline of sessions/events. |

Keyboard on Chats: `j` / `k` moves between People and Agent threads.

## Deploy on Fly.io

Requires the [Fly CLI](https://fly.io/docs/hands-on/install-flyctl/). **Deploy from the repository root** so `src/analyst_ledger` is in the build context:

```bash
# From repo root
fly volumes create messenger_data --region sjc --size 3 -a levin

fly secrets set -a levin \
  MESSENGER_INVITE_TOKEN='your-long-invite-token' \
  MESSENGER_SESSION_SECRET="$(openssl rand -hex 32)"

fly deploy --config messenger/fly.toml --dockerfile Dockerfile
```

Optional Postgres for shared `users` / `rooms` tables at scale (ledgers stay on the volume):

```bash
fly postgres create --name levin-db
fly postgres attach levin-db -a levin
fly secrets set -a levin MESSENGER_DATABASE_URL="$DATABASE_URL"
```

(`MESSENGER_DATABASE_URL` is reserved for a future Postgres adapter; SQLite on the volume is the working store today.)

Live (non-stub) scheduled automation runs:

```bash
fly secrets set -a levin MESSENGER_SCHEDULER_LIVE=1
```

## Model tab (per-user providers)

Live specialists use **the room owner's** linked provider — not a shared Fly secret.

In **Model**, each account can connect:
- **Claude (Anthropic)** — paste `sk-ant-…`
- **GPT (OpenAI)** — paste `sk-…`
- **OpenRouter** — one key, many models
- **Local Ollama (tunnel)** — `./scripts/secure_qwen_tunnel.sh`, then paste HTTPS `/v1` URL + token
- **Custom OpenAI-compatible** — Groq, Together, vLLM, etc.

```bash
# Local Ollama path only:
./scripts/secure_qwen_tunnel.sh
# Then: Workflow → Model → provider "Local Ollama" → Save & test
```

Avoid `./scripts/secure_qwen_tunnel.sh --set-fly` — that would make the whole
app depend on one operator machine.

## Local companion (Phase 6 safeguard)

Each account can register a local companion that holds private ledger data:

```bash
curl -b cookies.txt -X POST http://127.0.0.1:8790/api/companion/link \
  -H 'content-type: application/json' \
  -d '{"base_url":"http://127.0.0.1:8791","token":"..."}'
curl -b cookies.txt http://127.0.0.1:8790/api/companion/status
```

Until a companion is reachable, the cloud volume under `/data/users/{id}/` is the system of record.

## Env vars

| Variable | Purpose |
|----------|---------|
| `MESSENGER_INVITE_TOKEN` | Server invite (bots / legacy guest join / admin room list) |
| `MESSENGER_SESSION_SECRET` | Cookie signing key |
| `MESSENGER_DATA_DIR` | Shared DB + companions state (default `messenger/data`) |
| `MESSENGER_USERS_DIR` | Per-user ledger roots (default `$MESSENGER_DATA_DIR/users`) |
| `MESSENGER_DB_PATH` | Override shared SQLite path |
| `MESSENGER_SCHEDULER` | `0` to disable cloud scheduler |
| `MESSENGER_SCHEDULER_LIVE` | `1` for non-stub scheduled runs |
| `MESSENGER_SCHEDULER_INTERVAL` | Seconds between scheduler ticks (default 30) |
| `MESSENGER_COOKIE_SECURE` | Force Secure cookies |
| `MESSENGER_RESEND_API_KEY` | Resend API key for account email |
| `MESSENGER_EMAIL_FROM` | Verified sender used for account email |
| `MESSENGER_PUBLIC_BASE_URL` | Public origin used in verification/reset links |
| `MESSENGER_SMTP_HOST` | SMTP host when Resend is not used |
| `MESSENGER_SMTP_PORT` | SMTP port (default 587) |
| `MESSENGER_SMTP_USER` / `MESSENGER_SMTP_PASSWORD` | Optional SMTP credentials |
| `MESSENGER_AUTO_VERIFY` | Emergency bypass only; keep `0` in production |
| `ANALYST_QWEN_BASE_URL` | OpenAI-compatible base (local Ollama or tunnel `/v1`) |
| `ANALYST_QWEN_MODEL` | Model id (default `qwen3:8b`) |
| `ANALYST_QWEN_API_KEY` | Bearer token (required for the secure gateway) |
| `PORT` / `MESSENGER_PORT` | Listen port (8790 local, 8080 Fly) |

## API surface (selected)

- `POST /api/auth/signup|login|logout`, `GET /api/auth/me`
- People: `POST /api/rooms`, `GET /api/rooms/mine`, `POST /api/rooms/select`, `POST /api/join`, `WS /ws`
- Agents: `GET /api/agent-chats`, `POST /api/agent-chats/message`, `GET /api/agent-chats/jobs/{id}`
- Automations: `GET /api/automations`, `POST /api/automations/{mine,approve,run,…}`
- Tracking: `GET /api/tracking/summary|sessions|events`, `POST /api/tracking/session/{start,note,end}`
- Companion: `POST|DELETE /api/companion/link`, `GET /api/companion/status`
- Review: `GET /api/review`, `POST /api/review/run` (Claude/stub + chat-mining drafts)
