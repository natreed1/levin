#!/usr/bin/env bash
# Auto-generated launcher for ritual: morning_yahoo_scan
set -euo pipefail
RITUAL_ID="morning_yahoo_scan"
EXTRA=("$@")
if [[ ${#EXTRA[@]} -eq 0 ]]; then
  EXTRA=(--require-approved)
fi
exec analyst rituals run "$RITUAL_ID" "${EXTRA[@]}"
