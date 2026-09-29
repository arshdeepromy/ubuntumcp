"""Proxy configuration.

Settings live in ~/.mcp-bridge/pwproxy.env; the operator passphrase is shared
with the bridge (read from bridge.env), so consent uses the same secret the
operator already knows. Environment variables win over the files.
"""

from __future__ import annotations

import os
from pathlib import Path

from bridge.config import STATE_DIR, read_env_file
from bridge.config import Config

PWPROXY_ENV = STATE_DIR / "pwproxy.env"
DB_PATH = STATE_DIR / "pwproxy.db"

DEFAULTS = {
    "PWPROXY_UPSTREAM": "http://127.0.0.1:8931/mcp",
    "PWPROXY_PORT": "8932",
    "PWPROXY_BIND": "127.0.0.1",
    "PWPROXY_PUBLIC_URL": "",  # https origin of the tunnel hostname, e.g. https://pw.example.com
}


def _values() -> dict[str, str]:
    fromfile = read_env_file(PWPROXY_ENV)
    return {k: (os.environ.get(k) or fromfile.get(k) or d) for k, d in DEFAULTS.items()}


class ProxyConfig:
    def __init__(self) -> None:
        v = _values()
        self.public_url = (v["PWPROXY_PUBLIC_URL"] or "").rstrip("/")
        self.upstream = v["PWPROXY_UPSTREAM"]
        self.port = int(v["PWPROXY_PORT"])
        self.bind = v["PWPROXY_BIND"]
        self.passphrase = read_env_file().get("MCP_BRIDGE_PASSPHRASE", "")

    def validate(self) -> None:
        if not self.public_url:
            raise SystemExit(
                f"Set PWPROXY_PUBLIC_URL (the https:// origin of your Cloudflare "
                f"hostname) in {PWPROXY_ENV}."
            )
        if not self.public_url.startswith("https://"):
            raise SystemExit(
                f"PWPROXY_PUBLIC_URL must be an https:// URL (got {self.public_url!r}). "
                "OAuth issuers require HTTPS."
            )
        if not self.passphrase:
            raise SystemExit(
                "MCP_BRIDGE_PASSPHRASE is missing from bridge.env; the consent "
                "screen needs it. Start the bridge panel once to generate it."
            )

    def bridge_config(self) -> Config:
        """A bridge Config carrying just what BridgeAuthProvider and the consent
        screen read: the issuer/public URL and the operator passphrase."""
        return Config(
            mode="tunnel",
            auth_mode="oauth",
            public_url=self.public_url,
            bind_host=self.bind,
            port=self.port,
            admin_passphrase=self.passphrase,
            static_token="",
        )
