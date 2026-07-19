# Cloud Messenger

Separate invite-gated chat room for you and a friend. **Not part of the Analyst Ledger** — own process, own SQLite DB, own deploy.

## Local run

From the repo root:

```bash
python3 -m venv messenger/.venv
source messenger/.venv/bin/activate
pip install -r messenger/requirements.txt

export MESSENGER_INVITE_TOKEN='pick-a-long-secret'
export MESSENGER_SESSION_SECRET='another-long-secret'   # optional; derived from invite if unset
export PYTHONPATH="$PWD"

python -m messenger
# → http://127.0.0.1:8790/?invite=pick-a-long-secret
```

Or:

```bash
uvicorn messenger.app:app --reload --port 8790
```

## Deploy on Fly.io

Requires the [Fly CLI](https://fly.io/docs/hands-on/install-flyctl/) and a Fly account.

```bash
cd messenger

# First time: creates the app (edit app name in fly.toml if taken)
fly launch --no-deploy

# Persistent SQLite volume (pick the same region as primary_region in fly.toml)
fly volumes create messenger_data --region sjc --size 1

# Secrets (treat invite like a password)
fly secrets set \
  MESSENGER_INVITE_TOKEN='your-long-invite-token' \
  MESSENGER_SESSION_SECRET="$(openssl rand -hex 32)"

fly deploy
```

Share with your friend:

```text
https://<your-app>.fly.dev/?invite=<your-long-invite-token>
```

After they join once, the invite is dropped from the URL bar; the session cookie keeps them signed in.

Rotate a leaked invite:

```bash
fly secrets set MESSENGER_INVITE_TOKEN='new-token'
```

(Existing session cookies still work until they expire or users hit Leave.)

## Link from Analyst Ledger (Friend tab)

The ledger **Chats** page has a **People → Friend** thread that talks to this
app through a server-side bridge (same compose UI as agents). Friend messages
stay in the messenger DB — they are not written into the research ledger.
Either client can **Delete chat** (clears the whole room for everyone via
`DELETE /api/messages`).

```bash
export ANALYST_MESSENGER_URL='https://levin.fly.dev'
export ANALYST_MESSENGER_INVITE='same-as-MESSENGER_INVITE_TOKEN'
export ANALYST_MESSENGER_NAME='Nat'   # how you appear in the room
analyst dashboard
# → http://127.0.0.1:8788/chats?thread_id=friend
```

Your friend can still use the public web UI:
`https://levin.fly.dev/?invite=…`

## Env reference

| Variable | Purpose |
|----------|---------|
| `MESSENGER_INVITE_TOKEN` | Shared door key (required in production) |
| `MESSENGER_SESSION_SECRET` | Signs session cookies |
| `MESSENGER_DATA_DIR` | Directory for SQLite (default `messenger/data`, Fly: `/data`) |
| `MESSENGER_DB_PATH` | Override full DB file path |
| `MESSENGER_COOKIE_SECURE` | `1` to force Secure cookies (set on Fly) |
| `PORT` | Listen port (Fly sets this) |
| `ANALYST_MESSENGER_URL` | Cloud messenger base URL for the ledger bridge |
| `ANALYST_MESSENGER_INVITE` | Invite token the ledger uses to join |
| `ANALYST_MESSENGER_NAME` | Your display name in Friend chat (default `You`) |

## Security

- Anyone with the invite URL can join the room. Do not put research or confidential material here.
- This is a tiny private chat, not E2E encrypted messaging.
