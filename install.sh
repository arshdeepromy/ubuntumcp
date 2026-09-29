#!/usr/bin/env bash
# One-shot installer for a fresh Linux machine.
#
#   bash install.sh              # venv + deps + config + panel service + menu entry
#   bash install.sh --no-app     # venv + deps + config only, no service or .desktop
#
# Everything lands under your home directory; no sudo is needed for this script.
# The venv is built here rather than shipped because a virtualenv bakes in the
# absolute path and Python version of the machine that created it.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
STATE="${MCP_BRIDGE_STATE:-$HOME/.mcp-bridge}"
MIN_PY="3.11"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. interpreter ----------------------------------------------------------
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c "import sys;raise SystemExit(0 if sys.version_info>=tuple(int(x) for x in '$MIN_PY'.split('.')) else 1)"; then
    PY="$c"; break
  fi
done
[[ -n "$PY" ]] || die "need Python >= $MIN_PY. On Debian/Ubuntu: sudo apt install -y python3 python3-venv"
say "Using $("$PY" -V) at $(command -v "$PY")"

# --- 2. venv + dependencies -------------------------------------------------
# Three ways in, because `python3 -m venv` alone fails on any Debian/Ubuntu that
# ships python3 without the separately packaged ensurepip.
USED_UV=0
if [[ ! -x "$VENV/bin/python" ]]; then
  say "Creating virtualenv in .venv ..."
  if "$PY" -m venv "$VENV" 2>/dev/null; then
    :
  elif command -v uv >/dev/null 2>&1 && uv venv --python "$PY" "$VENV"; then
    USED_UV=1
  elif "$PY" -m venv --without-pip "$VENV" 2>/dev/null; then
    # venv works, only ensurepip is missing -- bootstrap pip straight into it.
    say "ensurepip unavailable; bootstrapping pip ..."
    BOOTSTRAP="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$BOOTSTRAP"
    else
      wget -qO "$BOOTSTRAP" https://bootstrap.pypa.io/get-pip.py
    fi
    "$VENV/bin/python" "$BOOTSTRAP" >/dev/null
    rm -f "$BOOTSTRAP"
  else
    rm -rf "$VENV"
    die "could not create a virtualenv. Install the venv package and retry:
    sudo apt install -y python3-venv      # Debian/Ubuntu
    sudo dnf install -y python3-virtualenv # Fedora/RHEL"
  fi
fi

say "Installing dependencies (needs network) ..."
if [[ $USED_UV -eq 1 ]]; then
  VIRTUAL_ENV="$VENV" uv pip install -r "$DIR/requirements.txt"
else
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install -r "$DIR/requirements.txt"
fi

# Fail here rather than at first run if something did not land.
"$VENV/bin/python" -c 'import mcp, psutil, pydantic, starlette, uvicorn, httpx' \
  || die "dependencies did not install cleanly; see the pip output above."

# --- 3. first-run config -----------------------------------------------------
# Generates ~/.mcp-bridge/bridge.env with a fresh passphrase and bearer token.
# Secrets are never shipped in this package; each machine makes its own.
say "Initializing configuration in $STATE ..."
( cd "$DIR" && "$VENV/bin/python" -c 'from bridge.config import ensure_initialized; ensure_initialized()' )

# --- 4. desktop app + panel service -----------------------------------------
if [[ "${1:-}" != "--no-app" ]]; then
  say "Installing control panel service and menu entry ..."
  bash "$DIR/install-app.sh"
fi

# --- 5. what to do next ------------------------------------------------------
get() { grep -oP "^$1=\K.*" "$STATE/bridge.env" 2>/dev/null || true; }
LAN=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo 127.0.0.1)

cat <<TXT

$(printf '\033[1mInstalled.\033[0m')

  Control panel   http://$LAN:$(get MCP_BRIDGE_PANEL_PORT)
  Passphrase      $(get MCP_BRIDGE_PASSPHRASE)
  MCP endpoint    http://$LAN:$(get MCP_BRIDGE_PORT)/mcp
  Bearer token    $(get MCP_BRIDGE_TOKEN)

Start the bridge itself from the panel, or:

  bash bridgectl.sh start
  bash bridgectl.sh status

Connect Claude Code from any machine on this LAN:

  claude mcp add --transport http kiro-box http://$LAN:$(get MCP_BRIDGE_PORT)/mcp \\
    --header "Authorization: Bearer $(get MCP_BRIDGE_TOKEN)"

For claude.ai in a browser you need Tunnel mode -- a LAN address is not
routable from Anthropic's servers. See README.md, then:

  bash setup-tunnel.sh mcp.yourdomain.com

Privileged tool calls (sudo=true) need passwordless sudo for this account:

  sudo bash install-sudoers.sh

Keep the panel running while logged out:

  sudo loginctl enable-linger $USER
TXT
