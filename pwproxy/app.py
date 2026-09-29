"""The proxy Starlette app: OAuth authorization server + consent + /mcp proxy."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from bridge.auth import BridgeAuthProvider, Store
from bridge.config import Config

logger = logging.getLogger("pw-mcp-proxy")

SCOPES = ["playwright:use"]

# Headers that must not be copied verbatim when relaying a request/response:
# they describe *this* hop's connection or framing, which the proxy re-derives.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

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
  button {{ width: 100%; margin-top: .9rem; padding: .75rem; border: 0;
           border-radius: 9px; background: #4f7cff; color: #fff;
           font-size: .95rem; font-weight: 600; cursor: pointer; }}
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


class ProviderTokenVerifier(TokenVerifier):
    """Verify a bearer token by looking it up in the provider's store."""

    def __init__(self, provider: BridgeAuthProvider) -> None:
        self.provider = provider

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.provider.load_access_token(token)


def _strip_roots(body: bytes) -> bytes:
    """Drop the client's ``roots`` capability from an initialize request.

    Playwright MCP blocks on a ``roots/list`` round trip before it creates the
    browser whenever the client advertises ``roots``. That request rides on the
    standalone GET SSE stream, which Claude's connector never opens, so it is
    never answered and every session stalls for the SDK's 60s request timeout --
    longer than the client's tool timeout, so each browser call reports a
    timeout. Nothing here needs roots: the server runs ``--isolated`` with an
    explicit ``--output-dir``, and roots only supplies a fallback cwd.
    """
    if b'"initialize"' not in body:
        return body
    try:
        msg = json.loads(body)
    except ValueError:
        return body
    if not isinstance(msg, dict) or msg.get("method") != "initialize":
        return body
    caps = msg.get("params", {}).get("capabilities")
    if not isinstance(caps, dict) or "roots" not in caps:
        return body
    caps.pop("roots")
    logger.info("stripped client 'roots' capability from initialize")
    return json.dumps(msg).encode()


_SNAPSHOT_MAX_BYTES = int(os.environ.get("PWPROXY_SNAPSHOT_MAX_BYTES", 2_000_000))
_WORKSPACE_ROOT = Path(os.environ.get("PWPROXY_WORKSPACE_ROOT") or Path.home())
_SNAPSHOT_DIR = Path(
    os.environ.get("PWPROXY_SNAPSHOT_DIR") or (_WORKSPACE_ROOT / "playwright-screenshots")
)

_SNAPSHOT_LINK = re.compile(r"^- \[Snapshot\]\(([^)\n]+)\)$", re.MULTILINE)


def _read_snapshot(rel_path: str) -> str | None:
    """Read a snapshot file the Playwright server wrote, if it sits safely
    inside the configured output directory."""
    try:
        root = _SNAPSHOT_DIR.resolve()
        target = (_WORKSPACE_ROOT / rel_path).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root):
        logger.warning("refusing to inline snapshot outside output dir: %s", rel_path)
        return None
    try:
        if target.stat().st_size > _SNAPSHOT_MAX_BYTES:
            logger.info("snapshot %s exceeds inline budget, leaving link", rel_path)
            return None
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read snapshot %s: %s", rel_path, exc)
        return None


def _inline_snapshot_text(text: str) -> str:
    """Replace ``- [Snapshot](file)`` links with the file's actual contents.

    Playwright MCP writes each tool's page snapshot into ``--output-dir`` and
    returns a workspace-relative link, assuming the client can read that path
    off a shared disk. Claude reaches this server over the tunnel and has no
    such disk, so every navigate/click/type came back with a dead link and no
    page content -- the browser worked, but the model was blind. We resolve the
    link here, where the file really exists, and inline it in the same fenced
    form ``browser_snapshot`` already uses, so the client sees one consistent
    shape. Oversized or out-of-tree files keep the link rather than failing.
    """
    def repl(match: "re.Match[str]") -> str:
        content = _read_snapshot(match.group(1))
        if content is None:
            return match.group(0)
        return "```yaml\n" + content.strip("\n") + "\n```"

    return _SNAPSHOT_LINK.sub(repl, text)


def _inline_snapshots(msg: object) -> bool:
    """Rewrite text blocks of a JSON-RPC tool result in place. True if changed."""
    if not isinstance(msg, dict):
        return False
    result = msg.get("result")
    if not isinstance(result, dict):
        return False
    content = result.get("content")
    if not isinstance(content, list):
        return False
    changed = False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        rewritten = _inline_snapshot_text(text)
        if rewritten != text:
            block["text"] = rewritten
            changed = True
    return changed


def _rewrite_sse_event(event: bytes) -> bytes:
    """Inline snapshot links inside one SSE event, leaving anything else byte
    for byte as it came from upstream."""
    if b"[Snapshot](" not in event:
        return event
    lines = event.split(b"\n")
    changed = False
    for i, line in enumerate(lines):
        if not line.startswith(b"data: "):
            continue
        try:
            msg = json.loads(line[6:])
        except ValueError:
            continue
        if _inline_snapshots(msg):
            lines[i] = b"data: " + json.dumps(msg).encode()
            changed = True
    return b"\n".join(lines) if changed else event


# --------------------------------------------------------------------------
# Session keepalive
#
# Playwright MCP pings the client every 3s and closes the session -- dropping
# the browser context and every element ref with it -- if a ping goes
# unanswered for 5s. Those pings ride the standalone GET SSE stream that an
# ordinary MCP client keeps open. Claude's connector never opens it, so every
# session died ~5s after it was created: single tool calls worked (each got a
# fresh session) but navigate-then-click never did.
#
# Rather than disable the server's heartbeat, we behave like a proper client:
# open that stream here and answer on the connector's behalf. Reaping then
# becomes our job, so we track per-session activity and delete sessions that
# have gone quiet. If this proxy dies, our streams die with it and the
# server's own heartbeat cleans up -- the backstop stays intact.
# --------------------------------------------------------------------------

_SESSION_IDLE_TIMEOUT = float(os.environ.get("PWPROXY_SESSION_IDLE_TIMEOUT", 900))
_WATCHDOG_INTERVAL = 30.0

_sessions: dict = {}
_watchdog_task = None


class _Session:
    """What we know about one upstream MCP session."""

    __slots__ = ("sid", "last_seen", "task", "client_owns_stream")

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.last_seen = time.monotonic()
        self.task = None
        self.client_owns_stream = False


def _short(sid: str) -> str:
    return sid[:8]


async def _reply(client, upstream_url: str, sid: str, msg_id, result=None, error=None) -> None:
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = {} if result is None else result
    await client.post(
        upstream_url,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": sid,
        },
        json=payload,
    )


async def _keepalive(client, upstream_url: str, sid: str) -> None:
    """Hold the GET stream open and answer server-to-client requests."""
    headers = {"Accept": "text/event-stream", "Mcp-Session-Id": sid}
    try:
        async with client.stream("GET", upstream_url, headers=headers) as resp:
            if resp.status_code >= 400:
                logger.info("keepalive refused for session %s: HTTP %s",
                            _short(sid), resp.status_code)
                return
            logger.info("keepalive stream open for session %s", _short(sid))
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    msg = json.loads(line[6:])
                except ValueError:
                    continue
                if not isinstance(msg, dict) or "method" not in msg or "id" not in msg:
                    continue  # a notification -- nothing to answer
                method = msg["method"]
                if method == "ping":
                    await _reply(client, upstream_url, sid, msg["id"], {})
                elif method == "roots/list":
                    await _reply(client, upstream_url, sid, msg["id"], {"roots": []})
                else:
                    # Answer rather than hang: an unanswered request would sit
                    # forever and we would never see why.
                    logger.info("unhandled server request %r on session %s",
                                method, _short(sid))
                    await _reply(client, upstream_url, sid, msg["id"],
                                 error={"code": -32601,
                                        "message": f"{method} is not supported by this proxy"})
    except asyncio.CancelledError:
        raise
    except httpx.HTTPError as exc:
        logger.info("keepalive stream for session %s ended: %s", _short(sid), exc)
    finally:
        sess = _sessions.get(sid)
        if sess is not None and sess.task is asyncio.current_task():
            # Drop the record so the next client request can reopen the stream.
            _sessions.pop(sid, None)


async def _watchdog(client, upstream_url: str) -> None:
    """Delete sessions whose client has gone quiet, freeing their browser."""
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL)
        now = time.monotonic()
        for sid, sess in list(_sessions.items()):
            idle = now - sess.last_seen
            if idle < _SESSION_IDLE_TIMEOUT:
                continue
            logger.info("session %s idle %.0fs, releasing it upstream", _short(sid), idle)
            _sessions.pop(sid, None)
            if sess.task is not None:
                sess.task.cancel()
            try:
                await client.delete(upstream_url, headers={"Mcp-Session-Id": sid})
            except httpx.HTTPError as exc:
                logger.info("could not delete session %s upstream: %s", _short(sid), exc)


def _track_session(client, upstream_url: str, sid: str) -> None:
    """Note activity on a session, opening the keepalive stream if needed."""
    global _watchdog_task
    sess = _sessions.get(sid)
    if sess is None:
        sess = _Session(sid)
        _sessions[sid] = sess
        logger.info("tracking session %s", _short(sid))
    sess.last_seen = time.monotonic()
    if sess.task is None and not sess.client_owns_stream:
        sess.task = asyncio.create_task(_keepalive(client, upstream_url, sid))
    if _watchdog_task is None or _watchdog_task.done():
        _watchdog_task = asyncio.create_task(_watchdog(client, upstream_url))


def _release_stream(sid: str) -> None:
    """The real client opened its own GET stream, so stand down for good."""
    sess = _sessions.get(sid)
    if sess is None:
        return
    sess.client_owns_stream = True
    sess.last_seen = time.monotonic()
    if sess.task is not None:
        logger.info("client took over the stream for session %s", _short(sid))
        sess.task.cancel()
        sess.task = None


def _forget_session(sid: str) -> None:
    sess = _sessions.pop(sid, None)
    if sess is not None and sess.task is not None:
        sess.task.cancel()


@asynccontextmanager
async def _lifespan(_app):
    """Cancel keepalive streams when the proxy stops."""
    yield
    await _shutdown_sessions()


async def _shutdown_sessions() -> None:
    for sid, sess in list(_sessions.items()):
        if sess.task is not None:
            sess.task.cancel()
        _sessions.pop(sid, None)
    if _watchdog_task is not None:
        _watchdog_task.cancel()


def _session_id_from(headers) -> str | None:
    for k, v in headers:
        if k.decode().lower() == "mcp-session-id":
            return v.decode()
    return None


def _make_proxy(upstream_url: str):
    """An ASGI app that relays one HTTP request to the upstream Playwright MCP
    and streams the response back, so SSE bodies flow without buffering."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None))

    async def proxy_app(scope, receive, send):
        # Drain the request body (MCP POST payloads are small JSON).
        body = b""
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        body = _strip_roots(body)

        # Follow the session this request belongs to, so the keepalive stream
        # exists for as long as the client is actually using it.
        sid = _session_id_from(scope["headers"])
        if sid:
            if scope["method"] == "DELETE":
                _forget_session(sid)
            elif scope["method"] == "GET":
                _release_stream(sid)
            else:
                _track_session(client, upstream_url, sid)

        req_headers = [
            (k.decode(), v.decode())
            for k, v in scope["headers"]
            if k.decode().lower() not in _HOP_BY_HOP
            and k.decode().lower() != "authorization"  # the token stops at the proxy
        ]

        try:
            async with client.stream(
                scope["method"], upstream_url, headers=req_headers, content=body,
            ) as resp:
                # Snapshot links are only inlined on uncompressed SSE bodies:
                # that is what the MCP transport actually uses, and rewriting
                # changes the length, so content-length has to go with it.
                rewriting = (
                    "text/event-stream" in resp.headers.get("content-type", "")
                    and not resp.headers.get("content-encoding")
                )
                out_headers = [
                    (k.encode(), v.encode())
                    for k, v in resp.headers.items()
                    if k.lower() not in _HOP_BY_HOP
                    and not (rewriting and k.lower() == "content-length")
                ]
                # initialize is the only response carrying a new session id.
                new_sid = resp.headers.get("mcp-session-id")
                if new_sid and scope["method"] == "POST":
                    _track_session(client, upstream_url, new_sid)

                await send({"type": "http.response.start",
                            "status": resp.status_code, "headers": out_headers})
                if rewriting:
                    buf = b""
                    async for chunk in resp.aiter_raw():
                        buf += chunk
                        # Flush event by event so SSE keeps streaming; only a
                        # partial trailing event is ever held back.
                        while b"\n\n" in buf:
                            event, buf = buf.split(b"\n\n", 1)
                            await send({"type": "http.response.body",
                                        "body": _rewrite_sse_event(event) + b"\n\n",
                                        "more_body": True})
                    if buf:
                        await send({"type": "http.response.body",
                                    "body": _rewrite_sse_event(buf), "more_body": True})
                else:
                    async for chunk in resp.aiter_raw():
                        await send({"type": "http.response.body",
                                    "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except httpx.HTTPError as exc:
            logger.warning("upstream proxy error: %s", exc)
            await send({"type": "http.response.start", "status": 502,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"playwright mcp upstream unreachable"}'})

    return proxy_app


def build_proxy_app(cfg: Config, store: Store, upstream_url: str) -> Starlette:
    provider = BridgeAuthProvider(cfg, store)
    verifier = ProviderTokenVerifier(provider)
    issuer = AnyHttpUrl(cfg.public_url)
    resource = AnyHttpUrl(cfg.mcp_endpoint)
    resource_metadata_url = build_resource_metadata_url(resource)

    # ---- consent screen: the operator gate in front of every authorization ----
    async def consent_form(request: Request):
        request_id = request.query_params.get("request_id", "")
        pending = provider.pending_request(request_id)
        if pending is None:
            return _render("Request expired",
                           "<h1>Request expired</h1><p>This approval link is no longer "
                           "valid. Start the connection again from Claude.</p>", status=400)
        body = f"""
          <h1>Authorize browser automation?</h1>
          <p>An MCP client wants to drive the Playwright browser on this machine.</p>
          <dl>
            <dt>Client</dt><dd>{html.escape(pending['client_name'])}</dd>
            <dt>Redirect</dt><dd>{html.escape(pending['redirect_uri'])}</dd>
          </dl>
          <div class="warn">Approving lets this client open pages, click, type and read
            content through a real browser. Only continue if you started this yourself
            and the redirect points at a Claude domain.</div>
          <form method="post" action="/consent">
            <input type="hidden" name="request_id" value="{html.escape(request_id)}">
            <label for="p">Operator passphrase</label>
            <input id="p" type="password" name="passphrase" autofocus autocomplete="off">
            <button type="submit">Approve access</button>
          </form>
        """
        return _render("Authorize Playwright MCP", body)

    async def consent_submit(request: Request):
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        passphrase = str(form.get("passphrase", ""))
        ok, outcome = provider.approve(request_id, passphrase)
        if ok:
            return RedirectResponse(outcome, status_code=302)
        return _render("Not approved",
                       f'<h1>Not approved</h1><p class="err">{html.escape(outcome)}</p>'
                       f'<form method="get" action="/consent">'
                       f'<input type="hidden" name="request_id" value="{html.escape(request_id)}">'
                       f'<button type="submit">Try again</button></form>', status=403)

    async def healthz(_request):
        return JSONResponse({"status": "ok", "service": "pw-mcp-proxy",
                             "upstream": upstream_url, "issuer": cfg.public_url})

    async def index(_request):
        return _render("Playwright MCP (protected)",
                       "<h1>Playwright MCP</h1><p>This is an OAuth-protected MCP endpoint. "
                       f"Add <code>{html.escape(cfg.mcp_endpoint)}</code> as a custom "
                       "connector in Claude.</p>")

    proxy_app = _make_proxy(upstream_url)

    routes = []
    routes += create_auth_routes(
        provider=provider,
        issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES,
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes.append(Route("/consent", consent_form, methods=["GET"]))
    routes.append(Route("/consent", consent_submit, methods=["POST"]))
    routes += create_protected_resource_routes(
        resource_url=resource, authorization_servers=[issuer], scopes_supported=SCOPES,
    )
    routes.append(Route(
        "/mcp",
        RequireAuthMiddleware(proxy_app, SCOPES, resource_metadata_url),
        methods=["GET", "POST", "DELETE"],
    ))
    routes.append(Route("/healthz", healthz, methods=["GET"]))
    routes.append(Route("/", index, methods=["GET"]))

    middleware = [
        Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier)),
        Middleware(AuthContextMiddleware),
    ]
    return Starlette(routes=routes, middleware=middleware,
                     lifespan=_lifespan)
