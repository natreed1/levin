#!/usr/bin/env bash
# Local environment launcher for morning_yahoo_scan
set -euo pipefail
cd "$(dirname "$0")"
exec ./runner.sh "$@"
