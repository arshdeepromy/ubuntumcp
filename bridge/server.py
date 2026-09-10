"""Assembles the MCP server, the OAuth provider, and the consent UI."""

from __future__ import annotations

import hmac
import html
import logging

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import audit, registry, tools
from .auth import BridgeAuthProvider, StaticTokenVerifier, Store
from .config import Config

logger = logging.getLogger("mcp-bridge")

_PAGE = """
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
         background: #0f1115; color: #e6e8eb; display: grid;
         place-items: center; min-height: 100vh; margin: 0; padding: 1.5rem; }}
  .card {{ background: #171a21; border: 1px solid #262b36; border-radius: 14px;
          padding: 2rem; max-width: 30rem; width: 100%;
          box-shadow: 0 10px 40px rgba(0,0,0,.45); }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .35rem; }}
  p  {{ color: #9aa4b2; font-size: .9rem; line-height: 1.55; margin: .5rem 0; }}
  dl {{ background: #0f1218; border: 1px solid #262b36; border-radius: 9px;
       padding: .85rem 1rem; margin: 1.1rem 0; font-size: .82rem; }}
  dt {{ color: #7d8797; font-size: .72rem; text-transform: uppercase;
       letter-spacing: .05em; margin-top: .6rem; }}
  dt:first-child {{ margin-top: 0; }}
  dd {{ margin: .15rem 0 0; font-family: ui-monospace, Menlo, monospace;
       word-break: break-all; color: #d7dce4; }}
  input {{ width: 100%; box-sizing: border-box; padding: .7rem .8rem;
          border-radius: 9px; border: 1px solid #313846; background: #0c0e13;
          color: #e6e8eb; font-size: .95rem; margin-top: .3rem; }}
  input:focus {{ outline: 2px solid #4f7cff; outline-offset: 1px; }}
  button {{ width: 100%; margin-top: .9rem; padding: .75rem; border: 0;
           border-radius: 9px; background: #4f7cff; color: #fff;
           font-size: .95rem; font-weight: 600; cursor: pointer; }}
  button:hover {{ background: #3d68e8; }}
  .warn {{ background: #2a1d12; border: 1px solid #5c3a1a; color: #f0b47a;
          border-radius: 9px; padding: .8rem .9rem; font-size: .82rem;
          margin: 1.1rem 0; }}
  .err {{ color: #ff8f8f; font-size: .85rem; margin-top: .8rem; }}
  label {{ font-size: .8rem; color: #9aa4b2; }}
</style>
<div class="card">{body}</div>
"""


def _render(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status)


def build_app(cfg: Config):
    audit.init(cfg.audit_path)
    store = Store(cfg.db_path)
    store.sweep()
    provider = BridgeAuthProvider(cfg, store)

    # Token mode: one static bearer credential, no browser handshake -- what LAN
    # clients want. OAuth mode: full authorization-code flow with the consent
    # screen, required for anything reachable from the internet.
    if cfg.auth_mode == "token":
        auth_kwargs: dict = {
            "token_verifier": StaticTokenVerifier(cfg),
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(cfg.public_url),
                resource_server_url=AnyHttpUrl(cfg.mcp_endpoint),
                required_scopes=["bridge:admin"],
            ),
        }
    else:
        auth_kwargs = {
            "auth_server_provider": provider,
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(cfg.public_url),
                resource_server_url=AnyHttpUrl(cfg.mcp_endpoint),
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,  # Claude registers itself via DCR
                    valid_scopes=["bridge:admin"],
                    default_scopes=["bridge:admin"],
                ),
                revocation_options=RevocationOptions(enabled=True),
                required_scopes=["bridge:admin"],
            ),
        }

    server = MCPServer(
        name="ubuntu-bridge",
        title=f"Ubuntu Bridge ({__import__('os').uname().nodename})",
        version="1.0.0",
        **auth_kwargs,
        instructions=(
            "You are connected to an Ubuntu 26.04 machine over an authenticated "
            "tunnel. You can inspect and change the system, including with sudo.\n\n"
            "Working style:\n"
            "- Diagnose before you change. Read logs and status first.\n"
            "- Prefer the structured tools (system_overview, read_journal, "
            "service_status, network_overview, disk_usage) over raw shell where they fit.\n"
            "- Commands are non-interactive: always pass -y, --no-pager, --yes and "
            "similar, or the call will hang until it times out.\n"
            "- State what a destructive or system-altering command will do, and why, "
            "before you run it.\n"
            "- Long jobs: raise `timeout` rather than backgrounding with &, or the "
            "output is lost when the call returns."
        ),
    )

    tools.register(server, cfg)

    # ---- consent screen (the human gate in front of every authorization) ----

    @server.custom_route("/consent", methods=["GET"])
    async def consent_form(request):
        request_id = request.query_params.get("request_id", "")
        pending = provider.pending_request(request_id)
        if pending is None:
            return _render(
                "Request expired",
                "<h1>Request expired</h1><p>This approval link is no longer valid. "
                "Start the connection again from Claude.</p>",
                status=400,
            )
        body = f"""
          <h1>Authorize access to this machine?</h1>
          <p>An MCP client is asking for shell access to
             <strong>{html.escape(__import__('os').uname().nodename)}</strong>.</p>
          <dl>
            <dt>Client</dt><dd>{html.escape(pending['client_name'])}</dd>
            <dt>Redirect</dt><dd>{html.escape(pending['redirect_uri'])}</dd>
            <dt>Scope</dt><dd>{html.escape(' '.join(pending['scopes']) or 'bridge:admin')}</dd>
          </dl>
          <div class="warn">Approving grants full command execution, including
            <strong>sudo</strong>. Only continue if you started this yourself, and
            check the redirect above points at a Claude domain.</div>
          <form method="post" action="/consent">
            <input type="hidden" name="request_id" value="{html.escape(request_id)}">
            <label for="p">Operator passphrase</label>
            <input id="p" type="password" name="passphrase" autofocus autocomplete="off">
            <button type="submit">Approve access</button>
          </form>
        """
        return _render("Authorize MCP bridge", body)

    @server.custom_route("/consent", methods=["POST"])
    async def consent_submit(request):
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        passphrase = str(form.get("passphrase", ""))
        ok, outcome = provider.approve(request_id, passphrase)
        if ok:
            return RedirectResponse(outcome, status_code=302)
        body = f"""
          <h1>Not approved</h1>
          <p class="err">{html.escape(outcome)}</p>
          <form method="get" action="/consent">
            <input type="hidden" name="request_id" value="{html.escape(request_id)}">
            <button type="submit">Try again</button>
          </form>
        """
        return _render("Not approved", body, status=403)

    # ---- admin surface -------------------------------------------------- #
    # Authenticated with the operator passphrase rather than an OAuth token, on
    # purpose: this is the recovery path for when the OAuth session is the thing
    # that is stuck. Bound to the same loopback/LAN port as the MCP endpoint.

    def _admin_ok(request) -> bool:
        supplied = request.headers.get("x-bridge-admin", "")
        return bool(supplied) and hmac.compare_digest(supplied, cfg.admin_passphrase)

    def _live_sessions() -> list[str]:
        """Session IDs known to the transport manager (private SDK attribute).

        Terminated transports linger in the manager's dict until its own cleanup
        runs, so filter them out or the count over-reports connections.
        """
        try:
            manager = server.session_manager
        except Exception:  # noqa: BLE001 - not started yet
            return []
        instances = getattr(manager, "_server_instances", None)
        if not isinstance(instances, dict):
            return []
        return [
            sid for sid, transport in instances.items()
            if not getattr(transport, "is_terminated", False)
        ]

    @server.custom_route("/admin/status", methods=["GET"])
    async def admin_status(request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        sessions = _live_sessions()
        return JSONResponse({
            "running_commands": registry.snapshot(),
            "sessions": len(sessions),
            "session_ids": [s[:12] for s in sessions],
            "registered_clients": store.client_count(),
            "active_tokens": store.active_tokens(),
            "auth_mode": cfg.auth_mode,
        })

    @server.custom_route("/admin/abort", methods=["POST"])
    async def admin_abort(request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json() if await request.body() else {}
        target = body.get("id")
        if target:
            ok = registry.abort(target)
            audit.record("admin.abort", target=target, ok=ok)
            return JSONResponse({
                "ok": ok,
                "message": f"Aborted {target}." if ok
                           else f"No running command with id {target}.",
            })
        killed = registry.abort_all()
        audit.record("admin.abort_all", count=killed)
        return JSONResponse({
            "ok": True,
            "message": f"Aborted {killed} running command(s)." if killed
                       else "Nothing was running.",
        })

    @server.custom_route("/admin/reset", methods=["POST"])
    async def admin_reset(request):
        """Kill in-flight commands and tear down every MCP transport session.

        Clients reconnect with a fresh session on their next request; their
        OAuth token stays valid, so no re-authorization is needed.
        """
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        killed = registry.abort_all(reason="session reset")

        terminated = 0
        try:
            manager = server.session_manager
            instances = getattr(manager, "_server_instances", {})
            for transport in list(instances.values()):
                try:
                    await transport.terminate()
                    terminated += 1
                except Exception:  # noqa: BLE001 - keep going through the rest
                    logger.exception("failed to terminate a transport")
        except Exception:  # noqa: BLE001 - manager not started
            logger.exception("session manager unavailable")

        audit.record("admin.reset", commands_killed=killed, sessions_terminated=terminated)
        return JSONResponse({
            "ok": True,
            "message": f"Reset complete: {killed} command(s) killed, "
                       f"{terminated} session(s) terminated. "
                       "Clients reconnect automatically.",
        })

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return JSONResponse({
            "status": "ok",
            "server": "ubuntu-bridge",
            "host": __import__("os").uname().nodename,
            "active_tokens": store.active_tokens(),
            "guardrails": cfg.guardrails,
            "sudo_enabled": cfg.allow_sudo,
        })

    @server.custom_route("/", methods=["GET"])
    async def index(_request):
        return _render(
            "Ubuntu MCP Bridge",
            "<h1>Ubuntu MCP Bridge</h1><p>This is an MCP server endpoint, not a "
            f"website. Add <code>{html.escape(cfg.public_url)}/mcp</code> as a custom "
            "connector in Claude.</p>",
        )

    app = server.streamable_http_app(streamable_http_path="/mcp", host=cfg.bind_host)
    logger.info("mode=%s auth=%s", cfg.mode, cfg.auth_mode)
    logger.info("mcp endpoint: %s", cfg.mcp_endpoint)
    return app
