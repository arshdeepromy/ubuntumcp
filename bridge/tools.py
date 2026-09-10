"""Tool surface exposed to Claude.

`run_command` is the general escape hatch. The rest are structured wrappers for
the diagnostics you reach for constantly -- they cost fewer tokens than parsing
raw shell output and they cannot be typo'd into something destructive.
"""

from __future__ import annotations

import asyncio
import getpass
import contextlib
import json
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from mcp.server.mcpserver import Context, Image, MCPServer

from . import audit, browser, safety
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
            "Run a shell command on the Ubuntu host and return stdout, stderr and "
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

    _register_browser(server, cfg)


# ---- browser automation ------------------------------------------------- #
# Proxied to the Playwright MCP server; see browser.py for why it is a proxy and
# why one connection is held open. The `target` argument everywhere is a `ref=`
# handle out of a snapshot (preferred -- it is unambiguous) or a CSS/text
# selector. Take a snapshot first; the refs change whenever the page does.

_SNAPSHOT_HINT = (
    "Targets come from browser_snapshot: pass the bare ref (e.g. 'e7') or a CSS "
    "selector. Refs are invalidated by navigation and re-renders, so snapshot "
    "again after anything that changes the page."
)


def _register_browser(server: MCPServer, cfg: Config) -> None:

    @server.tool(
        title="Navigate the browser",
        description=(
            "Point the headless Chromium at a URL and return the page's "
            "accessibility snapshot. This is the entry point for browser work: the "
            "returned tree carries the `ref=` handles every other browser tool "
            "takes as `target`. Set back=true to go back in history instead."
        ),
    )
    async def browser_navigate(ctx: Context, url: str = "", back: bool = False) -> str:
        if back:
            return await browser.session.call_text("browser_navigate_back", {})
        if not url:
            return "Pass a url, or back=true to go back in history."
        audit.record("tool.browser_navigate", principal=_principal(ctx), url=url)
        return await browser.session.call_text("browser_navigate", {"url": url})

    @server.tool(
        title="Snapshot the page",
        description=(
            "The accessibility tree of the current page: the structured view of "
            "what is on screen, with a `ref=` handle for each element. Cheaper and "
            "far more reliable to act on than a screenshot. Narrow a large page "
            "with target (subtree root) or depth."
        ),
    )
    async def browser_snapshot(
        target: str | None = None, depth: int | None = None, boxes: bool = False
    ) -> str:
        return await browser.session.call_text(
            "browser_snapshot", {"target": target, "depth": depth,
                                 "boxes": boxes or None}
        )

    @server.tool(
        title="Find on page",
        description=(
            "Search the current snapshot for text or a regex and return the "
            "matching elements with their refs. Use it to locate a control on a "
            "long page without paging through the whole tree."
        ),
    )
    async def browser_find(text: str | None = None, regex: str | None = None) -> str:
        if not text and not regex:
            return "Pass either text or regex."
        return await browser.session.call_text(
            "browser_find", {"text": text, "regex": regex}
        )

    @server.tool(
        title="Click an element",
        description="Click an element on the page. " + _SNAPSHOT_HINT,
    )
    async def browser_click(
        target: str,
        element: str | None = None,
        double_click: bool = False,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> str:
        return await browser.session.call_text("browser_click", {
            "target": target, "element": element,
            "doubleClick": double_click or None,
            "button": button if button != "left" else None,
            "modifiers": modifiers,
        })

    @server.tool(
        title="Type into an element",
        description=(
            "Type text into an input. submit=true presses Enter afterwards. "
            "slowly=true sends one character at a time, which is what you want "
            "when the page has per-keystroke handlers (autocomplete, validation). "
            + _SNAPSHOT_HINT
        ),
    )
    async def browser_type(
        target: str,
        text: str,
        element: str | None = None,
        submit: bool = False,
        slowly: bool = False,
    ) -> str:
        return await browser.session.call_text("browser_type", {
            "target": target, "text": text, "element": element,
            "submit": submit or None, "slowly": slowly or None,
        })

    @server.tool(
        title="Fill a form",
        description=(
            "Fill several fields in one call -- much faster than a browser_type "
            "per field. Each field is an object with keys: target (ref), name "
            "(human label), type (textbox|checkbox|radio|combobox|slider), and "
            "value (for a checkbox, 'true'/'false'; for a combobox, the option "
            "text)."
        ),
    )
    async def browser_fill_form(fields: list[dict[str, str]]) -> str:
        return await browser.session.call_text("browser_fill_form", {"fields": fields})

    @server.tool(
        title="Press a key",
        description=(
            "Press a key on the focused element, e.g. 'Enter', 'Escape', "
            "'ArrowDown', 'Tab', or a single character."
        ),
    )
    async def browser_press_key(key: str) -> str:
        return await browser.session.call_text("browser_press_key", {"key": key})

    @server.tool(
        title="Select a dropdown option",
        description="Choose one or more options in a <select>. " + _SNAPSHOT_HINT,
    )
    async def browser_select_option(
        target: str, values: list[str], element: str | None = None
    ) -> str:
        return await browser.session.call_text("browser_select_option", {
            "target": target, "values": values, "element": element,
        })

    @server.tool(
        title="Hover over an element",
        description="Move the pointer over an element, to trigger menus and "
                    "tooltips. " + _SNAPSHOT_HINT,
    )
    async def browser_hover(target: str, element: str | None = None) -> str:
        return await browser.session.call_text(
            "browser_hover", {"target": target, "element": element}
        )

    @server.tool(
        title="Drag and drop",
        description="Drag one element onto another. " + _SNAPSHOT_HINT,
    )
    async def browser_drag(
        start_target: str,
        end_target: str,
        start_element: str | None = None,
        end_element: str | None = None,
    ) -> str:
        return await browser.session.call_text("browser_drag", {
            "startTarget": start_target, "endTarget": end_target,
            "startElement": start_element, "endElement": end_element,
        })

    @server.tool(
        title="Upload files",
        description=(
            "Supply absolute paths to a file chooser. Call this after the click "
            "that opens the chooser; with no paths, the chooser is dismissed."
        ),
    )
    async def browser_file_upload(paths: list[str] | None = None) -> str:
        return await browser.session.call_text("browser_file_upload", {"paths": paths})

    @server.tool(
        title="Handle a dialog",
        description=(
            "Accept or dismiss a pending alert/confirm/prompt. A dialog blocks "
            "every other browser call until it is answered, so reach for this if "
            "actions start timing out right after a click."
        ),
    )
    async def browser_handle_dialog(accept: bool, prompt_text: str | None = None) -> str:
        return await browser.session.call_text(
            "browser_handle_dialog", {"accept": accept, "promptText": prompt_text}
        )

    @server.tool(
        title="Wait for the page",
        description=(
            "Wait for text to appear, for text to disappear, or for a fixed "
            "number of seconds. Prefer waiting on text over waiting on time -- it "
            "is both faster and less flaky."
        ),
    )
    async def browser_wait_for(
        text: str | None = None,
        text_gone: str | None = None,
        time: float | None = None,
    ) -> str:
        if text is None and text_gone is None and time is None:
            return "Pass one of: text, text_gone, time."
        return await browser.session.call_text(
            "browser_wait_for", {"text": text, "textGone": text_gone, "time": time},
            timeout=(time or 0) + 120,
        )

    @server.tool(
        title="Evaluate JavaScript",
        description=(
            "Run a JS function in the page and return its result -- the direct way "
            "to assert on state the accessibility tree does not expose. Pass "
            "'() => ...'; with a target, pass '(element) => ...' and it receives "
            "that element."
        ),
    )
    async def browser_evaluate(
        function: str, target: str | None = None, element: str | None = None
    ) -> str:
        return await browser.session.call_text("browser_evaluate", {
            "function": function, "target": target, "element": element,
        })

    @server.tool(
        title="Screenshot the page",
        description=(
            "A PNG of the current page, returned as an image. Use it to check "
            "visual rendering; for finding and clicking things, browser_snapshot "
            "is better and much cheaper. full_page captures the whole scrollable "
            "page, target captures one element."
        ),
        # Returns an image or, on failure, text. Pydantic cannot build an output
        # schema for that union, and there is nothing structured to describe.
        structured_output=False,
    )
    async def browser_screenshot(
        target: str | None = None,
        element: str | None = None,
        full_page: bool = False,
        image_type: str = "png",
    ):
        result = await browser.session.call("browser_take_screenshot", {
            "target": target, "element": element,
            "fullPage": full_page or None, "type": image_type, "scale": "css",
        })
        if getattr(result, "is_error", False):
            return browser.render(result)
        found = browser.images(result)
        if not found:
            return browser.render(result)
        data, mime = found[0]
        return Image(data=data, format=mime.split("/")[-1])

    @server.tool(
        title="Console messages",
        description=(
            "Console output from the page. Start here when a page 'looks broken' "
            "-- an uncaught exception shows up here and nowhere else. Defaults to "
            "errors only since the last navigation; widen with level or all=true."
        ),
    )
    async def browser_console_messages(level: str = "error", all: bool = False) -> str:
        return await browser.session.call_text(
            "browser_console_messages", {"level": level, "all": all or None}
        )

    @server.tool(
        title="Network requests",
        description=(
            "Requests the page made, with status codes. Use filter (a regex on the "
            "URL) to cut it down to the calls you care about, e.g. '/api/.*'. "
            "Static assets are excluded unless static=true. Follow up with "
            "browser_network_request for one request's headers or body."
        ),
    )
    async def browser_network_requests(
        filter: str | None = None, static: bool = False
    ) -> str:
        return await browser.session.call_text(
            "browser_network_requests", {"filter": filter, "static": static}
        )

    @server.tool(
        title="Inspect one network request",
        description=(
            "Full detail for a single request by its 1-based index from "
            "browser_network_requests. part narrows it to one of: request-headers, "
            "request-body, response-headers, response-body."
        ),
    )
    async def browser_network_request(index: int, part: str | None = None) -> str:
        return await browser.session.call_text(
            "browser_network_request", {"index": index, "part": part}
        )

    @server.tool(
        title="Manage tabs",
        description="List, open, close or switch browser tabs. action is one of: "
                    "list, new, close, select.",
    )
    async def browser_tabs(
        action: str, index: int | None = None, url: str | None = None
    ) -> str:
        return await browser.session.call_text(
            "browser_tabs", {"action": action, "index": index, "url": url}
        )

    @server.tool(
        title="Resize the viewport",
        description="Set the viewport size in CSS pixels -- the way to exercise "
                    "responsive breakpoints. Starts at 1280x800.",
    )
    async def browser_resize(width: int, height: int) -> str:
        return await browser.session.call_text(
            "browser_resize", {"width": width, "height": height}
        )

    @server.tool(
        title="Run Playwright code",
        description=(
            "Run raw Playwright JavaScript against the live page for anything the "
            "other tools do not cover -- multi-step flows, custom waits, "
            "locator APIs. The code is a function taking `page`, e.g. "
            "'async (page) => { await page.getByRole(\"button\").click(); "
            "return await page.title(); }'."
        ),
    )
    async def browser_run_code(code: str, ctx: Context) -> str:
        audit.record("tool.browser_run_code", principal=_principal(ctx),
                     bytes=len(code))
        return await browser.session.call_text("browser_run_code_unsafe", {"code": code})

    @server.tool(
        title="Browser state",
        description=(
            "Diagnostics for the browser connection: whether the bridge is "
            "attached to playwright-mcp, how many calls it has served, and the "
            "current tabs. Use it when browser calls start failing."
        ),
    )
    async def browser_state() -> str:
        lines = [f"bridge -> playwright-mcp: {browser.session.status()}"]
        if browser.session.connected:
            try:
                lines.append("\ntabs:\n" + await browser.session.call_text(
                    "browser_tabs", {"action": "list"}, timeout=30))
            except Exception as exc:  # noqa: BLE001 - diagnostics must still return
                lines.append(f"\ncould not list tabs: {exc}")
        return "\n".join(lines)

    @server.tool(
        title="Reset the browser",
        description=(
            "Throw away the current browser context -- tabs, cookies, storage, "
            "console and network history -- and start clean on the next call. Use "
            "it between independent test runs, or to recover a wedged page."
        ),
    )
    async def browser_reset(ctx: Context) -> str:
        audit.record("tool.browser_reset", principal=_principal(ctx))
        with contextlib.suppress(Exception):
            await browser.session.call("browser_close", {}, timeout=30)
        await browser.session.close()
        return ("Browser context discarded. The next browser call reconnects with a "
                "fresh about:blank.")
