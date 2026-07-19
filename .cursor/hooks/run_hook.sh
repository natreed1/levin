#!/usr/bin/env bash
# Cross-platform Cursor hook launcher (macOS/Linux + Git Bash on Windows).
# Finds the project venv Python first so hooks work without a pre-activated shell.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" "$@"
elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  exec "$ROOT/.venv/Scripts/python.exe" "$@"
elif command -v python3 >/dev/null 2>&1; then
  exec python3 "$@"
else
  exec python "$@"
fi
