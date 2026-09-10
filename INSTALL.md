# Installing on another Ubuntu machine

Needs Ubuntu 22.04 or newer (developed on 26.04) and a normal user account with sudo.
Everything runs as that user; nothing needs to be run as root directly.

## 1. Install

```bash
sudo apt-get install -y git
git clone https://github.com/arshdeepromy/ubuntumcp.git ~/mcp-bridge
cd ~/mcp-bridge
bash install.sh --with-sudo        # drop --with-sudo if you don't want passwordless sudo
```

The clone **must** end up at `~/mcp-bridge` (the systemd user units point there).

`install.sh` does the following:

| Step | What |
|---|---|
| apt | python3, venv, pip, git, curl, sqlite3 |
| Python | `.venv` + pinned `requirements.txt` |
| Browser tools | Node 18+ (NodeSource if apt's is too old), `@playwright/mcp@0.0.79` globally, Chromium + its system libs |
| Services | user units `playwright-mcp`, `mcp-panel`, `mcp-bridge-autostart`, all enabled |
| Lingering | `loginctl enable-linger` so it all survives logout and reboot |
| Desktop | "MCP Bridge" launcher in the app menu |
| `--with-sudo` | `/etc/sudoers.d/99-mcp-bridge` (passwordless sudo for this user) |

On first start a fresh `~/.mcp-bridge/bridge.env` is generated with a **new**
passphrase and bearer token. Nothing is shared with any other machine.

Check it:

```bash
bash bridgectl.sh status
systemctl --user status mcp-panel mcp-bridge-autostart playwright-mcp --no-pager
curl -s http://127.0.0.1:8901/healthz
```

## 2a. Connect from your LAN (Claude Code / Desktop / Cursor)

Default mode is **LAN + bearer token**. On the client machine:

```bash
claude mcp add --transport http <name> "$(ssh user@host 'bash ~/mcp-bridge/bridgectl.sh endpoint')" \
  --header "Authorization: Bearer <token from: bash bridgectl.sh token>"
```

Or open the panel at `http://<machine-ip>:8900`, sign in with
`bash bridgectl.sh passphrase`, and copy the ready-made command.
If ufw is on: `sudo ufw allow 8900/tcp && sudo ufw allow 8901/tcp`.

## 2b. Connect from claude.ai (browser) — Tunnel mode

claude.ai can't reach LAN addresses, so you need a public HTTPS hostname.
Pick one:

### Option A — Cloudflare dashboard tunnel (recommended; easiest to manage)

1. Cloudflare Zero Trust → Networks → Tunnels → **Create tunnel** (cloudflared).
   Copy the install token.
2. On the machine:
   ```bash
   curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
   echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
     | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt-get update -y && sudo apt-get install -y cloudflared
   sudo cloudflared service install <TOKEN>
   ```
3. In the tunnel → **Public hostname**: e.g. `mcp2.yourdomain.com` → service `http://127.0.0.1:8901`.
4. In the MCP Bridge panel (Network/Access): Mode **Tunnel**, Auth **OAuth**,
   Public URL `https://mcp2.yourdomain.com`, then **Restart**.
   Or by hand:
   ```bash
   sed -i -e 's|^MCP_BRIDGE_MODE=.*|MCP_BRIDGE_MODE=tunnel|' \
          -e 's|^MCP_BRIDGE_AUTH_MODE=.*|MCP_BRIDGE_AUTH_MODE=oauth|' \
          -e 's|^MCP_BRIDGE_PUBLIC_URL=.*|MCP_BRIDGE_PUBLIC_URL=https://mcp2.yourdomain.com|' \
          ~/.mcp-bridge/bridge.env
   bash bridgectl.sh restart
   curl -s https://mcp2.yourdomain.com/healthz
   ```
5. claude.ai → Settings → Connectors → **Add custom connector** →
   `https://mcp2.yourdomain.com/mcp` → approve on the consent page using the passphrase.

### Option B — `setup-tunnel.sh` (CLI-managed named tunnel)

`bash setup-tunnel.sh mcp2.yourdomain.com` logs cloudflared in, creates the
tunnel and DNS route, and installs **system** units `mcp-bridge` +
`mcp-bridge-tunnel`. If you use this, disable the user autostart so two
bridges don't fight over port 8901:
`systemctl --user disable --now mcp-bridge-autostart`.

### Option C — throwaway URL

`bash quick-tunnel.sh` — trycloudflare.com URL, changes every run.

## Updating

```bash
cd ~/mcp-bridge && git pull
.venv/bin/pip install -r requirements.txt -q
bash bridgectl.sh restart
```

## Uninstalling

```bash
systemctl --user disable --now mcp-bridge-autostart mcp-panel playwright-mcp
bash ~/mcp-bridge/bridgectl.sh stop
rm -f ~/.config/systemd/user/{mcp-bridge-autostart,mcp-panel,playwright-mcp}.service
rm -f ~/.local/share/applications/mcp-bridge.desktop ~/.local/bin/mcp-bridge-gui
sudo bash ~/mcp-bridge/install-sudoers.sh --remove
rm -rf ~/mcp-bridge ~/.mcp-bridge            # the second one holds the secrets
```
