"""Command execution: process-group isolation, timeouts, output caps."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import time
from dataclasses import dataclass, field

from . import audit, registry, safety
from .config import Config

# Shown in chat when a sudo command is attempted but passwordless sudo is off.
# The whole point of the operator's chosen model: the password is entered in the
# panel, on the host, never pasted into this conversation.
SUDO_OFF_MESSAGE = (
    "Passwordless sudo is currently OFF, so this privileged command did not run.\n\n"
    "To enable it, the operator opens the MCP Bridge control panel, goes to\n"
    "'Privileged access', and turns on 'Passwordless sudo' (entering the account\n"
    "account password there -- it stays on the host and is never sent through this\n"
    "chat). Once it's on, re-run the command.\n\n"
    "Ask the operator to enable it in the panel. Do NOT ask them to paste their\n"
    "password here -- it is entered only in the panel."
)


async def _passwordless_sudo_ok() -> bool:
    """True if `sudo -n` runs without a password right now.

    Cheap: `-n` never prompts, so this returns immediately whether or not the
    NOPASSWD rule is present. Checked per call so a panel toggle takes effect at
    once, with no restart.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "true",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), timeout=5) == 0
    except (OSError, asyncio.TimeoutError):
        return False


@dataclass
class Result:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    cwd: str = ""
    used_sudo: bool = False
    blocked_reason: str = ""
    sudo_off: bool = False
    aborted: bool = False
    notes: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        """Render for the model: status header, then streams."""
        if self.sudo_off:
            return SUDO_OFF_MESSAGE
        if self.blocked_reason:
            return (
                f"BLOCKED by guardrails: {self.blocked_reason}\n"
                f"command: {self.command}\n\n"
                "If this is genuinely what you intend, the operator can disable guardrails\n"
                "by setting MCP_BRIDGE_GUARDRAILS=false in ~/.mcp-bridge/bridge.env."
            )
        head = [
            f"$ {'sudo ' if self.used_sudo else ''}{self.command}",
            f"exit={self.exit_code} duration={self.duration_ms}ms cwd={self.cwd}",
        ]
        if self.aborted:
            head.append("!! ABORTED from the control panel -- process group was killed")
        if self.timed_out:
            head.append("!! TIMED OUT -- process group was killed")
        if self.truncated:
            head.append("!! output truncated")
        head.extend(f"note: {n}" for n in self.notes)
        body = []
        if self.stdout.strip():
            body.append(f"--- stdout ---\n{self.stdout.rstrip()}")
        if self.stderr.strip():
            body.append(f"--- stderr ---\n{self.stderr.rstrip()}")
        if not body:
            body.append("(no output)")
        return "\n".join(head) + "\n\n" + "\n\n".join(body)


def _cap(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) <= limit:
        return data.decode("utf-8", "replace"), False
    half = limit // 2
    head = data[:half].decode("utf-8", "replace")
    tail = data[-half:].decode("utf-8", "replace")
    omitted = len(data) - limit
    return f"{head}\n\n... [{omitted} bytes omitted] ...\n\n{tail}", True


async def run(
    cfg: Config,
    command: str,
    *,
    sudo: bool = False,
    cwd: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    principal: str = "unknown",
) -> Result:
    timeout = min(timeout or cfg.default_timeout, cfg.max_timeout)
    workdir = cwd or cfg.default_cwd
    started = time.monotonic()

    if cfg.guardrails:
        verdict = safety.check_command(command)
        if not verdict.allowed:
            audit.record(
                "command.blocked", principal=principal, command=command,
                sudo=sudo, reason=verdict.reason,
            )
            return Result(command, None, "", "", 0, cwd=workdir,
                          used_sudo=sudo, blocked_reason=verdict.reason)

    if sudo and not cfg.allow_sudo:
        return Result(command, None, "", "", 0, cwd=workdir, used_sudo=True,
                      sudo_off=True)

    # Fail fast with an actionable message instead of running the command and
    # letting sudo reject it half-way -- no partial side effects, and the model
    # is told exactly how to get it enabled.
    if sudo and not await _passwordless_sudo_ok():
        return Result(command, None, "", "", 0, cwd=workdir, used_sudo=True,
                      sudo_off=True)

    if not os.path.isdir(workdir):
        return Result(command, None, "", f"cwd does not exist: {workdir}", 0, cwd=workdir)

    # sudo -n so a missing NOPASSWD rule fails fast instead of hanging on a prompt.
    payload = f"sudo -n {cfg.shell} -c {shlex.quote(command)}" if sudo else command

    child_env = {
        **os.environ,
        "DEBIAN_FRONTEND": "noninteractive",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "SYSTEMD_PAGER": "",
        **(env or {}),
    }

    audit.record("command.start", principal=principal, command=command,
                 sudo=sudo, cwd=workdir, timeout=timeout)

    proc = await asyncio.create_subprocess_exec(
        cfg.shell, "-c", payload,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=child_env,
        start_new_session=True,  # own process group, so timeout kills children too
    )

    entry = registry.register(
        command, proc, sudo=sudo, principal=principal, cwd=workdir, timeout=timeout
    )
    timed_out = False
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                await asyncio.sleep(2)
                if proc.returncode is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            except (asyncio.TimeoutError, ValueError):
                out, err = b"", b""
    finally:
        registry.unregister(entry.id)

    stdout, t1 = _cap(out or b"", cfg.max_output_bytes)
    stderr, t2 = _cap(err or b"", cfg.max_output_bytes)
    duration = int((time.monotonic() - started) * 1000)

    result = Result(
        command=command,
        exit_code=proc.returncode,
        stdout=safety.redact(stdout),
        stderr=safety.redact(stderr),
        duration_ms=duration,
        timed_out=timed_out,
        truncated=t1 or t2,
        cwd=workdir,
        used_sudo=sudo,
        aborted=entry.aborted,
    )
    if sudo and proc.returncode != 0 and (
        "password is required" in stderr or "interactive authentication is required" in stderr
    ):
        result.notes.append(
            "Passwordless sudo went off part-way through this command. Ask the "
            "operator to re-enable it in the control panel (Privileged access -> "
            "Passwordless sudo), then retry."
        )
    audit.record("command.end", principal=principal, command=command, sudo=sudo,
                 exit_code=proc.returncode, duration_ms=duration, timed_out=timed_out)
    return result
