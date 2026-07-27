#!/usr/bin/env bash
# Install / reload macOS LaunchAgent so Local Companion survives sleep & logout-of-shell.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/scripts/com.flyleaf.companion.plist.template"
DEST="$HOME/Library/LaunchAgents/com.flyleaf.companion.plist"
LABEL="com.flyleaf.companion"
PY="$ROOT/.venv/bin/python"
DATA="${MESSENGER_DATA_DIR:-$ROOT/messenger/data}"

if [[ ! -x "$PY" ]]; then
  echo "Missing venv python at $PY" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
# Stop any ad-hoc companion first
pkill -f 'python.*-m messenger.companion_app' 2>/dev/null || true
sleep 1

sed \
  -e "s|__VENV_PYTHON__|$PY|g" \
  -e "s|__REPO_ROOT__|$ROOT|g" \
  -e "s|__MESSENGER_DATA__|$DATA|g" \
  "$TEMPLATE" >"$DEST"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$DEST"
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

sleep 2
curl -sS "http://127.0.0.1:${COMPANION_PORT:-8791}/healthz"
echo
echo "Installed LaunchAgent: $DEST (KeepAlive=true)"
