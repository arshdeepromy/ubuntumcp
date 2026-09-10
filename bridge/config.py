"""Configuration for the MCP bridge.

Settings live in ~/.mcp-bridge/bridge.env and are edited either by hand or by
the control panel. Environment variables always win over the file.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import netinfo

STATE_DIR = Path(os.environ.get("MCP_BRIDGE_STATE", Path.home() / ".mcp-bridge"))
ENV_FILE = STATE_DIR / "bridge.env"

# Defaults the panel writes out for a fresh install.
DEFAULTS: dict[str, str] = {
    "MCP_BRIDGE_MODE": "lan",           # lan | tunnel
    "MCP_BRIDGE_BIND": "lan",           # lan | loopback | all | <explicit ip>
    "MCP_BRIDGE_PORT": "8901",
    "MCP_BRIDGE_AUTH_MODE": "token",    # token | oauth
    "MCP_BRIDGE_PUBLIC_URL": "",        # tunnel mode only
    "MCP_BRIDGE_ALLOW_SUDO": "true",
    "MCP_BRIDGE_GUARDRAILS": "true",
    "MCP_BRIDGE_TIMEOUT": "60",
    "MCP_BRIDGE_MAX_TIMEOUT": "900",
    "MCP_BRIDGE_MAX_OUTPUT": "200000",
    "MCP_BRIDGE_PANEL_PORT": "8900",
    "MCP_BRIDGE_PANEL_BIND": "lan",
}


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def write_env_file(values: dict[str, str], path: Path = ENV_FILE) -> None:
    """Rewrite the env file atomically, preserving unknown keys."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    merged = {**read_env_file(path), **values}
    lines = [
        "# MCP bridge configuration.",
        "# Managed by the control panel -- hand edits are preserved.",
        "",
    ]
    lines += [f"{k}={v}" for k, v in sorted(merged.items())]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def ensure_initialized() -> dict[str, str]:
    """Create the state dir and env file on first run, with fresh secrets."""
    STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    values = read_env_file()
    updates = {k: v for k, v in DEFAULTS.items() if k not in values}
    if "MCP_BRIDGE_PASSPHRASE" not in values:
        updates["MCP_BRIDGE_PASSPHRASE"] = secrets.token_urlsafe(18)
    if "MCP_BRIDGE_TOKEN" not in values:
        updates["MCP_BRIDGE_TOKEN"] = "mcpk_" + secrets.token_urlsafe(32)
    if updates:
        write_env_file(updates)
    return read_env_file()


def _load_into_environ() -> None:
    for key, value in read_env_file().items():
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    mode: str                 # 'lan' or 'tunnel'
    auth_mode: str            # 'token' or 'oauth'
    public_url: str           # OAuth issuer / resource identifier
    bind_host: str            # concrete address uvicorn listens on
    port: int
    admin_passphrase: str
    static_token: str
    shell: str = "/bin/bash"
    default_cwd: str = str(Path.home())
    default_timeout: int = 60
    max_timeout: int = 900
    max_output_bytes: int = 200_000
    guardrails: bool = True
    allow_sudo: bool = True
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 60 * 60 * 24 * 30
    auth_code_ttl: int = 300
    state_dir: Path = field(default_factory=lambda: STATE_DIR)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "bridge.db"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.log"

    @property
    def mcp_endpoint(self) -> str:
        return f"{self.public_url}/mcp"


def load_config() -> Config:
    ensure_initialized()
    _load_into_environ()

    mode = os.environ.get("MCP_BRIDGE_MODE", "lan").strip().lower()
    auth_mode = os.environ.get("MCP_BRIDGE_AUTH_MODE", "token").strip().lower()
    port = int(os.environ.get("MCP_BRIDGE_PORT", "8901"))
    bind_host = netinfo.resolve_bind(os.environ.get("MCP_BRIDGE_BIND", "lan"))

    if mode == "tunnel":
        public_url = os.environ.get("MCP_BRIDGE_PUBLIC_URL", "").rstrip("/")
        if not public_url:
            raise SystemExit(
                "Tunnel mode needs MCP_BRIDGE_PUBLIC_URL (the https:// origin of "
                "your tunnel). Set it in the control panel or in "
                f"{ENV_FILE}."
            )
        if not public_url.startswith("https://"):
            raise SystemExit(f"Tunnel mode requires an https:// URL (got {public_url})")
    else:
        # LAN mode: advertise the address other machines dial, not the bind
        # wildcard. Plain HTTP is intentional -- this never leaves the LAN.
        public_url = f"http://{netinfo.advertised_host(bind_host)}:{port}"
        if auth_mode == "oauth":
            # RFC 8414 requires an HTTPS issuer, and the SDK enforces it.
            raise SystemExit(
                "OAuth mode requires HTTPS, which a plain LAN address cannot "
                "provide. Use MCP_BRIDGE_AUTH_MODE=token on the LAN, or switch to "
                "tunnel mode (MCP_BRIDGE_MODE=tunnel) for OAuth."
            )

    passphrase = os.environ.get("MCP_BRIDGE_PASSPHRASE", "")
    token = os.environ.get("MCP_BRIDGE_TOKEN", "")
    if not passphrase or not token:
        raise SystemExit(f"Missing secrets in {ENV_FILE}; delete it to regenerate.")

    return Config(
        mode=mode,
        auth_mode=auth_mode,
        public_url=public_url,
        bind_host=bind_host,
        port=port,
        admin_passphrase=passphrase,
        static_token=token,
        shell=os.environ.get("MCP_BRIDGE_SHELL", "/bin/bash"),
        default_cwd=os.environ.get("MCP_BRIDGE_CWD", str(Path.home())),
        default_timeout=int(os.environ.get("MCP_BRIDGE_TIMEOUT", "60")),
        max_timeout=int(os.environ.get("MCP_BRIDGE_MAX_TIMEOUT", "900")),
        max_output_bytes=int(os.environ.get("MCP_BRIDGE_MAX_OUTPUT", "200000")),
        guardrails=_bool("MCP_BRIDGE_GUARDRAILS", True),
        allow_sudo=_bool("MCP_BRIDGE_ALLOW_SUDO", True),
        access_token_ttl=int(os.environ.get("MCP_BRIDGE_TOKEN_TTL", "3600")),
    )
