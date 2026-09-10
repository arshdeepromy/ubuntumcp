"""Starts, stops and inspects the bridge process.

The panel and the bridge are separate processes on purpose: restarting the
bridge to apply a port change must not take the panel down with it. A pidfile
lets the panel re-attach to a running bridge after the panel itself restarts.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil

from bridge.config import STATE_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PID_FILE = STATE_DIR / "bridge.pid"
LOG_FILE = STATE_DIR / "bridge.log"
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _process() -> psutil.Process | None:
    """The running bridge, or None. Verifies the PID is really ours."""
    pid = _read_pid()
    if pid is None:
        return None
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
        # PIDs get recycled; confirm this is actually the bridge.
        if "bridge" in cmdline and proc.is_running():
            return proc
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    PID_FILE.unlink(missing_ok=True)
    return None


def is_running() -> bool:
    return _process() is not None


def status() -> dict:
    proc = _process()
    if proc is None:
        return {"running": False, "pid": None, "uptime_seconds": None,
                "memory_mb": None, "cpu_percent": None}
    try:
        with proc.oneshot():
            return {
                "running": True,
                "pid": proc.pid,
                "uptime_seconds": int(time.time() - proc.create_time()),
                "memory_mb": round(proc.memory_info().rss / 1_048_576, 1),
                "cpu_percent": proc.cpu_percent(None),
            }
    except psutil.Error:
        return {"running": False, "pid": None, "uptime_seconds": None,
                "memory_mb": None, "cpu_percent": None}


def start() -> tuple[bool, str]:
    if is_running():
        return False, "Bridge is already running."
    STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not PYTHON.exists():
        return False, f"Interpreter missing at {PYTHON}. Recreate the venv."

    log = open(LOG_FILE, "ab", buffering=0)  # noqa: SIM115 - owned by the child
    try:
        proc = subprocess.Popen(
            [str(PYTHON), "-m", "bridge"],
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives panel restart; killable as a group
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except OSError as exc:
        return False, f"Failed to launch: {exc}"

    PID_FILE.write_text(str(proc.pid))

    # A config error exits within the first second; surface it instead of
    # reporting a false success.
    time.sleep(1.5)
    if proc.poll() is not None:
        PID_FILE.unlink(missing_ok=True)
        return False, f"Bridge exited immediately (code {proc.returncode}). {tail_log(6)}"
    return True, f"Bridge started (pid {proc.pid})."


def stop(timeout: float = 10.0) -> tuple[bool, str]:
    proc = _process()
    if proc is None:
        return False, "Bridge is not running."
    pid = proc.pid
    try:
        # Signal the whole group so uvicorn's workers go too.
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except psutil.Error:
            pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not proc.is_running():
            break
        time.sleep(0.2)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    PID_FILE.unlink(missing_ok=True)
    return True, f"Bridge stopped (was pid {pid})."


def restart() -> tuple[bool, str]:
    if is_running():
        stop()
    return start()


def tail_log(lines: int = 60) -> str:
    try:
        content = LOG_FILE.read_text(errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return ""


def tail_audit(lines: int = 40) -> list[str]:
    try:
        content = (STATE_DIR / "audit.log").read_text(errors="replace").splitlines()
        return content[-lines:][::-1]  # newest first
    except OSError:
        return []


def active_oauth_tokens() -> int:
    db = STATE_DIR / "bridge.db"
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM tokens WHERE kind='access' AND expires > ?",
                (time.time(),),
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def bridge_base_url() -> str:
    """Where the panel reaches the bridge locally, regardless of public URL."""
    from bridge import netinfo
    from bridge.config import read_env_file

    env = read_env_file()
    port = env.get("MCP_BRIDGE_PORT", "8901")
    bind = netinfo.resolve_bind(env.get("MCP_BRIDGE_BIND", "lan"))
    host = "127.0.0.1" if bind in ("0.0.0.0", "::") else bind
    return f"http://{host}:{port}"


def admin_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Call the bridge's admin API using the operator passphrase."""
    import httpx

    from bridge.config import read_env_file

    passphrase = read_env_file().get("MCP_BRIDGE_PASSPHRASE", "")
    url = f"{bridge_base_url()}{path}"
    try:
        response = httpx.request(
            method, url, json=payload or {},
            headers={"X-Bridge-Admin": passphrase}, timeout=15,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Bridge unreachable: {exc}"}
    if response.status_code == 401:
        return {"ok": False, "error": "Bridge rejected the admin passphrase."}
    try:
        return response.json()
    except ValueError:
        return {"ok": False, "error": f"Unexpected reply ({response.status_code})."}


def sudoers_installed() -> bool:
    return Path("/etc/sudoers.d/99-mcp-bridge").exists()


def passwordless_sudo_works() -> bool:
    try:
        return subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=5
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def install_sudoers(password: str) -> tuple[bool, str]:
    """Install the NOPASSWD rule, authenticating with the account password.

    The password is piped to `sudo -S` on stdin and never stored or logged.
    """
    script = PROJECT_ROOT / "install-sudoers.sh"
    if not script.exists():
        return False, "install-sudoers.sh is missing."
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-p", "", "bash", str(script), "--yes"],
            input=password + "\n", capture_output=True, text=True, timeout=60,
        )
    except subprocess.SubprocessError as exc:
        return False, f"Failed to run installer: {exc}"

    if proc.returncode == 0:
        return True, "Passwordless sudo installed. Privileged tools will work now."

    # sudo-rs (Ubuntu 26.04's default) reports a bad password as "Authentication
    # failed, try again." and then adds a second line about being unable to
    # retry, so match anywhere in stderr rather than reading the last line.
    stderr = (proc.stderr or "").lower()
    if any(marker in stderr for marker in
           ("authentication failed", "incorrect password", "sorry, try again")):
        return False, "Incorrect password."
    if "not in the sudoers file" in stderr:
        return False, f"{os.environ.get('USER', 'this user')} is not permitted to use sudo."

    detail = [ln for ln in (proc.stderr or proc.stdout or "").strip().splitlines() if ln.strip()]
    return False, detail[-1][:200] if detail else f"Installer exited {proc.returncode}."


def remove_sudoers(password: str) -> tuple[bool, str]:
    script = PROJECT_ROOT / "install-sudoers.sh"
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-p", "", "bash", str(script), "--remove"],
            input=password + "\n", capture_output=True, text=True, timeout=60,
        )
    except subprocess.SubprocessError as exc:
        return False, f"Failed: {exc}"
    if proc.returncode == 0:
        return True, "Passwordless sudo removed. sudo needs a password again."
    stderr = (proc.stderr or "").lower()
    if any(marker in stderr for marker in
           ("authentication failed", "incorrect password", "sorry, try again")):
        return False, "Incorrect password."
    return False, "Could not remove the rule."


def revoke_all_tokens() -> int:
    db = STATE_DIR / "bridge.db"
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
            conn.execute("DELETE FROM tokens")
            conn.execute("DELETE FROM auth_codes")
            conn.execute("DELETE FROM pending")
            conn.commit()
            return n
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


if __name__ == "__main__":  # tiny CLI, handy for debugging
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "start":
        print(start()[1])
    elif action == "stop":
        print(stop()[1])
    elif action == "restart":
        print(restart()[1])
    else:
        print(status())
