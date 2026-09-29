"""Entry point: python -m pwproxy"""

from __future__ import annotations

import logging
import sys

import uvicorn

from bridge.auth import Store

from .app import build_proxy_app
from .config import DB_PATH, ProxyConfig


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("pw-mcp-proxy")

    proxy_cfg = ProxyConfig()
    proxy_cfg.validate()
    cfg = proxy_cfg.bridge_config()

    store = Store(DB_PATH)
    store.sweep()

    app = build_proxy_app(cfg, store, proxy_cfg.upstream)
    log.info("issuer   : %s", cfg.public_url)
    log.info("resource : %s", cfg.mcp_endpoint)
    log.info("upstream : %s", proxy_cfg.upstream)
    log.info("listening: http://%s:%d", proxy_cfg.bind, proxy_cfg.port)

    uvicorn.run(app, host=proxy_cfg.bind, port=proxy_cfg.port,
                log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
