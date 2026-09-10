#!/usr/bin/env bash
# Grants passwordless sudo to the user running the bridge, so tools with
# sudo=true work without an interactive password prompt.
#
# READ THIS FIRST: this makes every process running as this user able to become
# root with no further authentication -- not only the bridge. That is the
# tradeoff of remote sudo. Run it only if that is what you want.
#
#   sudo bash install-sudoers.sh          # install
#   sudo bash install-sudoers.sh --remove # undo
set -euo pipefail

TARGET_USER="${SUDO_USER:-$(id -un)}"
FILE=/etc/sudoers.d/99-mcp-bridge

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

if [[ "${1:-}" == "--remove" ]]; then
  rm -f "$FILE"
  echo "Removed $FILE -- sudo now requires a password again."
  exit 0
fi

echo "About to grant passwordless sudo to user: $TARGET_USER"
# --yes skips the confirmation for callers who already know what they asked for.
if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Type the username again to confirm: " CONFIRM
  if [[ "$CONFIRM" != "$TARGET_USER" ]]; then
    echo "Mismatch, aborting." >&2
    exit 1
  fi
fi

TMP=$(mktemp)
cat > "$TMP" <<EOF
# Installed by mcp-bridge (install-sudoers.sh)
# Allows $TARGET_USER to run sudo without a password, which the MCP bridge
# needs for privileged tool calls. Remove with:
#   sudo bash install-sudoers.sh --remove
$TARGET_USER ALL=(ALL) NOPASSWD: ALL
EOF

# Never install an unparseable sudoers file -- that can lock you out of sudo.
if visudo -cf "$TMP" >/dev/null; then
  install -m 0440 -o root -g root "$TMP" "$FILE"
  rm -f "$TMP"
  echo "Installed $FILE"
  echo "Verify with:  sudo -n true && echo 'passwordless sudo works'"
else
  rm -f "$TMP"
  echo "Generated sudoers file failed validation; nothing was changed." >&2
  exit 1
fi
