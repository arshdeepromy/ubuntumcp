"""Tool surface exposed to Claude.

`run_command` is the general escape hatch. The rest are structured wrappers for
the diagnostics you reach for constantly -- they cost fewer tokens than parsing
raw shell output and they cannot be typo'd into something destructive.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from mcp.server.mcpserver import Context, MCPServer

from . import audit, safety
from .config import Config
from .exec import run


def _principal(ctx: Any) -> str:
    """Best-effort identity of the caller, for the audit log.

    The SDK hands back a JSON-encoded (client, issuer, subject) triple. Collapse
    it to something a human can scan in a log file.
    """
    try:
        from mcp.server.mcpserver import authenticated_principal

        request_ctx = getattr(ctx, "request_context", None)
        if request_ctx is None:
            return "unknown"
        raw = authenticated_principal(request_ctx)
        if not raw:
            return "unauthenticated"
        try:
            client, _issuer, subject = json.loads(raw)
            return f"client:{(subject or client or '?')[:8]}"
        except (ValueError, TypeError):
            return str(raw)[:40]
    except Exception:  # noqa: BLE001 - auditing must never break a tool call
        return "unknown"


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def register(server: MCPServer, cfg: Config) -> None:
    @server.tool(
        title="Run shell command",
        description=(
            "Run a shell command on the Linux host and return stdout, stderr and "
            f"exit code. Runs as user '{getpass.getuser()}'. Set sudo=true for privileged commands "
            "(apt, systemctl restart, editing /etc). Non-interactive: commands that "
            "wait for input will hit the timeout, so pass flags like -y or --no-pager. "
            "Prefer the structured tools (system_overview, read_journal, "
            "service_status) when they cover what you need."
        ),
    )
    async def run_command(
        command: str,
        ctx: Context,
        sudo: bool = False,
        cwd: str | None = None,
        timeout: int = 60,
    ) -> str:
        result = await run(
            cfg, command, sudo=sudo, cwd=cwd, timeout=timeout,
            principal=_principal(ctx),
        )
        return result.to_text()

    @server.tool(
        title="System overview",
        description=(
            "Snapshot of host health: uptime, load, CPU, memory, swap, disk usage per "
            "mount, and any failed systemd units. Start here when diagnosing "
            "'the machine is slow' or 'something is broken'."
        ),
    )
    async def system_overview(ctx: Context) -> str:
        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        load1, load5, load15 = os.getloadavg()
        cores = psutil.cpu_count() or 1

        lines = [
            f"host: {os.uname().nodename}   kernel: {os.uname().release}",
            f"uptime: {uptime.days}d {uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m "
            f"(booted {boot:%Y-%m-%d %H:%M})",
            f"load: {load1:.2f} {load5:.2f} {load15:.2f}  over {cores} cores"
            f"{'   <-- SATURATED' if load1 > cores else ''}",
            f"cpu: {psutil.cpu_percent(interval=0.4):.1f}% busy",
            f"memory: {_human_bytes(vm.used)} / {_human_bytes(vm.total)} "
            f"({vm.percent:.1f}%), available {_human_bytes(vm.available)}",
            f"swap: {_human_bytes(sm.used)} / {_human_bytes(sm.total)} ({sm.percent:.1f}%)",
            "",
            "disks:",
        ]
        for part in psutil.disk_partitions(all=False):
            # snap loop-mounts are read-only squashfs, always "100% full" and never
            # actionable -- listing them buries the real filesystems.
            if part.fstype in {"squashfs", "overlay"} or part.device.startswith("/dev/loop"):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            flag = "  <-- LOW SPACE" if usage.percent > 85 else ""
            lines.append(
                f"  {part.mountpoint:<24} {_human_bytes(usage.used)}/{_human_bytes(usage.total)} "
                f"({usage.percent:.0f}%) {part.fstype}{flag}"
            )

        failed = await run(cfg, "systemctl --failed --no-legend --no-pager",
                           timeout=15, principal=_principal(ctx))
        body = failed.stdout.strip()
        lines += ["", f"failed units:\n{body if body else '  none'}"]
        audit.record("tool.system_overview", principal=_principal(ctx))
        return "\n".join(lines)

    @server.tool(
        title="Read service logs",
        description=(
            "Read journalctl logs. Pass a unit name to scope to one service, or omit "
            "it for the whole journal. `since` accepts systemd time syntax such as "
            "'10 min ago', 'today', '2026-08-11 09:00'. Set priority to 'err' to see "
            "only errors and worse."
        ),
    )
    async def read_journal(
        ctx: Context,
        unit: str | None = None,
        lines: int = 100,
        since: str | None = None,
        priority: str | None = None,
        grep: str | None = None,
    ) -> str:
        cmd = ["journalctl", "--no-pager", "-n", str(max(1, min(lines, 5000)))]
        if unit:
            cmd += ["-u", shlex.quote(unit)]
        if since:
            cmd += ["--since", shlex.quote(since)]
        if priority:
            cmd += ["-p", shlex.quote(priority)]
        if grep:
            cmd += ["--grep", shlex.quote(grep)]
        # sudo widens visibility to other users' and kernel messages.
        result = await run(cfg, " ".join(cmd), sudo=True, timeout=45,
                           principal=_principal(ctx))
        return result.to_text()

    @server.tool(
        title="Service status",
        description=(
            "Detailed systemd status for a unit: active state, PID, memory, recent "
            "log tail. Use service_control to start/stop/restart it."
        ),
    )
    async def service_status(name: str, ctx: Context) -> str:
        result = await run(
            cfg, f"systemctl status {shlex.quote(name)} --no-pager -l -n 30",
            timeout=20, principal=_principal(ctx),
        )
        return result.to_text()

    @server.tool(
        title="Control a service",
        description=(
            "start / stop / restart / reload / enable / disable a systemd unit "
            "(runs under sudo). Refuses to touch the bridge's own services, since "
            "that would sever this connection."
        ),
    )
    async def service_control(name: str, action: str, ctx: Context) -> str:
        allowed = {"start", "stop", "restart", "reload", "enable", "disable", "status"}
        if action not in allowed:
            return f"action must be one of: {', '.join(sorted(allowed))}"
        if any(p in name for p in ("mcp-bridge", "cloudflared")) and action != "status":
            return (
                f"Refusing to {action} {name}: that is this bridge's own plumbing and "
                "would drop your connection. Do it from a local terminal instead."
            )
        result = await run(
            cfg, f"systemctl {action} {shlex.quote(name)} --no-pager",
            sudo=True, timeout=60, principal=_principal(ctx),
        )
        return result.to_text()

    @server.tool(
        title="Read a file",
        description=(
            "Read a text file from disk. Use sudo=true for root-owned paths. "
            "Refuses well-known secret files (SSH private keys, .aws/credentials, "
            "/etc/shadow) -- read those from a local terminal if you truly need them."
        ),
    )
    async def read_file(
        path: str,
        ctx: Context,
        max_bytes: int = 100_000,
        sudo: bool = False,
        tail: int | None = None,
    ) -> str:
        expanded = os.path.expanduser(path)
        if safety.SENSITIVE_PATH_RE.search(expanded):
            audit.record("tool.read_file.blocked", principal=_principal(ctx), path=expanded)
            return f"Refusing to read {expanded}: matches the sensitive-file denylist."
        if tail:
            cmd = f"tail -n {int(tail)} {shlex.quote(expanded)}"
        else:
            cmd = f"head -c {int(max_bytes)} {shlex.quote(expanded)}"
        result = await run(cfg, cmd, sudo=sudo, timeout=30, principal=_principal(ctx))
        if result.exit_code != 0:
            return result.to_text()
        return f"--- {expanded} ---\n{result.stdout}"

    @server.tool(
        title="Write a file",
        description=(
            "Write text to a file, creating parent directories as needed. Use "
            "sudo=true for root-owned locations. Takes a .bak copy of any existing "
            "file first."
        ),
    )
    async def write_file(
        path: str, content: str, ctx: Context, sudo: bool = False
    ) -> str:
        expanded = os.path.expanduser(path)
        principal = _principal(ctx)
        audit.record("tool.write_file", principal=principal, path=expanded,
                     bytes=len(content), sudo=sudo)
        # Heredoc with a quoted delimiter: no expansion of the payload.
        script = (
            f"mkdir -p {shlex.quote(str(Path(expanded).parent))} && "
            f"{{ [ -f {shlex.quote(expanded)} ] && cp -a {shlex.quote(expanded)} "
            f"{shlex.quote(expanded + '.bak')}; }}; "
            f"cat > {shlex.quote(expanded)} <<'MCP_BRIDGE_EOF'\n{content}\nMCP_BRIDGE_EOF"
        )
        result = await run(cfg, script, sudo=sudo, timeout=30, principal=principal)
        if result.exit_code == 0:
            return f"Wrote {len(content)} bytes to {expanded} (backup at {expanded}.bak if it existed)."
        return result.to_text()

    @server.tool(
        title="List directory",
        description="List a directory with sizes, permissions and modification times.",
    )
    async def list_directory(path: str, ctx: Context, all_files: bool = False) -> str:
        flags = "-lah" if all_files else "-lh"
        result = await run(
            cfg, f"ls {flags} --time-style=long-iso {shlex.quote(os.path.expanduser(path))}",
            timeout=20, principal=_principal(ctx),
        )
        return result.to_text()

    @server.tool(
        title="Top processes",
        description=(
            "Processes ranked by CPU or memory. Use when something is eating the "
            "machine and you need the PID."
        ),
    )
    async def top_processes(ctx: Context, sort_by: str = "cpu", limit: int = 15) -> str:
        key = "cpu_percent" if sort_by == "cpu" else "memory_percent"

        # psutil reports CPU as a delta since the previous call on that process,
        # so the first read is always 0.0. Prime, wait, then measure.
        tracked = {}
        for p in psutil.process_iter():
            try:
                p.cpu_percent(None)
                tracked[p.pid] = p
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        await asyncio.sleep(0.5)

        procs = []
        for p in tracked.values():
            try:
                with p.oneshot():
                    procs.append({
                        "pid": p.pid,
                        "name": p.name(),
                        "username": p.username(),
                        "cpu_percent": p.cpu_percent(None),
                        "memory_percent": p.memory_percent(),
                        "cmdline": p.cmdline(),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        procs.sort(key=lambda i: i.get(key) or 0, reverse=True)

        out = [f"{'PID':>7}  {'USER':<12} {'CPU%':>6} {'MEM%':>6}  COMMAND"]
        for info in procs[: max(1, min(limit, 100))]:
            cmd = " ".join(info.get("cmdline") or [info.get("name") or "?"])[:90]
            out.append(
                f"{info['pid']:>7}  {(info.get('username') or '?')[:12]:<12} "
                f"{info.get('cpu_percent') or 0:>6.1f} {info.get('memory_percent') or 0:>6.1f}  {cmd}"
            )
        audit.record("tool.top_processes", principal=_principal(ctx), sort_by=sort_by)
        return "\n".join(out)

    @server.tool(
        title="Network overview",
        description=(
            "Interfaces and addresses, listening TCP/UDP sockets with owning "
            "processes, default routes, and DNS configuration."
        ),
    )
    async def network_overview(ctx: Context) -> str:
        principal = _principal(ctx)
        sections = []
        for label, cmd, use_sudo in (
            ("addresses", "ip -brief address", False),
            ("routes", "ip route", False),
            ("listening sockets", "ss -tulpn", True),
            ("dns", "resolvectl status --no-pager | head -40", False),
        ):
            result = await run(cfg, cmd, sudo=use_sudo, timeout=25, principal=principal)
            body = (result.stdout or result.stderr).rstrip() or "(no output)"
            sections.append(f"=== {label} ===\n{body}")
        return "\n\n".join(sections)

    @server.tool(
        title="Disk usage breakdown",
        description=(
            "Find what is consuming space: per-filesystem usage plus the largest "
            "subdirectories under the given path."
        ),
    )
    async def disk_usage(ctx: Context, path: str = "/", depth: int = 1, top: int = 20) -> str:
        principal = _principal(ctx)
        target = shlex.quote(os.path.expanduser(path))
        df = await run(cfg, "df -hT -x tmpfs -x devtmpfs", timeout=20, principal=principal)
        du = await run(
            cfg,
            f"du -h --max-depth={int(depth)} -x {target} 2>/dev/null | sort -rh | head -{int(top)}",
            sudo=True, timeout=180, principal=principal,
        )
        return (
            f"=== filesystems ===\n{df.stdout.rstrip()}\n\n"
            f"=== largest under {path} (depth {depth}) ===\n"
            f"{du.stdout.rstrip() or '(no output)'}"
        )
