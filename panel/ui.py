"""The control panel's HTML. Single page, no build step, no external assets."""

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Bridge — Sign in</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #0b0d12; color: #e7e9ee; display: grid; place-items: center;
         min-height: 100vh; margin: 0; padding: 1.5rem; }
  .card { background: #12151d; border: 1px solid #232838; border-radius: 16px;
          padding: 2.25rem; width: 100%; max-width: 25rem;
          box-shadow: 0 20px 60px rgba(0,0,0,.5); }
  .logo { width: 42px; height: 42px; border-radius: 11px; margin-bottom: 1.1rem;
          background: linear-gradient(135deg,#5b8cff,#8b5bff); display: grid;
          place-items: center; font-size: 1.3rem; }
  h1 { font-size: 1.2rem; margin: 0 0 .3rem; }
  p { color: #8f98ab; font-size: .87rem; margin: 0 0 1.5rem; line-height: 1.5; }
  label { display:block; font-size: .78rem; color: #8f98ab; margin-bottom: .4rem; }
  input { width: 100%; padding: .75rem .85rem; border-radius: 10px;
          border: 1px solid #2c3346; background: #0a0c11; color: #e7e9ee;
          font-size: .95rem; }
  input:focus { outline: 2px solid #5b8cff; outline-offset: 1px; border-color: transparent; }
  button { width: 100%; margin-top: 1rem; padding: .8rem; border: 0; border-radius: 10px;
           background: #5b8cff; color: #fff; font-size: .95rem; font-weight: 600;
           cursor: pointer; }
  button:hover { background: #4a7bf0; }
  .err { color: #ff8b8b; font-size: .85rem; margin-top: .9rem; min-height: 1.2em; }
  .hint { font-size: .76rem; color: #6b7488; margin-top: 1.3rem; line-height: 1.5;
          border-top: 1px solid #1e2331; padding-top: 1rem; }
  code { background:#0a0c11; padding:.12rem .35rem; border-radius:4px; color:#9db4ff; }
</style></head><body>
<div class="card">
  <div class="logo">🔌</div>
  <h1>MCP Bridge</h1>
  <p>Sign in to control remote access to <strong>__HOSTNAME__</strong>.</p>
  <form id="f">
    <label for="p">Operator passphrase</label>
    <input id="p" type="password" autofocus autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
  <div class="err" id="e">__ERROR__</div>
  <div class="hint">Forgot it? Run <code>grep PASSPHRASE ~/.mcp-bridge/bridge.env</code>
    in a terminal on the host.</div>
</div>
<script>
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const e = document.getElementById('e'); e.textContent = '';
  const r = await fetch('/api/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({passphrase: document.getElementById('p').value})
  });
  const j = await r.json().catch(() => ({}));
  if (r.ok) location.href = '/';
  else { e.textContent = j.error || 'Sign in failed.'; document.getElementById('p').select(); }
});
</script></body></html>
"""


PANEL_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Bridge Control</title>
<style>
  :root { color-scheme: dark;
    --bg:#0b0d12; --panel:#12151d; --panel2:#0f121a; --line:#232838;
    --text:#e7e9ee; --dim:#8f98ab; --faint:#6b7488;
    --accent:#5b8cff; --ok:#3ddc97; --warn:#ffb454; --bad:#ff6b6b; }
  * { box-sizing: border-box; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); margin: 0;
         padding: 1.5rem 1.25rem 4rem; line-height: 1.5; }
  .wrap { max-width: 60rem; margin: 0 auto; }
  header { display:flex; align-items:center; gap:.9rem; flex-wrap:wrap;
           margin-bottom: 1.6rem; }
  .logo { width:40px; height:40px; border-radius:11px; flex:none;
          background:linear-gradient(135deg,#5b8cff,#8b5bff); display:grid;
          place-items:center; font-size:1.25rem; }
  h1 { font-size:1.15rem; margin:0; }
  .sub { color: var(--dim); font-size:.82rem; }
  .spacer { flex:1 1 auto; }
  .pill { display:inline-flex; align-items:center; gap:.45rem; padding:.35rem .8rem;
          border-radius:999px; font-size:.8rem; font-weight:600; border:1px solid var(--line); }
  .pill.on  { background:rgba(61,220,151,.12); color:var(--ok); border-color:rgba(61,220,151,.3); }
  .pill.off { background:rgba(255,107,107,.1); color:var(--bad); border-color:rgba(255,107,107,.28); }
  .dot { width:7px; height:7px; border-radius:50%; background:currentColor; }
  .pill.on .dot { animation: pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(20rem,1fr)); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:14px;
          padding:1.15rem 1.25rem; }
  .card.wide { grid-column: 1 / -1; }
  h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.09em;
       color:var(--faint); margin:0 0 .9rem; font-weight:700; }
  .row { display:flex; gap:.6rem; flex-wrap:wrap; }
  .row > * { flex:1 1 auto; }
  button { padding:.6rem .95rem; border:1px solid var(--line); border-radius:9px;
           background:#1a1f2c; color:var(--text); font-size:.87rem; font-weight:600;
           cursor:pointer; transition:.13s; font-family:inherit; }
  button:hover:not(:disabled) { background:#232a3a; border-color:#333c52; }
  button:disabled { opacity:.4; cursor:not-allowed; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.primary:hover:not(:disabled) { background:#4a7bf0; }
  button.danger { background:rgba(255,107,107,.12); border-color:rgba(255,107,107,.3);
                  color:var(--bad); }
  button.danger:hover:not(:disabled) { background:rgba(255,107,107,.2); }
  button.sm { padding:.4rem .7rem; font-size:.78rem; flex:0 0 auto; }
  label { display:block; font-size:.75rem; color:var(--dim); margin:.85rem 0 .35rem; }
  label:first-of-type { margin-top:0; }
  input, select { width:100%; padding:.6rem .7rem; border-radius:9px;
                  border:1px solid #2c3346; background:var(--panel2); color:var(--text);
                  font-size:.88rem; font-family:inherit; }
  input:focus, select:focus { outline:2px solid var(--accent); outline-offset:1px; }
  .endpoint { display:flex; gap:.5rem; align-items:stretch; }
  .endpoint code { flex:1; background:var(--panel2); border:1px solid var(--line);
                   border-radius:9px; padding:.6rem .7rem; font-size:.83rem;
                   font-family:ui-monospace,Menlo,monospace; color:#9db4ff;
                   overflow-x:auto; white-space:nowrap; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(6.5rem,1fr));
           gap:.75rem; }
  .stat { background:var(--panel2); border:1px solid var(--line); border-radius:10px;
          padding:.7rem .8rem; }
  .stat .k { font-size:.68rem; color:var(--faint); text-transform:uppercase;
             letter-spacing:.06em; }
  .stat .v { font-size:1.05rem; font-weight:600; margin-top:.15rem; }
  .stat .d { font-size:.68rem; color:var(--faint); margin-top:.15rem; }
  .switch { display:flex; align-items:center; justify-content:space-between;
            padding:.6rem 0; border-bottom:1px solid #1a1f2b; }
  .switch:last-child { border-bottom:0; }
  .switch .t { font-size:.87rem; }
  .switch .d { font-size:.74rem; color:var(--faint); margin-top:.1rem; }
  .toggle { position:relative; width:42px; height:23px; flex:none; }
  .toggle input { position:absolute; opacity:0; width:100%; height:100%; margin:0;
                  cursor:pointer; z-index:2; }
  .track { position:absolute; inset:0; background:#2c3346; border-radius:999px;
           transition:.18s; }
  .track::after { content:''; position:absolute; width:17px; height:17px; left:3px;
                  top:3px; background:#fff; border-radius:50%; transition:.18s; }
  .toggle input:checked ~ .track { background:var(--accent); }
  .toggle input:checked ~ .track::after { transform:translateX(19px); }
  pre { background:var(--panel2); border:1px solid var(--line); border-radius:10px;
        padding:.8rem; font-size:.76rem; overflow-x:auto; margin:0;
        font-family:ui-monospace,Menlo,monospace; color:#b9c2d4;
        max-height:19rem; overflow-y:auto; }
  .log-line { padding:.32rem .5rem; border-bottom:1px solid #171b26; font-size:.76rem;
              font-family:ui-monospace,Menlo,monospace; display:flex; gap:.6rem;
              align-items:center; }
  .log-line:last-child { border-bottom:0; }
  .log-t { color:var(--faint); flex:none; }
  .log-e { flex:none; font-weight:600; min-width:9.5rem; }
  .log-e.blocked, .log-e.rejected { color:var(--bad); }
  .log-e.granted, .log-e.issued { color:var(--ok); }
  .log-d { color:var(--dim); overflow:hidden; text-overflow:ellipsis;
           white-space:nowrap; }
  .logbox { background:var(--panel2); border:1px solid var(--line); border-radius:10px;
            max-height:19rem; overflow-y:auto; }
  .note { font-size:.76rem; color:var(--faint); margin-top:.75rem; line-height:1.55; }
  .banner { border-radius:10px; padding:.75rem .9rem; font-size:.81rem; margin-bottom:1rem;
            display:none; }
  .banner.show { display:block; }
  .banner.ok  { background:rgba(61,220,151,.1); border:1px solid rgba(61,220,151,.3); color:var(--ok); }
  .banner.err { background:rgba(255,107,107,.1); border:1px solid rgba(255,107,107,.3); color:var(--bad); }
  .warnbox { background:rgba(255,180,84,.09); border:1px solid rgba(255,180,84,.28);
             color:#ffcf8f; border-radius:10px; padding:.75rem .9rem; font-size:.79rem;
             margin-top:.85rem; line-height:1.55; }
  .hidden { display:none !important; }
  @media (max-width:640px){ body{padding:1rem .75rem 3rem} .card{padding:1rem} }
</style></head><body>
<div class="wrap">
  <header>
    <div class="logo">🔌</div>
    <div>
      <h1>MCP Bridge</h1>
      <div class="sub" id="hostline">—</div>
    </div>
    <div class="spacer"></div>
    <span class="pill off" id="statuspill"><span class="dot"></span><span id="statustext">…</span></span>
    <button class="sm" onclick="logout()">Sign out</button>
  </header>

  <div class="banner" id="banner"></div>

  <div class="grid">
    <div class="card wide">
      <h2>Service</h2>
      <div class="row">
        <button class="primary" id="btn-start" onclick="act('start')">Start</button>
        <button id="btn-stop" onclick="act('stop')">Stop</button>
        <button id="btn-restart" onclick="act('restart')">Restart</button>
      </div>
      <div class="stats" style="margin-top:1rem">
        <div class="stat"><div class="k">Uptime</div><div class="v" id="s-uptime">—</div></div>
        <div class="stat"><div class="k">PID</div><div class="v" id="s-pid">—</div></div>
        <div class="stat"><div class="k">Memory</div><div class="v" id="s-mem">—</div></div>
        <div class="stat"><div class="k">Running now</div><div class="v" id="s-busy">—</div></div>
        <div class="stat"><div class="k">Auth</div><div class="v" id="s-tok">—</div>
             <div class="d">token, or live OAuth tokens</div></div>
      </div>
    </div>

    <div class="card wide">
      <h2>Connections</h2>
      <div class="stats">
        <div class="stat"><div class="k">Configured clients</div>
             <div class="v" id="c-clients">—</div>
             <div class="d" id="c-clients-d">connectors registered</div></div>
        <div class="stat"><div class="k">Live sessions</div>
             <div class="v" id="c-sessions">—</div>
             <div class="d">MCP transports open</div></div>
        <div class="stat"><div class="k">Valid tokens</div>
             <div class="v" id="c-tokens">—</div>
             <div class="d">unexpired credentials</div></div>
      </div>

      <h2 style="margin:1.3rem 0 .6rem">Configured clients</h2>
      <div class="logbox" id="clients-list"></div>
      <div class="note"><strong>Remove</strong> deletes a connector's registration
        and every token it holds, and drops any session it has open — the client
        must authorize again from scratch.</div>

      <h2 style="margin:1.3rem 0 .6rem">Valid tokens</h2>
      <div class="logbox" id="tokens-list"></div>
      <div class="note"><strong>Revoke</strong> kills a single access token: its
        next call is rejected. The client can still refresh unless you remove it
        above. Shown by prefix only — the secret never leaves the server.</div>

      <h2 style="margin:1.3rem 0 .6rem">Live sessions</h2>
      <div class="logbox" id="sessions-list"></div>
      <div class="note">Each open MCP transport, the client that owns it, how long
        it has been connected, and whether it is running a command right now.</div>

      <div style="display:flex;align-items:center;justify-content:space-between;
                  margin:1.3rem 0 .6rem">
        <h2 style="margin:0">In-flight commands</h2>
        <div style="display:flex;gap:.5rem">
          <button class="sm danger" onclick="abortAll()">Abort all</button>
          <button class="sm danger" onclick="resetSessions()">Reset sessions</button>
        </div>
      </div>
      <div class="logbox" id="inflight"></div>
      <div class="note">If a call is wedged, <strong>Abort</strong> kills that command
        and lets the client get its reply. <strong>Reset sessions</strong> also tears
        down every MCP transport — clients reconnect automatically and keep their
        authorization.</div>
    </div>

    <div class="card wide">
      <h2>Connection</h2>
      <label>MCP endpoint — add this to your client</label>
      <div class="endpoint">
        <code id="endpoint">—</code>
        <button class="sm" onclick="copyText(document.getElementById('endpoint').textContent,this)">Copy</button>
      </div>

      <div id="tokenblock">
        <label>Bearer token</label>
        <div class="endpoint">
          <code id="token">••••••••••••••••</code>
          <button class="sm" onclick="toggleToken(this)">Show</button>
          <button class="sm" onclick="copyToken(this)">Copy</button>
        </div>
      </div>

      <label>Claude Code — run this on the client machine</label>
      <div class="endpoint">
        <code id="cmd">—</code>
        <button class="sm" onclick="copyText(document.getElementById('cmd').dataset.full,this)">Copy</button>
      </div>
      <div class="warnbox" id="lanwarn"></div>
    </div>

    <div class="card">
      <h2>Network</h2>
      <label for="f-mode">Access mode</label>
      <select id="f-mode" onchange="modeChanged()">
        <option value="lan">LAN — reachable on your local network</option>
        <option value="tunnel">Tunnel — public HTTPS via Cloudflare</option>
      </select>

      <div id="lanfields">
        <label for="f-bind">Listen on</label>
        <select id="f-bind"></select>
      </div>

      <div id="tunnelfields" class="hidden">
        <label for="f-url">Public HTTPS URL</label>
        <input id="f-url" placeholder="https://mcp.yourdomain.com">
      </div>

      <label for="f-port">Port</label>
      <input id="f-port" type="number" min="1" max="65535">

      <label for="f-auth">Authentication</label>
      <select id="f-auth">
        <option value="token">Bearer token — simple, for LAN clients</option>
        <option value="oauth">OAuth — browser consent, needed for claude.ai</option>
      </select>
      <div class="note" id="authnote"></div>

      <div class="row" style="margin-top:1.1rem">
        <button class="primary" onclick="saveSettings()">Save &amp; restart</button>
      </div>
    </div>

    <div class="card">
      <h2>Capabilities</h2>
      <div class="switch">
        <div><div class="t">Guardrails</div>
             <div class="d">Block catastrophic commands</div></div>
        <label class="toggle"><input type="checkbox" id="f-guard"><span class="track"></span></label>
      </div>
      <div class="note">Sudo is controlled in <strong>Privileged access</strong> below —
        turning it on there is all that's needed for privileged tool calls.</div>
      <label for="f-timeout">Default command timeout (seconds)</label>
      <input id="f-timeout" type="number" min="5" max="3600">
      <div class="row" style="margin-top:1.1rem">
        <button class="primary" onclick="saveSettings()">Save &amp; restart</button>
      </div>
    </div>

    <div class="card">
      <h2>Privileged access</h2>
      <div class="switch">
        <div><div class="t">Passwordless sudo</div>
             <div class="d" id="sudo-state">checking…</div></div>
        <span class="pill off" id="sudo-pill"><span class="dot"></span><span id="sudo-pill-t">—</span></span>
      </div>
      <div id="sudo-form" style="margin-top:.9rem">
        <div id="sudo-pw-row">
          <label for="f-syspass">Account password for <code id="whoami">this account</code></label>
          <input id="f-syspass" type="password" autocomplete="off"
                 placeholder="Your Ubuntu login password">
        </div>
        <div class="row" style="margin-top:.7rem">
          <button class="primary" id="sudo-btn" onclick="installSudo(false)">Enable sudo</button>
        </div>
        <div class="note" id="sudo-note">Sent once to install
          <code>/etc/sudoers.d/99-mcp-bridge</code>, never stored or logged. The
          file is validated with <code>visudo -c</code> before install, so a bad
          rule cannot lock you out. Turning it back off needs no password.</div>
        <div class="warnbox">This lets <em>every</em> process running as your user
          become root without authentication — not only the bridge. While it's off,
          a privileged tool call is refused and the chat is told to ask you to turn
          it on here.</div>
      </div>
    </div>

    <div class="card">
      <h2>Security</h2>
      <label for="f-pass">New operator passphrase</label>
      <input id="f-pass" type="password" placeholder="At least 8 characters"
             autocomplete="new-password">
      <div class="row" style="margin-top:.7rem">
        <button onclick="changePassphrase()">Change passphrase</button>
      </div>
      <div class="row" style="margin-top:1.4rem">
        <button class="danger" onclick="rotateToken()">Rotate bearer token</button>
      </div>
      <div class="row" style="margin-top:.6rem">
        <button class="danger" onclick="revokeAll()">Revoke all sessions</button>
      </div>
      <div class="note">Rotating or revoking cuts off every connected client
        immediately. They must be reconfigured with the new credential.</div>
    </div>

    <div class="card">
      <h2>Diagnostics</h2>
      <div class="row">
        <button class="sm" onclick="loadLog()">Refresh service log</button>
      </div>
      <pre id="svclog" style="margin-top:.8rem">—</pre>
    </div>

    <div class="card wide">
      <h2>Activity</h2>
      <div class="logbox" id="audit"></div>
    </div>
  </div>
</div>

<script>
let STATE = {};
let PRINCIPALS = {clients: [], tokens: [], auth_mode: 'token'};
let tokenVisible = false;
// Until the form has been filled from the server at least once it still holds
// its static HTML defaults (mode=lan, auth=token, empty URL, empty bind list).
// Saving in that state would silently overwrite the real config with them.
let formHydrated = false;

// Write text into a node, tolerating one that isn't there. A missing id used to
// throw mid-refresh and abandon everything below it -- including the settings
// form and the activity feed -- with no visible error but a half-dead page.
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
  else console.warn('panel: no element #' + id);
}

function banner(msg, ok) {
  const b = document.getElementById('banner');
  b.textContent = msg;
  b.className = 'banner show ' + (ok ? 'ok' : 'err');
  clearTimeout(b._t);
  b._t = setTimeout(() => b.className = 'banner', 5000);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = '/'; return null; }
  return r;
}

function fmtUptime(s) {
  if (s === null || s === undefined) return '—';
  const d = Math.floor(s/86400), h = Math.floor(s%86400/3600),
        m = Math.floor(s%3600/60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm ' + (s % 60) + 's';
  return s + 's';
}

async function refresh() {
  const r = await api('/api/status');
  if (!r) return;
  const j = await r.json();
  STATE = j;

  setText('hostline', j.hostname + ' · ' + j.os);
  if (j.user) setText('whoami', j.user);

  const pill = document.getElementById('statuspill');
  if (pill) pill.className = 'pill ' + (j.running ? 'on' : 'off');
  setText('statustext', j.running ? 'Running' : 'Stopped');

  setText('s-uptime', fmtUptime(j.uptime_seconds));
  setText('s-pid', j.pid || '—');
  setText('s-mem', j.memory_mb ? j.memory_mb + ' MB' : '—');
  setText('s-tok', j.settings.auth_mode === 'token'
    ? (j.running ? 'token' : '—') : j.active_tokens);

  ['start','stop','restart'].forEach(b => {
    const el = document.getElementById('btn-' + b);
    if (el) el.disabled = b === 'start' ? j.running : !j.running;
  });

  document.getElementById('endpoint').textContent = j.endpoint;

  const tb = document.getElementById('tokenblock');
  tb.style.display = j.settings.auth_mode === 'token' ? '' : 'none';
  if (!tokenVisible) document.getElementById('token').textContent = '•'.repeat(24);

  const cmd = j.settings.auth_mode === 'token'
    ? 'claude mcp add --transport http ubuntu ' + j.endpoint +
      ' --header "Authorization: Bearer ' + j.token + '"'
    : 'claude mcp add --transport http ubuntu ' + j.endpoint;
  const el = document.getElementById('cmd');
  el.dataset.full = cmd;
  el.textContent = j.settings.auth_mode === 'token'
    ? cmd.replace(j.token, '<token>') : cmd;

  const warn = document.getElementById('lanwarn');
  if (j.settings.mode === 'lan') {
    warn.innerHTML = '<strong>LAN mode:</strong> reachable only from this network. ' +
      'claude.ai in the browser <strong>cannot</strong> reach a private address — ' +
      'use Tunnel mode for that. Works with Claude Code, Claude Desktop and other ' +
      'clients on your LAN.';
    warn.style.display = '';
  } else if (!j.settings.public_url) {
    warn.innerHTML = '<strong>Tunnel mode needs a public URL.</strong> Set it above, ' +
      'then run <code>bash setup-tunnel.sh &lt;hostname&gt;</code> on the host.';
    warn.style.display = '';
  } else { warn.style.display = 'none'; }

  // The option list is structure, not something the user types, so keep it in
  // sync unconditionally -- an unpopulated <select> reads back as '' and would
  // be saved as an empty bind address. Compare contents, not just length: the
  // host's addresses can change without the count changing.
  const bind = document.getElementById('f-bind');
  const want = JSON.stringify(j.bind_options.map(o => o.value));
  const have = JSON.stringify(Array.from(bind.options).map(o => o.value));
  if (want !== have) {
    const keep = bind.value;
    bind.innerHTML = '';
    j.bind_options.forEach(o => {
      const opt = document.createElement('option');
      opt.value = o.value; opt.textContent = o.label;
      bind.appendChild(opt);
    });
    if (keep) bind.value = keep;
  }

  // Don't clobber the one field the user is editing -- but leave every other
  // field in sync, and fill all of them on the first pass. Guarding the whole
  // block meant focusing any input froze the entire form on its HTML defaults.
  const active = document.activeElement;
  const editing = id => formHydrated && active === document.getElementById(id);

  if (!editing('f-mode'))    document.getElementById('f-mode').value = j.settings.mode;
  if (!editing('f-port'))    document.getElementById('f-port').value = j.settings.port;
  if (!editing('f-auth'))    document.getElementById('f-auth').value = j.settings.auth_mode;
  if (!editing('f-url'))     document.getElementById('f-url').value = j.settings.public_url || '';
  if (!editing('f-guard'))   document.getElementById('f-guard').checked = j.settings.guardrails;
  if (!editing('f-timeout')) document.getElementById('f-timeout').value = j.settings.timeout;
  if (!editing('f-bind')) {
    bind.value = j.settings.bind;
    // A stored value with no matching option leaves the select blank; fall
    // back rather than letting the next save write the blank back out.
    if (!bind.value) bind.value = 'lan';
  }

  formHydrated = true;
  modeChanged();
  renderAudit(j.audit);
}

function renderAudit(entries) {
  const box = document.getElementById('audit');
  if (!box) return;
  if (!entries || !entries.length) {
    box.innerHTML = '<div class="log-line"><span class="log-d">No commands yet.</span></div>';
    return;
  }
  box.innerHTML = entries.map(e => {
    // Lead with the outcome rather than the raw event name -- for a command
    // feed, "exit 1" is the thing worth scanning for.
    let cls = '', label;
    if (/blocked|denied|rejected|revoked/.test(e.event)) { cls = 'blocked'; label = 'blocked'; }
    else if (e.running)      { cls = 'issued'; label = 'running'; }
    else if (e.timed_out)    { cls = 'blocked'; label = 'timeout'; }
    else if (e.exit_code === 0)          { label = 'ok'; }
    else if (e.exit_code !== undefined && e.exit_code !== null) {
      cls = 'blocked'; label = 'exit ' + e.exit_code;
    } else { label = e.event; }
    if (e.sudo) label = 'sudo ' + label;

    const ms = e.duration_ms;
    const dur = (ms === undefined || ms === null) ? ''
      : (ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms');
    const detail = e.command || e.path || e.reason || e.client_id || '';

    return '<div class="log-line"><span class="log-t">' + esc(e.time) +
      '</span><span class="log-e ' + cls + '">' + esc(label) +
      '</span><span class="log-d" title="' + esc(detail) + '">' + esc(detail) + '</span>' +
      (dur ? '<span class="log-t" style="margin-left:auto;flex:0 0 auto">' +
             esc(dur) + '</span>' : '') +
      '</div>';
  }).join('');
}

function esc(s) {
  return String(s === undefined || s === null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function fmtAge(s) {
  if (s === null || s === undefined) return '';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h + 'h ' + m + 'm';
}

function renderSessions(list) {
  const box = document.getElementById('sessions-list');
  if (!box) return;
  if (!list.length) {
    box.innerHTML = '<div class="log-line"><span class="log-d">' +
      'No clients connected.</span></div>';
    return;
  }
  box.innerHTML = list.map(s => {
    const tok = (s.token_expires_seconds !== null && s.token_expires_seconds !== undefined)
      ? 'token expires in ' + fmtAge(s.token_expires_seconds) : '';
    const detail = s.busy
      ? ('running: ' + (s.command || ''))
      : ('idle' + (tok ? ' · ' + tok : ''));
    return '<div class="log-line">' +
      '<span class="log-e ' + (s.busy ? 'issued' : '') + '">' + esc(s.client_name) + '</span>' +
      '<span class="log-t">' + esc(s.id) +
        (s.client_id ? ' · ' + esc(s.client_id) : '') + '</span>' +
      '<span class="log-d" title="' + esc(detail) + '">' + esc(detail) + '</span>' +
      '<span class="log-t" style="margin-left:auto;flex:0 0 auto">up ' +
        esc(fmtAge(s.connected_seconds)) + '</span></div>';
  }).join('');
}

function emptyLine(msg) {
  return '<div class="log-line"><span class="log-d">' + esc(msg) + '</span></div>';
}

// Rows are killed by array index into PRINCIPALS, never by interpolating a name
// or id into the onclick -- so nothing a client chose (its name, redirect) can
// break out of the handler.
function renderClients() {
  const box = document.getElementById('clients-list');
  if (!box) return;
  if (PRINCIPALS.auth_mode === 'token') {
    box.innerHTML = emptyLine('Bearer-token mode — no OAuth clients.'); return;
  }
  const list = PRINCIPALS.clients || [];
  if (!list.length) { box.innerHTML = emptyLine('No clients registered.'); return; }
  box.innerHTML = list.map((c, i) => {
    const uris = (c.redirect_uris || []).join(', ');
    const meta = esc((c.client_id || '').slice(0, 8)) + ' · ' + c.live_tokens +
      ' token' + (c.live_tokens === 1 ? '' : 's');
    return '<div class="log-line">' +
      '<span class="log-e">' + esc(c.client_name) + '</span>' +
      '<span class="log-t">' + meta + '</span>' +
      '<span class="log-d" title="' + esc(uris) + '">' + esc(uris) + '</span>' +
      '<button class="sm danger" style="margin-left:auto;flex:0 0 auto" ' +
        'onclick="killClient(' + i + ')">Remove</button></div>';
  }).join('');
}

function renderTokens() {
  const box = document.getElementById('tokens-list');
  if (!box) return;
  if (PRINCIPALS.auth_mode === 'token') {
    box.innerHTML = emptyLine('Bearer-token mode — one static credential.'); return;
  }
  const list = PRINCIPALS.tokens || [];
  if (!list.length) { box.innerHTML = emptyLine('No active tokens.'); return; }
  box.innerHTML = list.map((t, i) => {
    return '<div class="log-line">' +
      '<span class="log-e">' + esc(t.client_name) + '</span>' +
      '<span class="log-t">' + esc(t.prefix) + '…</span>' +
      '<span class="log-d">expires in ' + esc(fmtAge(t.expires_seconds)) + '</span>' +
      '<button class="sm danger" style="margin-left:auto;flex:0 0 auto" ' +
        'onclick="killToken(' + i + ')">Revoke</button></div>';
  }).join('');
}

async function refreshPrincipals() {
  const r = await api('/api/principals');
  if (!r) return;
  PRINCIPALS = await r.json();
  renderClients();
  renderTokens();
}

async function killClient(i) {
  const c = (PRINCIPALS.clients || [])[i];
  if (!c) return;
  if (!confirm('Remove client "' + c.client_name + '"?\n\nIt loses every token, ' +
      'any open session is dropped, and it must authorize again from scratch.')) return;
  const r = await api('/api/client/delete', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({client_id: c.client_id})});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  refreshPrincipals(); refreshLive();
}

async function killToken(i) {
  const t = (PRINCIPALS.tokens || [])[i];
  if (!t) return;
  if (!confirm('Revoke this token for "' + t.client_name + '"?\n\n' +
      'Its next call is rejected. The client can refresh a new one unless you ' +
      'remove it under Configured clients.')) return;
  const r = await api('/api/token/revoke', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ref: t.ref})});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  refreshPrincipals(); refreshLive();
}

function modeChanged() {
  const lan = document.getElementById('f-mode').value === 'lan';
  document.getElementById('lanfields').classList.toggle('hidden', !lan);
  document.getElementById('tunnelfields').classList.toggle('hidden', lan);

  // OAuth mandates an HTTPS issuer (RFC 8414), which a plain LAN address
  // cannot satisfy -- so don't offer a combination that refuses to boot.
  const auth = document.getElementById('f-auth');
  const oauthOpt = auth.querySelector('option[value="oauth"]');
  oauthOpt.disabled = lan;
  if (lan && auth.value === 'oauth') auth.value = 'token';
  document.getElementById('authnote').textContent = lan
    ? 'OAuth is unavailable on LAN: it requires HTTPS. Switch to Tunnel mode to use it.'
    : '';
}

async function act(what) {
  ['start','stop','restart'].forEach(b =>
    document.getElementById('btn-'+b).disabled = true);
  const r = await api('/api/' + what, {method:'POST'});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  await refresh();
}

async function saveSettings() {
  // Never submit a form that hasn't been filled from the server yet -- it
  // would post HTML defaults over the live config.
  if (!formHydrated) {
    banner('Settings are still loading. Try again in a moment.', false);
    return;
  }
  const mode = document.getElementById('f-mode').value;
  const bind = document.getElementById('f-bind').value;
  if (mode === 'lan' && !bind) {
    banner('Pick a "Listen on" address before saving.', false);
    return;
  }
  const body = {
    mode: mode,
    bind: bind,
    port: parseInt(document.getElementById('f-port').value, 10),
    auth_mode: document.getElementById('f-auth').value,
    public_url: document.getElementById('f-url').value.trim(),
    guardrails: document.getElementById('f-guard').checked,
    timeout: parseInt(document.getElementById('f-timeout').value, 10),
  };
  const r = await api('/api/settings', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  await refresh();
}

async function changePassphrase() {
  const v = document.getElementById('f-pass').value;
  if (v.length < 8) { banner('Passphrase must be at least 8 characters.', false); return; }
  const r = await api('/api/passphrase', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({passphrase: v})});
  if (r) { const j = await r.json(); banner(j.message, j.ok);
           if (j.ok) document.getElementById('f-pass').value = ''; }
}

async function rotateToken() {
  if (!confirm('Rotate the bearer token? Every connected client stops working until reconfigured.')) return;
  const r = await api('/api/token/rotate', {method:'POST'});
  if (r) { const j = await r.json(); banner(j.message, j.ok); tokenVisible = false; }
  await refresh();
}

async function revokeAll() {
  if (!confirm('Revoke all sessions? Clients must authorize again.')) return;
  const r = await api('/api/revoke', {method:'POST'});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  await refresh();
}

function toggleToken(btn) {
  tokenVisible = !tokenVisible;
  document.getElementById('token').textContent =
    tokenVisible ? STATE.token : '•'.repeat(24);
  btn.textContent = tokenVisible ? 'Hide' : 'Show';
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
  }
  const old = btn.textContent; btn.textContent = 'Copied';
  setTimeout(() => btn.textContent = old, 1400);
}
function copyToken(btn) { copyText(STATE.token, btn); }

async function refreshLive() {
  const r = await api('/api/live');
  if (!r) return;
  const j = await r.json();

  setText('c-clients', j.auth_mode === 'token' ? '—' : (j.registered_clients ?? 0));
  setText('c-clients-d',
    j.auth_mode === 'token' ? 'n/a in token mode' : 'connectors registered');
  setText('c-sessions', j.sessions ?? 0);
  setText('c-tokens', j.auth_mode === 'token' ? '1 (static)' : (j.active_tokens ?? 0));

  renderSessions(j.session_details || []);

  const cmds = j.running_commands || [];
  setText('s-busy', cmds.length);

  const box = document.getElementById('inflight');
  if (!box) return;
  if (!cmds.length) {
    box.innerHTML = '<div class="log-line"><span class="log-d">Nothing running.</span></div>';
    return;
  }
  box.innerHTML = cmds.map(c => {
    // Flag anything past 80% of its own timeout as likely stuck.
    const stuck = c.elapsed_seconds > c.timeout * 0.8;
    return '<div class="log-line">' +
      '<span class="log-t">' + c.elapsed_seconds.toFixed(0) + 's</span>' +
      '<span class="log-e' + (stuck ? ' blocked' : '') + '">' +
        (c.sudo ? 'sudo ' : '') + 'pid ' + (c.pid || '?') + '</span>' +
      '<span class="log-d" title="' + esc(c.command) + '">' + esc(c.command) + '</span>' +
      '<button class="sm danger" style="margin-left:auto;flex:0 0 auto" ' +
        'onclick="abortOne(\'' + c.id + '\')">Abort</button></div>';
  }).join('');
}

async function abortOne(id) {
  const r = await api('/api/abort', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  refreshLive();
}

async function abortAll() {
  const r = await api('/api/abort', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  refreshLive();
}

async function resetSessions() {
  if (!confirm('Reset all MCP sessions? In-flight commands are killed and clients reconnect.')) return;
  const r = await api('/api/reset-sessions', {method:'POST'});
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  refreshLive();
}

async function refreshSudo() {
  const r = await api('/api/sudo/status');
  if (!r) return;
  const j = await r.json();
  const pill = document.getElementById('sudo-pill');
  pill.className = 'pill ' + (j.works ? 'on' : 'off');
  document.getElementById('sudo-pill-t').textContent = j.works ? 'Enabled' : 'Disabled';
  document.getElementById('sudo-state').textContent = j.works
    ? 'Privileged tool calls work'
    : (j.rule_installed ? 'Rule present but not effective'
                        : 'Privileged tool calls are refused');
  const btn = document.getElementById('sudo-btn');
  btn.textContent = j.works ? 'Turn off sudo' : 'Enable sudo';
  btn.onclick = () => installSudo(j.works);
  // No password needed to turn it off, so hide the field when it's on.
  const pwRow = document.getElementById('sudo-pw-row');
  if (pwRow) pwRow.style.display = j.works ? 'none' : '';
  const note = document.getElementById('sudo-note');
  if (note) note.style.display = j.works ? 'none' : '';
}

async function installSudo(remove) {
  const pw = document.getElementById('f-syspass');
  // Turning ON needs the account password; turning OFF does not (the bridge
  // already holds root while passwordless sudo is on).
  if (!remove && !pw.value) { banner('Enter your account password to turn sudo on.', false); return; }
  if (remove && !confirm('Turn off passwordless sudo? Privileged tool calls will fail until you turn it back on.')) return;
  const btn = document.getElementById('sudo-btn');
  btn.disabled = true; btn.textContent = 'Working…';
  const r = await api('/api/sudo/install', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: pw.value, remove: !!remove})});
  pw.value = '';                       // never keep it in the DOM
  btn.disabled = false;
  if (r) { const j = await r.json(); banner(j.message, j.ok); }
  refreshSudo();
}

async function loadLog() {
  const r = await api('/api/log');
  if (!r) return;
  const j = await r.json();
  document.getElementById('svclog').textContent = j.log || '(empty)';
}

async function logout() {
  await fetch('/api/logout', {method:'POST'});
  location.href = '/';
}

refresh();
refreshLive();
refreshPrincipals();
refreshSudo();
loadLog();
setInterval(refresh, 4000);
setInterval(refreshLive, 2000);   // in-flight view must feel live
setInterval(refreshPrincipals, 5000);
setInterval(refreshSudo, 15000);
</script></body></html>
"""
