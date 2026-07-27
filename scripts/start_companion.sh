#!/usr/bin/env bash
# Durable Local Companion starter (survives terminal/agent shell exit).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export MESSENGER_DATA_DIR="${MESSENGER_DATA_DIR:-$ROOT/messenger/data}"
# Ensure Homebrew binaries (cloudflared) are visible even under stripped PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"
LOG="${COMPANION_LOG:-/tmp/companion-qa.log}"
if curl -sf "http://127.0.0.1:${COMPANION_PORT:-8791}/healthz" >/dev/null 2>&1; then
  echo "Companion already healthy on :${COMPANION_PORT:-8791}"
  curl -sS "http://127.0.0.1:${COMPANION_PORT:-8791}/healthz"
  echo
  exit 0
fi
# shellcheck disable=SC2086
nohup "$ROOT/.venv/bin/python" -u -m messenger.companion_app >>"$LOG" 2>&1 &
echo "started pid=$! log=$LOG"
sleep 1
curl -sS "http://127.0.0.1:${COMPANION_PORT:-8791}/healthz"
echo
