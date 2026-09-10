#!/usr/bin/env bash
# Creates a named Cloudflare Tunnel on your own domain and installs both
# services so the bridge survives reboots.
#
#   bash setup-tunnel.sh mcp.yourdomain.com
#
# Requires a domain whose nameservers are on Cloudflare (free plan is fine).
# For a throwaway URL with no domain, use quick-tunnel.sh instead.
set -euo pipefail

HOSTNAME_ARG="${1:-}"
TUNNEL_NAME="${2:-mcp-bridge}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.mcp-bridge/bridge.env"

if [[ -z "$HOSTNAME_ARG" ]]; then
  echo "usage: bash setup-tunnel.sh <hostname> [tunnel-name]" >&2
  echo "   eg: bash setup-tunnel.sh mcp.example.com" >&2
  exit 1
fi

if [[ ! -f "$HOME/.cloudflared/cert.pem" ]]; then
  echo "cloudflared is not authenticated yet. Running login (opens a browser)..."
  cloudflared tunnel login
fi

if ! cloudflared tunnel list --output json | grep -q "\"name\":\"$TUNNEL_NAME\""; then
  echo "Creating tunnel '$TUNNEL_NAME'..."
  cloudflared tunnel create "$TUNNEL_NAME"
else
  echo "Tunnel '$TUNNEL_NAME' already exists, reusing it."
fi

echo "Pointing $HOSTNAME_ARG at the tunnel..."
cloudflared tunnel route dns --overwrite-dns "$TUNNEL_NAME" "$HOSTNAME_ARG"

echo "Updating $ENV_FILE with the public URL..."
if grep -q '^MCP_BRIDGE_PUBLIC_URL=' "$ENV_FILE"; then
  sed -i "s|^MCP_BRIDGE_PUBLIC_URL=.*|MCP_BRIDGE_PUBLIC_URL=https://$HOSTNAME_ARG|" "$ENV_FILE"
else
  echo "MCP_BRIDGE_PUBLIC_URL=https://$HOSTNAME_ARG" >> "$ENV_FILE"
fi

echo "Installing systemd units..."
sed -e "s|MCP_USER|$USER|g" -e "s|MCP_HOME|$HOME|g" "$DIR/systemd/mcp-bridge.service" \
  | sudo tee /etc/systemd/system/mcp-bridge.service >/dev/null
sed -e "s|MCP_TUNNEL_NAME|$TUNNEL_NAME|" -e "s|MCP_USER|$USER|g" \
    -e "s|/usr/local/bin/cloudflared|$(command -v cloudflared)|" "$DIR/systemd/mcp-bridge-tunnel.service" \
  | sudo tee /etc/systemd/system/mcp-bridge-tunnel.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-bridge.service mcp-bridge-tunnel.service

sleep 4
echo
echo "--- local health ---"
curl -fsS http://127.0.0.1:8901/healthz && echo || echo "bridge not responding locally"
echo "--- public health (may take ~30s for DNS) ---"
curl -fsS "https://$HOSTNAME_ARG/healthz" && echo || echo "not reachable yet; retry shortly"

cat <<EOF

Done. In Claude: Settings -> Connectors -> Add custom connector

    https://$HOSTNAME_ARG/mcp

You will be sent to a consent page. Enter the passphrase from:
    grep PASSPHRASE $ENV_FILE

Logs:    journalctl -u mcp-bridge -u mcp-bridge-tunnel -f
Audit:   tail -f ~/.mcp-bridge/audit.log
EOF
