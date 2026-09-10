"""Control panel backend.

Guards a root-capable service, so the panel itself is authenticated: the
operator passphrase buys a signed, expiring session cookie. Brute force is
throttled per source address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import time
from collections import defaultdict
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from bridge import netinfo
from bridge.config import STATE_DIR, ensure_initialized, read_env_file, write_env_file

from . import supervisor
from .ui import LOGIN_PAGE, PANEL_PAGE

SESSION_COOKIE = "mcp_panel"
SESSION_TTL = 12 * 3600
SECRET_FILE = STATE_DIR / "panel.secret"

MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 300
_attempts: dict[str, list[float]] = defaultdict(list)


def _secret() -> bytes:
    """Cookie signing key, persisted so sessions survive a panel restart."""
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    key = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(key)
    SECRET_FILE.chmod(0o600)
    return key


def _sign(payload: str) -> str:
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return payload + "." + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def _verify(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    payload, _, sig = cookie.rpartition(".")
    expected = _sign(payload).rpartition(".")[2]
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return float(payload.split("|")[1]) > time.time()
    except (IndexError, ValueError):
        return False


def _new_session() -> str:
    return _sign(f"{secrets.token_urlsafe(12)}|{time.time() + SESSION_TTL}")


def _authed(request: Request) -> bool:
    return _verify(request.cookies.get(SESSION_COOKIE))


def _throttled(ip: str) -> bool:
    cutoff = time.time() - LOCKOUT_SECONDS
    _attempts[ip] = [t for t in _attempts[ip] if t > cutoff]
    return len(_attempts[ip]) >= MAX_ATTEMPTS


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

async def index(request: Request) -> Response:
    if not _authed(request):
        page = LOGIN_PAGE.replace("__HOSTNAME__", os.uname().nodename).replace("__ERROR__", "")
        return HTMLResponse(page)
    return HTMLResponse(PANEL_PAGE)


async def login(request: Request) -> Response:
    ip = _client_ip(request)
    if _throttled(ip):
        return JSONResponse(
            {"error": "Too many attempts. Try again in a few minutes."}, status_code=429
        )
    body = await request.json()
    supplied = str(body.get("passphrase", ""))
    expected = read_env_file().get("MCP_BRIDGE_PASSPHRASE", "")

    if not expected or not hmac.compare_digest(supplied, expected):
        _attempts[ip].append(time.time())
        remaining = MAX_ATTEMPTS - len(_attempts[ip])
        return JSONResponse(
            {"error": f"Incorrect passphrase. {max(remaining, 0)} attempts left."},
            status_code=401,
        )

    _attempts.pop(ip, None)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE, _new_session(),
        max_age=SESSION_TTL, httponly=True, samesite="lax",
    )
    return response


async def logout(request: Request) -> Response:
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


async def status(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    env = read_env_file()
    port = int(env.get("MCP_BRIDGE_PORT", "8901"))
    mode = env.get("MCP_BRIDGE_MODE", "lan")
    bind_setting = env.get("MCP_BRIDGE_BIND", "lan")

    if mode == "tunnel":
        base = env.get("MCP_BRIDGE_PUBLIC_URL", "").rstrip("/")
        endpoint = f"{base}/mcp" if base else "(set a public URL)"
    else:
        host = netinfo.advertised_host(netinfo.resolve_bind(bind_setting))
        endpoint = f"http://{host}:{port}/mcp"

    options = [{"value": "lan", "label": "Primary LAN address (recommended)"}]
    for addr in netinfo.candidate_addresses():
        options.append({
            "value": addr["address"],
            "label": f"{addr['address']} ({addr['interface']})",
        })
    options.append({"value": "all", "label": "All interfaces (0.0.0.0)"})
    options.append({"value": "loopback", "label": "This machine only (127.0.0.1)"})

    return JSONResponse({
        **supervisor.status(),
        "hostname": os.uname().nodename,
        "os": f"{platform.system()} {platform.release()}",
        "user": os.environ.get("USER") or Path.home().name,
        "endpoint": endpoint,
        "token": env.get("MCP_BRIDGE_TOKEN", ""),
        "active_tokens": supervisor.active_oauth_tokens(),
        "bind_options": options,
        "settings": {
            "mode": mode,
            "bind": bind_setting,
            "port": port,
            "auth_mode": env.get("MCP_BRIDGE_AUTH_MODE", "token"),
            "public_url": env.get("MCP_BRIDGE_PUBLIC_URL", ""),
            "allow_sudo": env.get("MCP_BRIDGE_ALLOW_SUDO", "true").lower() == "true",
            "guardrails": env.get("MCP_BRIDGE_GUARDRAILS", "true").lower() == "true",
            "timeout": int(env.get("MCP_BRIDGE_TIMEOUT", "60")),
        },
        "audit": _audit_entries(),
    })


def _audit_entries(limit: int = 40) -> list[dict]:
    out = []
    for line in supervisor.tail_audit(limit):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        entry["time"] = entry.get("ts", "")[11:19]
        out.append(entry)
    return out


async def control(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    action = request.path_params["action"]
    handler = {"start": supervisor.start, "stop": supervisor.stop,
               "restart": supervisor.restart}.get(action)
    if handler is None:
        return JSONResponse({"ok": False, "message": "Unknown action."}, status_code=400)
    ok, message = handler()
    return JSONResponse({"ok": ok, "message": message})


async def settings(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()

    port = int(body.get("port", 8901))
    if not 1 <= port <= 65535:
        return JSONResponse({"ok": False, "message": "Port must be 1–65535."})
    if port < 1024 and os.geteuid() != 0:
        return JSONResponse({"ok": False,
                             "message": "Ports below 1024 need root. Pick 1024 or higher."})

    panel_port = int(read_env_file().get("MCP_BRIDGE_PANEL_PORT", "8900"))
    if port == panel_port:
        return JSONResponse({"ok": False,
                             "message": f"Port {port} is used by this control panel."})

    mode = body.get("mode", "lan")
    auth_mode = str(body.get("auth_mode", "token"))
    public_url = str(body.get("public_url", "")).strip().rstrip("/")
    if mode == "tunnel":
        if not public_url:
            return JSONResponse({"ok": False,
                                 "message": "Tunnel mode needs a public HTTPS URL."})
        if not public_url.startswith("https://"):
            return JSONResponse({"ok": False,
                                 "message": "Public URL must start with https://"})

    # RFC 8414 requires an HTTPS issuer (loopback excepted), so OAuth cannot run
    # over a plain-HTTP LAN address. Catch it here rather than at startup.
    if mode == "lan" and auth_mode == "oauth":
        return JSONResponse({"ok": False, "message":
            "OAuth needs HTTPS, which a plain LAN address cannot provide. "
            "Use bearer-token auth for LAN, or switch to Tunnel mode for OAuth."})

    timeout = int(body.get("timeout", 60))
    write_env_file({
        "MCP_BRIDGE_MODE": mode,
        "MCP_BRIDGE_BIND": str(body.get("bind", "lan")),
        "MCP_BRIDGE_PORT": str(port),
        "MCP_BRIDGE_AUTH_MODE": auth_mode,
        "MCP_BRIDGE_PUBLIC_URL": public_url,
        "MCP_BRIDGE_ALLOW_SUDO": "true" if body.get("allow_sudo") else "false",
        "MCP_BRIDGE_GUARDRAILS": "true" if body.get("guardrails") else "false",
        "MCP_BRIDGE_TIMEOUT": str(max(5, min(timeout, 3600))),
    })

    if supervisor.is_running():
        ok, message = supervisor.restart()
        return JSONResponse({"ok": ok, "message": f"Settings saved. {message}"})
    return JSONResponse({"ok": True, "message": "Settings saved. Bridge is stopped."})


async def passphrase(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    new = str(body.get("passphrase", ""))
    if len(new) < 8:
        return JSONResponse({"ok": False, "message": "Must be at least 8 characters."})
    write_env_file({"MCP_BRIDGE_PASSPHRASE": new})
    message = "Passphrase changed."
    if supervisor.is_running():
        supervisor.restart()
        message += " Bridge restarted."
    return JSONResponse({"ok": True, "message": message})


async def rotate_token(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    write_env_file({"MCP_BRIDGE_TOKEN": "mcpk_" + secrets.token_urlsafe(32)})
    message = "Bearer token rotated."
    if supervisor.is_running():
        supervisor.restart()
        message += " Bridge restarted; reconfigure your clients."
    return JSONResponse({"ok": True, "message": message})


async def revoke(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    count = supervisor.revoke_all_tokens()
    return JSONResponse({"ok": True, "message": f"Revoked {count} session(s)."})


async def live(request: Request) -> Response:
    """Connection counts and in-flight commands, straight from the bridge."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not supervisor.is_running():
        return JSONResponse({"running": False, "running_commands": [],
                             "sessions": 0, "registered_clients": 0,
                             "active_tokens": 0})
    data = supervisor.admin_request("GET", "/admin/status")
    data["running"] = True
    return JSONResponse(data)


async def abort(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json() if await request.body() else {}
    result = supervisor.admin_request("POST", "/admin/abort",
                                      {"id": body.get("id")} if body.get("id") else {})
    if result.get("error"):
        return JSONResponse({"ok": False, "message": result["error"]})
    return JSONResponse(result)


async def reset_sessions(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    result = supervisor.admin_request("POST", "/admin/reset")
    if result.get("error"):
        return JSONResponse({"ok": False, "message": result["error"]})
    return JSONResponse(result)


async def sudo_status(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "rule_installed": supervisor.sudoers_installed(),
        "works": supervisor.passwordless_sudo_works(),
    })


async def sudo_install(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    password = str(body.get("password", ""))
    if not password:
        return JSONResponse({"ok": False, "message": "Account password required."})
    if body.get("remove"):
        ok, message = supervisor.remove_sudoers(password)
    else:
        ok, message = supervisor.install_sudoers(password)
    # Deliberately not audited: the audit log must never contain a password, and
    # the action itself is visible in /etc/sudoers.d.
    return JSONResponse({"ok": ok, "message": message})


async def service_log(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"log": supervisor.tail_log(80)})


def build_panel() -> Starlette:
    ensure_initialized()
    # Starlette matches in order, so every literal path must precede the
    # /api/{action} catch-all or it gets swallowed by it.
    return Starlette(routes=[
        Route("/", index),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/status", status),
        Route("/api/settings", settings, methods=["POST"]),
        Route("/api/passphrase", passphrase, methods=["POST"]),
        Route("/api/token/rotate", rotate_token, methods=["POST"]),
        Route("/api/revoke", revoke, methods=["POST"]),
        Route("/api/log", service_log),
        Route("/api/live", live),
        Route("/api/abort", abort, methods=["POST"]),
        Route("/api/reset-sessions", reset_sessions, methods=["POST"]),
        Route("/api/sudo/status", sudo_status),
        Route("/api/sudo/install", sudo_install, methods=["POST"]),
        Route("/api/{action:str}", control, methods=["POST"]),
    ])
