"""Entry point: python -m bridge"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .config import load_config
from .server import build_app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = load_config()
    app = build_app(cfg)
    log = logging.getLogger("mcp-bridge")
    log.info("listening on %s:%d", cfg.bind_host, cfg.port)
    log.info("guardrails=%s sudo=%s", cfg.guardrails, cfg.allow_sudo)
    uvicorn.run(app, host=cfg.bind_host, port=cfg.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
