"""Browser automation, proxied to the Playwright MCP server on 127.0.0.1:8931.

Why proxy instead of driving Playwright in-process: @playwright/mcp already
implements the whole accessibility-snapshot tool surface, and a second copy of
that here would be someone else's protocol work, re-derived and left to rot.

Why one long-lived connection rather than a connection per call: playwright-mcp
ties the browser context to its *connected clients*. With
--shared-browser-context that context is shared between clients connected at the
same time, but it is torn down the moment the last one disconnects. Measured on
this host: connect, navigate to example.com, disconnect, reconnect -> the tab
list is back to a single about:blank. A connection per tool call would therefore
throw away the page between every step, so the bridge holds exactly one
connection open for the life of the process and funnels calls through it.

Calls are serialized deliberately. The browser is one shared surface; two
overlapping actions would race on the same page, and the second would usually
act on a DOM the first had already invalidated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client

logger = logging.getLogger("mcp-bridge.browser")

ENDPOINT = "http://127.0.0.1:8931/mcp"
STARTUP_TIMEOUT = 45.0
DEFAULT_TIMEOUT = 120.0

# playwright-mcp writes snapshot files relative to its own working directory,
# which the unit file pins to the home directory.
OUTPUT_ROOT = Path.home()
MAX_INLINE_SNAPSHOT = 20_000


class BrowserError(RuntimeError):
    """A browser call failed, or the session behind it went away."""


@dataclass
class _Job:
    tool: str
    args: dict[str, Any]
    timeout: float
    future: asyncio.Future = field(repr=False)


class BrowserSession:
    """Owns the single MCP connection to playwright-mcp.

    The connection lives inside one background task. That is not incidental: the
    SDK client is an anyio task group under the hood, and a task group must be
    exited by the same task that entered it. Opening it in one request handler
    and closing it in another would blow up with a cancel-scope error, so the
    connection is opened, used, and closed entirely within `_run`.
    """

    def __init__(self, endpoint: str = ENDPOINT) -> None:
        self.endpoint = endpoint
        self.generation = 0          # bumped per successful connect
        self.calls = 0
        self._queue: asyncio.Queue[_Job] | None = None
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._error = ""
        self._lock = asyncio.Lock()

    # ---- lifecycle ----------------------------------------------------- #

    async def _ensure(self) -> None:
        """Connect if we are not already connected. Safe to call concurrently."""
        async with self._lock:
            if self._task is not None and not self._task.done():
                return
            self._queue = asyncio.Queue()
            self._ready = asyncio.Event()
            self._error = ""
            self._task = asyncio.create_task(self._run(), name="browser-session")

            ready = asyncio.ensure_future(self._ready.wait())
            done, _ = await asyncio.wait(
                {ready, self._task}, timeout=STARTUP_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                ready.cancel()
                raise BrowserError(
                    f"timed out after {STARTUP_TIMEOUT:.0f}s connecting to "
                    f"playwright-mcp at {self.endpoint}."
                )
            ready.cancel()
            # The worker finishing before signalling ready means the connect failed.
            if self._task.done():
                raise BrowserError(self._error or self._why_unreachable())

    async def _run(self) -> None:
        queue, ready = self._queue, self._ready
        assert queue is not None and ready is not None
        try:
            async with Client(self.endpoint, read_timeout_seconds=None) as client:
                self.generation += 1
                logger.info("browser session connected (generation %d)", self.generation)
                ready.set()
                while True:
                    job = await queue.get()
                    if job.future.done():      # caller gave up while queued
                        continue
                    try:
                        result = await asyncio.wait_for(
                            client.call_tool(job.tool, job.args), timeout=job.timeout
                        )
                    except asyncio.TimeoutError:
                        self._fail(job, BrowserError(
                            f"{job.tool} did not finish within {job.timeout:.0f}s. The "
                            f"page may still be loading; browser_state will show where "
                            f"it got to."
                        ))
                    except Exception as exc:   # noqa: BLE001 - surface, keep session
                        self._fail(job, BrowserError(f"{job.tool} failed: {exc}"))
                    else:
                        self.calls += 1
                        if not job.future.done():
                            job.future.set_result(result)
        except asyncio.CancelledError:
            self._error = "session closed"
            raise
        except Exception as exc:  # noqa: BLE001 - connect or transport died
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("browser session ended: %s", self._error)
        finally:
            ready.set()
            self._drain(queue)

    def _fail(self, job: _Job, exc: Exception) -> None:
        if not job.future.done():
            job.future.set_exception(exc)

    def _drain(self, queue: asyncio.Queue[_Job]) -> None:
        while not queue.empty():
            self._fail(queue.get_nowait(), BrowserError(
                "the browser session dropped before this call ran; retry it."
            ))

    def _why_unreachable(self) -> str:
        return (
            f"could not reach playwright-mcp at {self.endpoint}. Check it with: "
            f"systemctl --user status playwright-mcp"
        )

    async def close(self) -> None:
        """Drop the connection. playwright-mcp discards the browser context with it."""
        async with self._lock:
            task, self._task = self._task, None
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # ---- calling ------------------------------------------------------- #

    async def call(
        self, tool: str, args: dict[str, Any], timeout: float = DEFAULT_TIMEOUT
    ) -> Any:
        """Run one playwright-mcp tool and hand back its raw CallToolResult."""
        await self._ensure()
        assert self._queue is not None
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Job(tool, {k: v for k, v in args.items() if v is not None},
                                    timeout, future))
        return await future

    async def call_text(
        self, tool: str, args: dict[str, Any], timeout: float = DEFAULT_TIMEOUT
    ) -> str:
        """The common case: run a tool and render its reply as text."""
        return render(await self.call(tool, args, timeout))

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> str:
        if self.connected:
            return (f"connected to {self.endpoint} "
                    f"(session #{self.generation}, {self.calls} calls served)")
        return f"not connected to {self.endpoint}" + (f" -- last error: {self._error}"
                                                      if self._error else "")


# ---- result rendering --------------------------------------------------- #

def render(result: Any, *, drop_code: bool = True) -> str:
    """Flatten a CallToolResult to text.

    playwright-mcp echoes the equivalent Playwright source for every action under
    a '### Ran Playwright code' heading. It is helpful when you are writing a
    spec by hand and pure noise when a model is driving, so it goes by default --
    it is roughly a third of the payload on a typical click.
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif type(block).__name__ == "ImageContent":
            data = getattr(block, "data", "") or ""
            parts.append(f"[image: {getattr(block, 'mime_type', 'image')}, "
                         f"{len(data)} b64 chars -- use browser_screenshot to view it]")
    body = "\n".join(parts).strip()
    if drop_code:
        body = _strip_code_echo(body)
    body = _inline_snapshot(body)
    # Checked after stripping, not before: actions like resize reply with nothing
    # but the code echo, and would otherwise come back as an empty string.
    body = body.strip() or "(done -- no output)"
    if getattr(result, "is_error", False):
        return f"BROWSER CALL FAILED\n{body}"
    return body


_SNAPSHOT_LINK = re.compile(r"\[Snapshot\]\(([^)]+)\)")


def _inline_snapshot(text: str) -> str:
    """Splice in snapshot files that playwright-mcp returns as a bare link.

    Actions answer with `[Snapshot](path.yml)` rather than the tree itself, which
    would otherwise cost a second round-trip -- and a file read -- before the
    caller can see what the click actually did. Explicit browser_snapshot calls
    already come back inline, so this only fires on the linked form.
    """
    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        path = Path(raw)
        if not path.is_absolute():
            path = OUTPUT_ROOT / path
        try:
            content = path.read_text(errors="replace").strip()
        except OSError:
            return match.group(0)  # leave the link; better than swallowing it
        if len(content) > MAX_INLINE_SNAPSHOT:
            content = (content[:MAX_INLINE_SNAPSHOT]
                       + f"\n... truncated at {MAX_INLINE_SNAPSHOT} chars; "
                         f"full tree at {path}")
        return f"\n```yaml\n{content}\n```"

    return _SNAPSHOT_LINK.sub(replace, text)


def _strip_code_echo(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith("### Ran Playwright code"):
            skipping = True
            continue
        if skipping:
            # the echo is one fenced block; the next '###' heading ends it
            if line.startswith("###"):
                skipping = False
            else:
                continue
        out.append(line)
    return "\n".join(out).strip()


def images(result: Any) -> list[tuple[bytes, str]]:
    """Extract (raw_bytes, mime_type) for every image block in a result."""
    import base64

    found = []
    for block in getattr(result, "content", None) or []:
        if type(block).__name__ == "ImageContent" and getattr(block, "data", None):
            with contextlib.suppress(Exception):
                found.append((base64.b64decode(block.data),
                              getattr(block, "mime_type", "image/png")))
    return found


session = BrowserSession()
