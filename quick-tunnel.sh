#!/usr/bin/env bash
# Throwaway trycloudflare.com tunnel -- no domain or Cloudflare account needed.
# The URL changes every run, so you must re-add the connector in Claude each
# time. Good for trying this out; use setup-tunnel.sh for anything permanent.
#
#   bash quick-tunnel.sh
#
# Ctrl-C stops the tunnel and the bridge, which revokes all remote access.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.mcp-bridge/bridge.env"
LOG=$(mktemp /tmp/mcp-tunnel.XXXXXX.log)
PORT=$(grep -oP '^MCP_BRIDGE_PORT=\K.*' "$ENV_FILE" 2>/dev/null || echo 8901)

cleanup() {
  echo
  echo "Shutting down..."
  [[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  [[ -n "${BRIDGE_PID:-}" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
  echo "Access revoked."
}
trap cleanup EXIT INT TERM

echo "Starting tunnel..."
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > "$LOG" 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [[ -n "$URL" ]] && break
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "Could not obtain a tunnel URL. Tunnel log:" >&2
  cat "$LOG" >&2
  exit 1
fi

echo "Tunnel URL: $URL"
sed -i "s|^MCP_BRIDGE_PUBLIC_URL=.*|MCP_BRIDGE_PUBLIC_URL=$URL|" "$ENV_FILE"

echo "Starting bridge..."
"$DIR/.venv/bin/python" -m bridge &
BRIDGE_PID=$!
sleep 3

cat <<EOF

============================================================
  Add this as a custom connector in Claude:

      $URL/mcp

  Passphrase for the consent screen:
      $(grep -oP '^MCP_BRIDGE_PASSPHRASE=\K.*' "$ENV_FILE")

  Ctrl-C here to kill the tunnel and revoke access.
============================================================

EOF

wait $BRIDGE_PID
