#!/usr/bin/env bash
# One-shot installer for a fresh Ubuntu machine.
#
#   git clone https://github.com/arshdeepromy/ubuntumcp.git ~/mcp-bridge
#   cd ~/mcp-bridge && bash install.sh [--with-sudo] [--no-browser]
#
#   --with-sudo    also grant this user passwordless sudo (so sudo=true tool
#                  calls work). Read install-sudoers.sh before using this.
#   --no-browser   skip Playwright/Chromium (the 23 browser_* tools won't work)
#
# Run as your normal user, NOT with sudo. It will ask for your password when
# it needs root (apt, npm -g, lingering).
set -euo pipefail

WITH_SUDO=0; WITH_BROWSER=1
for arg in "$@"; do
  case "$arg" in
    --with-sudo)  WITH_SUDO=1 ;;
    --no-browser) WITH_BROWSER=0 ;;
    -h|--help)    sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user (not root / sudo). It calls sudo itself." >&2
  exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="$HOME/mcp-bridge"
UNITS="$HOME/.config/systemd/user"
PW_VERSION="0.0.79"   # @playwright/mcp version known to work with bridge/browser.py

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# The user units reference %h/mcp-bridge, so the code must live (or be linked) there.
if [[ "$DIR" != "$HOME_DIR" ]]; then
  if [[ -e "$HOME_DIR" && "$(readlink -f "$HOME_DIR")" != "$DIR" ]]; then
    echo "$HOME_DIR already exists and is not this checkout. Clone into ~/mcp-bridge instead." >&2
    exit 1
  fi
  ln -sfn "$DIR" "$HOME_DIR"
  echo "Linked $HOME_DIR -> $DIR"
fi

step "Installing system packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip git curl sqlite3 iproute2 ca-certificates

PYV=$(python3 -c 'import sys;print("%d%02d"%sys.version_info[:2])')
if (( PYV < 310 )); then
  echo "Python 3.10+ is required (found $(python3 --version)). Use Ubuntu 22.04 or newer." >&2
  exit 1
fi
# python3-venv doesn't always pull in the versioned package (ensurepip lives there).
PYDOT=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "python${PYDOT}-venv" || true

step "Creating Python virtualenv"
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
.venv/bin/python -c "import bridge.config, panel.app; print('python deps OK')"

if (( WITH_BROWSER )); then
  step "Installing Node.js + Playwright MCP (browser tools)"
  NODE_MAJOR=$(node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0)
  if [[ -z "$NODE_MAJOR" || "$NODE_MAJOR" -lt 18 ]]; then
    # Ubuntu 22.04's apt node is too old; use NodeSource LTS.
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
  fi
  command -v npm >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y npm
  sudo npm install -g "@playwright/mcp@$PW_VERSION"
  NPM_ROOT=$(npm root -g)
  PW_CLI="$NPM_ROOT/@playwright/mcp/node_modules/playwright/cli.js"
  [[ -f "$PW_CLI" ]] || PW_CLI="$NPM_ROOT/playwright/cli.js"
  PW_BIN=$(command -v playwright-mcp || echo "$NPM_ROOT/@playwright/mcp/cli.js")
  # Use the playwright bundled with @playwright/mcp so the browser build matches.
  sudo node "$PW_CLI" install-deps chromium
  PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" node "$PW_CLI" install chromium

  mkdir -p "$UNITS"
  sed "s|/usr/local/bin/playwright-mcp|$PW_BIN|" systemd/playwright-mcp.service > "$UNITS/playwright-mcp.service"
fi

step "Keeping services running while logged out"
sudo loginctl enable-linger "$USER"

# Over SSH there may be no user bus yet; lingering starts one at /run/user/UID.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
for _ in $(seq 1 10); do [[ -S "$XDG_RUNTIME_DIR/bus" ]] && break; sleep 1; done

step "Installing user services (panel, bridge autostart)"
mkdir -p "$UNITS"
cp systemd/mcp-bridge-autostart.service "$UNITS/"
bash "$DIR/install-app.sh"          # panel service + desktop launcher; creates ~/.mcp-bridge/bridge.env
systemctl --user daemon-reload
if (( WITH_BROWSER )); then systemctl --user enable --now playwright-mcp.service; fi
systemctl --user enable --now mcp-bridge-autostart.service

if (( WITH_SUDO )); then
  step "Granting passwordless sudo to $USER"
  sudo bash "$DIR/install-sudoers.sh" --yes
fi

if command -v ufw >/dev/null && sudo ufw status | grep -q 'Status: active'; then
  echo
  echo "ufw is active. To reach the panel/bridge from your LAN run:"
  echo "    sudo ufw allow 8900/tcp && sudo ufw allow 8901/tcp"
fi

sleep 3
step "Done"
bash "$DIR/bridgectl.sh" status || true
cat <<MSG

Control panel:   http://$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo 127.0.0.1):8900
Passphrase:      bash bridgectl.sh passphrase
Bearer token:    bash bridgectl.sh token
MCP endpoint:    bash bridgectl.sh endpoint

For claude.ai in a browser you need Tunnel mode -- see INSTALL.md.
MSG
