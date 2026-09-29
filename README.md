# Ubuntu MCP Bridge

Gives Claude authenticated shell access to this machine — effectively SSH,
exposed as MCP tools — with a GUI to start, stop and configure it.

```
LAN mode (default)                    Tunnel mode
──────────────────                    ───────────
MCP client on your network            claude.ai in a browser
        │ bearer token                        │ OAuth 2.1
        ▼                                     ▼
http://<this-host-ip>:8901/mcp         Cloudflare Tunnel → 127.0.0.1
        │                                     │
        └──────────► bash on this host ◄──────┘
```

## The GUI

```bash
bash install-app.sh     # one time: adds "MCP Bridge" to your application menu
mcp-bridge-gui          # or launch it from a terminal
```

Also reachable from any device on your LAN at `http://<this-host-ip>:8900` —
phone, laptop, whatever. Sign in with the operator passphrase.

From the panel you can start/stop/restart the bridge, change the port and bind
address, switch access mode, change the passphrase, rotate the bearer token,
revoke sessions, enable passwordless sudo, watch a live activity feed, and
**abort wedged commands**.

The panel runs as a user service (`systemctl --user status mcp-panel`) and comes
back on reboot. To keep it up while you're logged out:
`sudo loginctl enable-linger $USER`.

## Which mode do I want?

|  | LAN | Tunnel |
|---|---|---|
| Reachable from | your local network only | anywhere |
| Auth | bearer token | OAuth 2.1 + consent screen |
| Works with claude.ai in a browser | **no** | yes |
| Works with Claude Code / Desktop / Cursor on your LAN | yes | yes |
| Extra setup | none | Cloudflare domain |

**claude.ai in the browser cannot reach a LAN address.** Custom connectors are
fetched by Anthropic's servers, not by your browser, so `<this-host-ip>` is
unroutable from there. Use Tunnel mode for browser Claude.

The panel won't let you pick LAN + OAuth: RFC 8414 requires an HTTPS issuer, and
a plain LAN address can't provide one.

## Connecting a client

**Claude Code** (on any machine on your LAN):

```bash
claude mcp add --transport http ubuntu http://<this-host-ip>:8901/mcp \
  --header "Authorization: Bearer <token>"
```

The panel shows this command with your real token, ready to copy.

**claude.ai in a browser** — switch to Tunnel mode first:

```bash
bash setup-tunnel.sh mcp.yourdomain.com   # persistent, survives reboot
bash quick-tunnel.sh                       # throwaway URL, no domain needed
```

Then Settings → Connectors → Add custom connector, paste the `/mcp` URL, and
approve on the consent screen.

## Recovering a wedged session

When a tool call blocks — a command waiting on stdin, or a long timeout — the
client sits there with no reply. You no longer have to abandon the session:

- **Connections** in the panel lists every in-flight command with its elapsed
  time, PID and originating client. Anything past 80% of its own timeout is
  flagged red.
- **Abort** kills that command's process group. The blocked call returns
  immediately with whatever output it produced plus an `ABORTED` marker, so the
  client recovers instead of hanging.
- **Reset sessions** additionally tears down every MCP transport. Clients
  reconnect on their next request and keep their authorization — no
  re-approval needed.

From a terminal:

```bash
bash bridgectl.sh ps           # connections + in-flight commands
bash bridgectl.sh abort cmd3   # kill one
bash bridgectl.sh abort        # kill all
bash bridgectl.sh reset        # kill all + drop every session
```

These run over an admin API authenticated with the operator passphrase, not an
OAuth token — deliberately, since a stuck OAuth session is the thing you're
recovering from.

## Connection counts

The panel's **Connections** card shows three different numbers, which are easy
to confuse:

| | Meaning |
|---|---|
| Configured clients | OAuth clients registered — one per connector you added |
| Live sessions | MCP transports currently open |
| Valid tokens | Unexpired access tokens |

A configured client with no live session is normal — Claude opens a session per
conversation and drops it afterwards.

## Read this before you use it

This grants **full command execution including sudo**. That is what it's for,
but the consequences are real:

- Anyone holding the bearer token can run anything as root. It is exactly as
  sensitive as an SSH private key.
- In LAN mode there is no TLS, so the token crosses your network in cleartext.
  Fine on a home LAN you control; not fine on café Wi-Fi.
- Passwordless sudo (`install-sudoers.sh`) lets *every* process running as your
  user become root, not just the bridge.
- Prompt injection is a live risk. If Claude reads a file, log line or web page
  containing instructions, it may act on them.

Kill switches: **Stop** in the GUI, `bash bridgectl.sh stop`, or
**Rotate bearer token** to cut off every client at once.

## Tools

| Tool | What it does |
|---|---|
| `run_command` | Arbitrary shell, optional `sudo`, `cwd`, `timeout` |
| `system_overview` | Uptime, load, CPU, memory, disks, failed units |
| `read_journal` | journalctl with unit / since / priority / grep filters |
| `service_status` | Detailed systemd status for a unit |
| `service_control` | start/stop/restart/enable/disable a unit |
| `read_file` | Read a file, optional `sudo`, `tail` |
| `write_file` | Write a file, takes a `.bak` first |
| `list_directory` | `ls -lh` with timestamps |
| `top_processes` | Processes by CPU or memory |
| `network_overview` | Interfaces, routes, listening sockets, DNS |
| `disk_usage` | Filesystem usage plus largest subdirectories |

## Safeguards

These reduce accident damage. None is a security boundary — anything with a
shell can work around a regex.

- **Catastrophic-command denylist**: `rm -rf /`, `mkfs`, `dd` to block devices,
  fork bombs, `poweroff`, cutting outbound networking, stopping the bridge's own
  services. Toggle in the GUI.
- **Self-protection**: `service_control` refuses to touch `mcp-bridge` or
  `cloudflared`.
- **Sensitive files**: `read_file` refuses SSH private keys, `.aws/credentials`,
  `/etc/shadow`, `.env` and similar.
- **Output redaction**: lines that look like `password=`/`api_key=` are masked.
- **Timeouts**: 60s default, 900s ceiling; the whole process group is killed.
- **Output caps**: 200KB, middle elided.
- **Audit log**: every call appended to `~/.mcp-bridge/audit.log` *before* it runs.
- **Panel login**: passphrase, signed session cookie, 6 attempts per 5 minutes.

## Terminal control

```bash
bash bridgectl.sh status       # what's running, where, recent activity
bash bridgectl.sh start|stop|restart
bash bridgectl.sh endpoint     # the MCP URL
bash bridgectl.sh token        # the bearer token
bash bridgectl.sh rotate       # new bearer token
bash bridgectl.sh audit        # follow the audit log
bash bridgectl.sh ps           # connections + in-flight commands
bash bridgectl.sh abort [id]   # kill a wedged command
bash bridgectl.sh reset        # kill commands + drop all sessions
bash bridgectl.sh gui          # open the panel
```

## Enabling sudo

Either from the panel's **Privileged access** card (enter your account password
once), or in a terminal:

```bash
sudo bash install-sudoers.sh --yes     # grant
sudo bash install-sudoers.sh --remove  # revoke
```

The panel pipes the password to `sudo -S` for that single call and never stores
or logs it. On a LAN-bound panel there is no TLS, so the password crosses your
network in cleartext — if that bothers you, use the terminal, or set
`MCP_BRIDGE_PANEL_BIND=loopback` and use the panel only on this machine.

## Layout

```
bridge/          the MCP server
  config.py      settings, LAN/tunnel resolution
  server.py      app assembly, consent screen, both auth modes
  auth.py        OAuth 2.1 provider (SQLite) + static token verifier
  tools.py       the 11 tools
  exec.py        process isolation, timeouts, output caps
  safety.py      guardrail patterns, secret redaction
  netinfo.py     interface / bind-address detection
panel/           the GUI
  app.py         backend API, session auth
  ui.py          the page
  supervisor.py  start/stop/inspect the bridge process
```

Settings live in `~/.mcp-bridge/bridge.env` (mode 600) and are edited by the
panel; hand edits are preserved.

## Troubleshooting

**Bridge won't start.** `bash bridgectl.sh status`, then
`tail ~/.mcp-bridge/bridge.log` — config errors are reported there.

**Client can't connect over LAN.** Confirm the bind address covers the interface
you're dialing (Network → Listen on), and check any firewall:
`sudo ufw allow 8901/tcp`.

**sudo calls fail with "interactive authentication is required".**
`sudo bash install-sudoers.sh`.

**A command hangs until timeout.** It's waiting on stdin. Add `-y`, `--no-pager`
or `--yes`; stdin is `/dev/null`.

**Panel says the port is in use by the control panel.** The bridge and panel
need different ports (8901 and 8900 by default).
