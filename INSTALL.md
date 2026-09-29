# Installing on another Linux PC

This archive is the MCP bridge that Claude connects to as the **kiro box**
connector: an authenticated MCP endpoint that exposes shell access and a set of
structured system tools (`system_overview`, `read_journal`, `service_status`,
`network_overview`, `disk_usage`, `read_file`, `write_file`, `run_command`,
`service_control`, `top_processes`, `list_directory`), plus a web control panel
for starting, stopping and configuring it.

## What is and isn't in the box

Included: all source, the control panel, the systemd units, the tunnel helpers
and the installer.

**Not** included, deliberately:

- `.venv/` — a virtualenv hardcodes the absolute path and Python version of the
  machine that built it, so `install.sh` builds a fresh one on the target.
- `~/.mcp-bridge/` — the config, database, audit log and **secrets**. The
  bearer token and operator passphrase are per-machine and are generated on
  first run. Nothing from the source machine is carried over.

## Requirements

- Linux with systemd (built and tested on Ubuntu 26.04)
- Python >= 3.11, including the `venv` module
  (`sudo apt install -y python3 python3-venv`)
- Network access for `pip install` during setup
- `cloudflared` only if you want Tunnel mode

## Install

```bash
unzip mcp-bridge-<version>.zip
cd mcp-bridge
bash install.sh
```

That creates `.venv`, installs the pinned dependencies, generates
`~/.mcp-bridge/bridge.env` with a fresh passphrase and bearer token, installs
the control panel as a user service, and adds "MCP Bridge" to the application
menu. It finishes by printing the panel URL, the endpoint and the token.

Use `bash install.sh --no-app` to skip the service and menu entry.

## Then

```bash
bash bridgectl.sh start       # start the bridge
bash bridgectl.sh status      # endpoint, token, recent activity
mcp-bridge-gui                # open the control panel
```

Connect a client on the same LAN:

```bash
claude mcp add --transport http kiro-box http://<host-ip>:8901/mcp \
  --header "Authorization: Bearer <token>"
```

For claude.ai in a browser, LAN mode will not work — custom connectors are
fetched by Anthropic's servers, which cannot route to a private address. Switch
to Tunnel mode (`bash setup-tunnel.sh mcp.yourdomain.com`), which also turns on
OAuth and the consent screen.

## Ports

| Port | What |
|---|---|
| 8900 | control panel |
| 8901 | MCP endpoint |
| 8932 | Playwright MCP OAuth proxy (optional, `pwproxy`) |

Change them in the panel or in `~/.mcp-bridge/bridge.env`.

## Optional pieces

- `sudo bash install-sudoers.sh` — passwordless sudo for this account, required
  for tool calls with `sudo=true`. Read the warning in that file first: it
  applies to *every* process running as the user, not only the bridge.
- `systemd/pw-mcp-proxy.service` — the Playwright MCP OAuth proxy. It needs a
  Playwright MCP server of your own on `127.0.0.1:8931` and its own settings in
  `~/.mcp-bridge/pwproxy.env`; it is not set up by `install.sh`.
- `systemd/mcp-bridge.service` — run the bridge as its own user service instead
  of letting the panel supervise it. The units use `%h`, so they work for any
  user: `cp systemd/mcp-bridge.service ~/.config/systemd/user/ && systemctl --user enable --now mcp-bridge`.

## Uninstall

```bash
systemctl --user disable --now mcp-panel mcp-bridge 2>/dev/null
rm -f ~/.config/systemd/user/mcp-{panel,bridge}.service
rm -f ~/.local/share/applications/mcp-bridge.desktop ~/.local/bin/mcp-bridge-gui
sudo bash install-sudoers.sh --remove     # if you installed it
rm -rf ~/.mcp-bridge                      # config, secrets, audit log
rm -rf <this directory>
```

## Read the security note

`README.md` ends with a section on what this actually grants. It is full
command execution including sudo, reachable over the network. Read it before
exposing the endpoint anywhere.
