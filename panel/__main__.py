"""Entry point for the control panel: python -m panel"""

from __future__ import annotations

import logging
import sys

import uvicorn

from bridge import netinfo
from bridge.config import ensure_initialized, read_env_file

from .app import build_panel


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ensure_initialized()
    env = read_env_file()
    port = int(env.get("MCP_BRIDGE_PANEL_PORT", "8900"))
    bind = netinfo.resolve_bind(env.get("MCP_BRIDGE_PANEL_BIND", "lan"))
    shown = netinfo.advertised_host(bind)

    log = logging.getLogger("mcp-panel")
    log.info("control panel: http://%s:%d", shown, port)
    if shown != "127.0.0.1":
        log.info("reachable from any device on your LAN")

    uvicorn.run(build_panel(), host=bind, port=port,
                log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
