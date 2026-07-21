#!/usr/bin/env bash
# Secure path: Ollama (localhost) → auth gateway → HTTPS tunnel.
#
# Each Workflow user runs this on THEIR OWN computer, then pastes the URL +
# token into the Model tab on levin.fly.dev (or local Workflow). Do not point
# the whole Fly app at one person's machine.
#
# Tunnel backends (first available):
#   1. ngrok (if authenticated):  ngrok config add-authtoken …
#   2. cloudflared quick tunnel (no account required)
#
# Usage (from repo root):
#   ./scripts/secure_qwen_tunnel.sh
#   ./scripts/secure_qwen_tunnel.sh --set-fly   # operator-only; discouraged

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DATA_DIR="${MESSENGER_DATA_DIR:-$ROOT/messenger/data}"
SECRET_FILE="$DATA_DIR/qwen_gateway_token"
PID_DIR="$DATA_DIR/run"
GATEWAY_PORT="${QWEN_GATEWAY_PORT:-11435}"
MODEL="${ANALYST_QWEN_MODEL:-qwen3:8b}"
SET_FLY=0
TUNNEL_BACKEND="${QWEN_TUNNEL_BACKEND:-auto}"  # auto | ngrok | cloudflared

for arg in "$@"; do
  case "$arg" in
    --set-fly) SET_FLY=1 ;;
    --ngrok) TUNNEL_BACKEND=ngrok ;;
    --cloudflared) TUNNEL_BACKEND=cloudflared ;;
    -h|--help)
      sed -n '1,22p' "$0"
      exit 0
      ;;
  esac
done

mkdir -p "$DATA_DIR" "$PID_DIR"

if ! curl -sf "http://127.0.0.1:11434/api/tags" >/dev/null; then
  echo "Ollama is not reachable on 127.0.0.1:11434 — start the Ollama app first." >&2
  exit 1
fi

pick_backend() {
  if [[ "$TUNNEL_BACKEND" == "ngrok" || "$TUNNEL_BACKEND" == "cloudflared" ]]; then
    echo "$TUNNEL_BACKEND"
    return
  fi
  if command -v ngrok >/dev/null && ngrok config check >/dev/null 2>&1; then
    echo ngrok
    return
  fi
  if command -v cloudflared >/dev/null; then
    echo cloudflared
    return
  fi
  echo "No tunnel backend available." >&2
  echo "Install cloudflared (brew install cloudflared) or authenticate ngrok:" >&2
  echo "  ngrok config add-authtoken YOUR_TOKEN" >&2
  exit 1
}

BACKEND="$(pick_backend)"
echo "Tunnel backend: $BACKEND"

if [[ ! -f "$SECRET_FILE" ]]; then
  python3 - <<'PY' >"$SECRET_FILE"
import secrets
print(secrets.token_urlsafe(48))
PY
  chmod 600 "$SECRET_FILE"
  echo "Wrote new gateway token → $SECRET_FILE"
fi
TOKEN="$(tr -d '[:space:]' <"$SECRET_FILE")"
export QWEN_GATEWAY_TOKEN="$TOKEN"
export ANALYST_QWEN_API_KEY="$TOKEN"
export QWEN_GATEWAY_PORT="$GATEWAY_PORT"
export QWEN_GATEWAY_HOST=127.0.0.1
export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"

stop_pidfile() {
  local file="$1"
  if [[ -f "$file" ]]; then
    local old
    old="$(cat "$file" || true)"
    if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
      kill "$old" 2>/dev/null || true
      sleep 0.4
    fi
    rm -f "$file"
  fi
}

stop_pidfile "$PID_DIR/qwen_gateway.pid"
stop_pidfile "$PID_DIR/ngrok.pid"
stop_pidfile "$PID_DIR/cloudflared.pid"

echo "Starting auth gateway on 127.0.0.1:$GATEWAY_PORT …"
nohup .venv/bin/python -m messenger.qwen_gateway >"$PID_DIR/qwen_gateway.log" 2>&1 &
echo $! >"$PID_DIR/qwen_gateway.pid"
disown $! 2>/dev/null || true
sleep 0.5
if ! curl -sf "http://127.0.0.1:$GATEWAY_PORT/healthz" >/dev/null; then
  echo "Gateway failed to start — see $PID_DIR/qwen_gateway.log" >&2
  exit 1
fi
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$GATEWAY_PORT/v1/models" || true)"
if [[ "$code" != "401" ]]; then
  echo "Expected 401 without token, got $code" >&2
  exit 1
fi

PUBLIC_URL=""
if [[ "$BACKEND" == "ngrok" ]]; then
  echo "Starting ngrok tunnel (HTTPS) → gateway …"
  nohup ngrok http "127.0.0.1:$GATEWAY_PORT" --log=stdout --log-format=logfmt \
    >"$PID_DIR/tunnel.log" 2>&1 &
  echo $! >"$PID_DIR/ngrok.pid"
  disown $! 2>/dev/null || true
  for _ in $(seq 1 40); do
    PUBLIC_URL="$(curl -sf http://127.0.0.1:4040/api/tunnels 2>/dev/null \
      | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)
  for t in d.get("tunnels") or []:
    u=t.get("public_url") or ""
    if u.startswith("https://"):
      print(u); break
except Exception:
  pass' || true)"
    [[ -n "$PUBLIC_URL" ]] && break
    sleep 0.4
  done
else
  echo "Starting cloudflared quick tunnel (HTTPS) → gateway …"
  nohup cloudflared tunnel --url "http://127.0.0.1:$GATEWAY_PORT" \
    --no-autoupdate \
    >"$PID_DIR/tunnel.log" 2>&1 &
  echo $! >"$PID_DIR/cloudflared.pid"
  disown $! 2>/dev/null || true
  for _ in $(seq 1 60); do
    PUBLIC_URL="$(python3 - <<'PY' "$PID_DIR/tunnel.log"
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
print(m[-1] if m else "")
PY
)"
    [[ -n "$PUBLIC_URL" ]] && break
    sleep 0.5
  done
fi

if [[ -z "$PUBLIC_URL" ]]; then
  echo "Could not read public tunnel URL — see $PID_DIR/tunnel.log" >&2
  tail -40 "$PID_DIR/tunnel.log" >&2 || true
  exit 1
fi

# Wait until the public URL actually answers (DNS/propagation lag is common).
echo "Waiting for public tunnel to become reachable …"
ready=0
for _ in $(seq 1 60); do
  if curl -sf --max-time 5 "$PUBLIC_URL/healthz" | grep -q qwen-gateway; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "Public URL never became healthy: $PUBLIC_URL" >&2
  tail -40 "$PID_DIR/tunnel.log" >&2 || true
  exit 1
fi

BASE_URL="${PUBLIC_URL%/}/v1"
echo
echo "Gateway + tunnel up."
echo "  Backend:     $BACKEND"
echo "  Public base: $BASE_URL"
echo "  Token file:  $SECRET_FILE"
echo
echo "Keep these processes running (Mac awake)."
echo "  gateway: $(cat "$PID_DIR/qwen_gateway.pid")"
if [[ -f "$PID_DIR/ngrok.pid" ]]; then echo "  ngrok:   $(cat "$PID_DIR/ngrok.pid")"; fi
if [[ -f "$PID_DIR/cloudflared.pid" ]]; then echo "  cloudflared: $(cat "$PID_DIR/cloudflared.pid")"; fi
echo

# Write env snapshot for tests (gitignored under messenger/data/)
cat >"$PID_DIR/qwen_tunnel.env" <<EOF
ANALYST_QWEN_BASE_URL=$BASE_URL
ANALYST_QWEN_MODEL=$MODEL
ANALYST_QWEN_API_KEY=$TOKEN
EOF
chmod 600 "$PID_DIR/qwen_tunnel.env"

if [[ "$SET_FLY" -eq 1 ]]; then
  echo "WARNING: --set-fly makes EVERY account on levin use YOUR computer." >&2
  echo "Prefer the Model tab so each user connects their own tunnel." >&2
  fly secrets set -a levin \
    "ANALYST_QWEN_BASE_URL=$BASE_URL" \
    "ANALYST_QWEN_MODEL=$MODEL" \
    "ANALYST_QWEN_API_KEY=$TOKEN"
  echo "Done. Fly machines will restart with the new secrets."
else
  echo "Paste into Workflow → Model tab (your account only):"
  echo "  Base URL: $BASE_URL"
  echo "  API key:  (contents of $SECRET_FILE)"
  echo "  Model:    $MODEL"
fi
