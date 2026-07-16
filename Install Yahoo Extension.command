#!/bin/bash
# Double-click this file in Finder to stage the Yahoo extension and open install UI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
export ANALYST_LEDGER_DATA="${ANALYST_LEDGER_DATA:-$ROOT/data}"
echo "Staging Yahoo Capture extension…"
analyst install-extension
echo ""
echo "Next in Chrome:"
echo "  1. Developer mode ON"
echo "  2. Load unpacked"
echo "  3. Pick the highlighted folder (Yahoo Capture Extension)"
echo ""
read -r -p "Press Enter to close…"
