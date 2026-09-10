# Ubuntu MCP Bridge

Gives Claude authenticated shell access to this machine — effectively SSH,
exposed as MCP tools — with a GUI to start, stop and configure it.

```
LAN mode (default)                    Tunnel mode
──────────────────                    ───────────
MCP client on your network            claude.ai in a browser
        │ bearer token                        │ OAuth 2.1
        ▼                                     ▼
http://<machine-lan-ip>:8901/mcp         Cloudflare Tunnel → 127.0.0.1
        │                                     │
        └──────────► bash on this host ◄──────┘
```

## Install

```bash
git clone https://github.com/arshdeepromy/ubuntumcp.git ~/mcp-bridge
cd ~/mcp-bridge && bash install.sh --with-sudo
```

Full walkthrough, tunnel setup, updating and uninstalling: [INSTALL.md](INSTALL.md).

## The GUI

```bash
bash install-app.sh     # one time: adds "MCP Bridge" to your application menu
mcp-bridge-gui          # or launch it from a terminal
```

Also reachable from any device on your LAN at `http://<machine-lan-ip>:8900` —
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
fetched by Anthropic's servers, not by your browser, so `<machine-lan-ip>` is
unroutable from there. Use Tunnel mode for browser Claude.

The panel won't let you pick LAN + OAuth: RFC 8414 requires an HTTPS issuer, and
a plain LAN address can't provide one.

## Connecting a client

**Claude Code** (on any machine on your LAN):

```bash
claude mcp add --transport http ubuntu http://<machine-lan-ip>:8901/mcp \
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

### Browser automation

A headless Chromium, driven over the same connector — enough to test a web app
end to end without leaving the conversation.

| Tool | What it does |
|---|---|
| `browser_navigate` | Go to a URL (or `back=true`), returns the page snapshot |
| `browser_snapshot` | Accessibility tree with a `ref=` per element |
| `browser_find` | Locate elements by text or regex |
| `browser_click` / `browser_hover` / `browser_drag` | Pointer actions |
| `browser_type` / `browser_fill_form` / `browser_press_key` | Text entry |
| `browser_select_option` / `browser_file_upload` / `browser_handle_dialog` | Form controls, choosers, alerts |
| `browser_wait_for` | Wait for text to appear or disappear, or a fixed time |
| `browser_evaluate` / `browser_run_code` | JS in the page / raw Playwright against `page` |
| `browser_screenshot` | PNG of the page or one element, returned as an image |
| `browser_console_messages` / `browser_network_requests` / `browser_network_request` | What the page logged and fetched |
| `browser_tabs` / `browser_resize` | Tab management, viewport size |
| `browser_state` / `browser_reset` | Connection diagnostics; discard the context |

The usual loop is: `browser_navigate`, read the snapshot, act on a `ref`,
snapshot again. Refs are invalidated by navigation and re-renders, so re-snapshot
after anything that changes the page. Prefer snapshots over screenshots for
finding things — they are smaller and unambiguous.

The work is done by [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp)
running as a separate user service on `127.0.0.1:8931`; the bridge proxies to it
rather than carrying its own copy of that tool surface:

```
systemctl --user status playwright-mcp
journalctl --user -u playwright-mcp -f
```

Two things about that service are load-bearing. It runs with
`--shared-browser-context`, because the browser context is shared only between
clients connected *at the same time* and is destroyed when the last one leaves —
so the bridge holds a single connection open for its whole life, and browser
calls are serialized through it (two overlapping actions would race on one page).
And `--allowed-hosts` includes `127.0.0.1:8931`, because playwright-mcp checks
the `Host` header and otherwise 403s anything not addressed to `localhost`.

State therefore persists across calls — a login survives until you navigate away,
close the tab, or call `browser_reset`. Sessions are *not* isolated per client:
everyone connected to the bridge shares one browser.

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
  tools.py       the 34 tools (11 system, 23 browser)
  browser.py     the held-open connection to playwright-mcp
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

**Browser tools fail to connect.** `browser_state` reports what the bridge
thinks; the backing service is `systemctl --user status playwright-mcp`. A
reboot with lingering disabled is the usual cause — `loginctl enable-linger $USER`
lets user services start without a login.

**A page is wedged, or actions time out just after a click.** An open dialog
blocks every other call: answer it with `browser_handle_dialog`. Failing that,
`browser_reset` throws the context away and starts clean.

**Refs like `e7` stop matching.** They are invalidated whenever the page
re-renders. Take a fresh `browser_snapshot` and use the new refs.
