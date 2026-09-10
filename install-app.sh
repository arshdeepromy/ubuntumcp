#!/usr/bin/env bash
# Installs the control panel as a desktop app:
#  - a user systemd service so the panel is always available (and survives reboot)
#  - a .desktop entry so "MCP Bridge" appears in your application menu
#
#   bash install-app.sh
#
# No sudo required; everything lands under your home directory.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
UNITS="$HOME/.config/systemd/user"

mkdir -p "$APPS" "$ICONS" "$UNITS" "$HOME/.local/bin"

echo "Installing icon..."
cat > "$ICONS/mcp-bridge.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#5b8cff"/><stop offset="1" stop-color="#8b5bff"/>
  </linearGradient></defs>
  <rect width="64" height="64" rx="15" fill="url(#g)"/>
  <g fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round">
    <path d="M20 26v-7M28 26v-7"/>
    <rect x="15" y="26" width="18" height="13" rx="3.2" fill="#fff" stroke="none"/>
    <path d="M24 39v6a6 6 0 0 0 6 6h9a6 6 0 0 0 6-6v-9"/>
    <circle cx="45" cy="30" r="5"/>
  </g>
</svg>
SVG

echo "Installing launcher..."
ln -sf "$DIR/mcp-bridge-gui" "$HOME/.local/bin/mcp-bridge-gui"

cat > "$APPS/mcp-bridge.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MCP Bridge
GenericName=Remote access control
Comment=Start, stop and configure Claude's access to this machine
Exec=$DIR/mcp-bridge-gui
Icon=mcp-bridge
Terminal=false
Categories=System;RemoteAccess;
Keywords=mcp;claude;remote;ssh;bridge;
StartupWMClass=MCPBridge
EOF

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "Installing user service..."
cp "$DIR/systemd/mcp-panel.service" "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now mcp-panel.service

# Without lingering, user services stop when you log out.
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo
  echo "To keep the panel running when you are logged out, run:"
  echo "    sudo loginctl enable-linger $USER"
fi

sleep 2
PORT=$(grep -oP '^MCP_BRIDGE_PANEL_PORT=\K.*' "$HOME/.mcp-bridge/bridge.env" 2>/dev/null || echo 8900)
LAN=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo 127.0.0.1)

cat <<EOF

Installed.

  Application menu:  "MCP Bridge"
  Terminal:          mcp-bridge-gui
  Any LAN device:    http://$LAN:$PORT

Sign in with the operator passphrase:
  grep PASSPHRASE ~/.mcp-bridge/bridge.env

Panel status:  systemctl --user status mcp-panel
EOF
