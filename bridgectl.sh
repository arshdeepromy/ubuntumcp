#!/usr/bin/env bash
# Terminal control, for when you'd rather not open the GUI.
#
#   bash bridgectl.sh status      what's running, where, recent activity
#   bash bridgectl.sh start|stop|restart
#   bash bridgectl.sh gui         open the control panel
#   bash bridgectl.sh endpoint    print the MCP URL
#   bash bridgectl.sh token       print the bearer token
#   bash bridgectl.sh passphrase  print the panel/consent passphrase
#   bash bridgectl.sh rotate      new bearer token
#   bash bridgectl.sh revoke      drop all OAuth sessions
#   bash bridgectl.sh ps          connections + in-flight commands
#   bash bridgectl.sh abort [id]  kill a wedged command (all, if no id)
#   bash bridgectl.sh reset       kill commands and drop all MCP sessions
#   bash bridgectl.sh audit       follow the audit log
#   bash bridgectl.sh logs        follow the bridge log
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HOME/.mcp-bridge"
ENV_FILE="$STATE/bridge.env"
PY="$DIR/.venv/bin/python"
get() { grep -oP "^$1=\K.*" "$ENV_FILE" 2>/dev/null || true; }

endpoint() {
  local mode port host
  mode=$(get MCP_BRIDGE_MODE); port=$(get MCP_BRIDGE_PORT)
  if [[ "$mode" == "tunnel" ]]; then
    echo "$(get MCP_BRIDGE_PUBLIC_URL)/mcp"
  else
    host=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo 127.0.0.1)
    echo "http://$host:${port:-8901}/mcp"
  fi
}

case "${1:-status}" in
  status)
    echo "=== bridge ==="
    "$PY" -m panel.supervisor status
    echo
    echo "=== configuration ==="
    printf '  mode       %s\n' "$(get MCP_BRIDGE_MODE)"
    printf '  bind       %s\n' "$(get MCP_BRIDGE_BIND)"
    printf '  port       %s\n' "$(get MCP_BRIDGE_PORT)"
    printf '  auth       %s\n' "$(get MCP_BRIDGE_AUTH_MODE)"
    printf '  sudo       %s\n' "$(get MCP_BRIDGE_ALLOW_SUDO)"
    printf '  guardrails %s\n' "$(get MCP_BRIDGE_GUARDRAILS)"
    printf '  endpoint   %s\n' "$(endpoint)"
    echo
    echo "=== control panel ==="
    printf '  service    %s\n' "$(systemctl --user is-active mcp-panel 2>/dev/null || echo 'not installed')"
    printf '  url        http://%s:%s\n' \
      "$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo 127.0.0.1)" \
      "$(get MCP_BRIDGE_PANEL_PORT)"
    echo
    echo "=== recent activity ==="
    tail -n 8 "$STATE/audit.log" 2>/dev/null || echo "  (nothing yet)"
    ;;

  start)   "$PY" -m panel.supervisor start ;;
  stop)    "$PY" -m panel.supervisor stop ;;
  restart) "$PY" -m panel.supervisor restart ;;

  gui)     exec "$DIR/mcp-bridge-gui" ;;
  endpoint) endpoint ;;
  token)   get MCP_BRIDGE_TOKEN ;;
  passphrase) get MCP_BRIDGE_PASSPHRASE ;;

  rotate)
    new="mcpk_$("$PY" -c 'import secrets;print(secrets.token_urlsafe(32))')"
    sed -i "s|^MCP_BRIDGE_TOKEN=.*|MCP_BRIDGE_TOKEN=$new|" "$ENV_FILE"
    echo "New token: $new"
    "$PY" -m panel.supervisor restart
    ;;

  revoke)
    sqlite3 "$STATE/bridge.db" \
      "DELETE FROM tokens; DELETE FROM auth_codes; DELETE FROM pending;" 2>/dev/null || true
    echo "OAuth sessions revoked."
    ;;

  ps)
    curl -fsS -H "X-Bridge-Admin: $(get MCP_BRIDGE_PASSPHRASE)" \
      "http://127.0.0.1:$(get MCP_BRIDGE_PORT)/admin/status" 2>/dev/null \
      | "$PY" -m json.tool || echo "Bridge not responding."
    ;;

  abort)
    if [[ -n "${2:-}" ]]; then body="{\"id\":\"$2\"}"; else body='{}'; fi
    curl -fsS -X POST -H "X-Bridge-Admin: $(get MCP_BRIDGE_PASSPHRASE)" \
      -H 'Content-Type: application/json' -d "$body" \
      "http://127.0.0.1:$(get MCP_BRIDGE_PORT)/admin/abort" 2>/dev/null \
      | "$PY" -m json.tool || echo "Bridge not responding."
    ;;

  reset)
    curl -fsS -X POST -H "X-Bridge-Admin: $(get MCP_BRIDGE_PASSPHRASE)" \
      "http://127.0.0.1:$(get MCP_BRIDGE_PORT)/admin/reset" 2>/dev/null \
      | "$PY" -m json.tool || echo "Bridge not responding."
    ;;

  audit)   tail -f "$STATE/audit.log" ;;
  logs)    tail -f "$STATE/bridge.log" ;;

  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
