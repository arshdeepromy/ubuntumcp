"""Registry of in-flight commands, so a wedged one can be killed out of band.

The failure this exists for: a tool call blocks (waiting on stdin, or a long
timeout), the client can no longer get a response, and the only recovery is
abandoning the session. With this, the panel can see what is stuck and kill it
without touching the MCP session at all.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_lock = threading.Lock()
_running: dict[str, "RunningCommand"] = {}
_counter = 0


@dataclass
class RunningCommand:
    id: str
    command: str
    sudo: bool
    principal: str
    cwd: str
    timeout: int
    started: float
    proc: Any                       # asyncio.subprocess.Process
    aborted: bool = False
    abort_reason: str = ""
    tags: dict = field(default_factory=dict)

    @property
    def pid(self) -> int | None:
        return getattr(self.proc, "pid", None)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "sudo": self.sudo,
            "principal": self.principal,
            "cwd": self.cwd,
            "pid": self.pid,
            "elapsed_seconds": round(self.elapsed, 1),
            "timeout": self.timeout,
            "aborted": self.aborted,
        }


def register(
    command: str, proc: Any, *, sudo: bool, principal: str, cwd: str, timeout: int
) -> RunningCommand:
    global _counter
    with _lock:
        _counter += 1
        entry = RunningCommand(
            id=f"cmd{_counter}", command=command, sudo=sudo, principal=principal,
            cwd=cwd, timeout=timeout, started=time.time(), proc=proc,
        )
        _running[entry.id] = entry
        return entry


def unregister(entry_id: str) -> None:
    with _lock:
        _running.pop(entry_id, None)


def snapshot() -> list[dict]:
    with _lock:
        return [e.to_dict() for e in _running.values()]


def count() -> int:
    with _lock:
        return len(_running)


def _kill(entry: RunningCommand, reason: str) -> bool:
    """SIGTERM the process group, then SIGKILL. Children die with it."""
    pid = entry.pid
    if pid is None:
        return False
    entry.aborted = True
    entry.abort_reason = reason
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            entry.proc.terminate()
        except Exception:  # noqa: BLE001 - process may already be gone
            return False

    async def _hard_kill() -> None:
        await asyncio.sleep(3)
        try:
            if entry.proc.returncode is None:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        asyncio.get_running_loop().create_task(_hard_kill())
    except RuntimeError:
        pass  # no loop here; the SIGTERM still stands
    return True


def abort(entry_id: str, reason: str = "aborted from control panel") -> bool:
    with _lock:
        entry = _running.get(entry_id)
    return _kill(entry, reason) if entry else False


def abort_all(reason: str = "aborted from control panel") -> int:
    with _lock:
        entries = list(_running.values())
    return sum(1 for e in entries if _kill(e, reason))
