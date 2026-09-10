"""Append-only audit log. Every tool invocation lands here before it runs."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_path: Path | None = None
logger = logging.getLogger("mcp-bridge.audit")


def init(path: Path) -> None:
    global _path
    _path = path
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)


def record(event: str, **fields: Any) -> None:
    """Write one JSON line. Never raises -- auditing must not break a call."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    line = json.dumps(entry, default=str, ensure_ascii=False)
    logger.info("%s %s", event, line)
    if _path is None:
        return
    try:
        with _lock, _path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        logger.exception("failed to write audit entry")
