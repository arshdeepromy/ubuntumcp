"""Starts, stops and inspects the bridge process.

The panel and the bridge are separate processes on purpose: restarting the
bridge to apply a port change must not take the panel down with it. A pidfile
lets the panel re-attach to a running bridge after the panel itself restarts.
"""

from __future__ import annotations

import hashlib
import json
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


def remove_sudoers(password: str = "") -> tuple[bool, str]:
    script = PROJECT_ROOT / "install-sudoers.sh"
    # Turning it OFF needs root, but if passwordless sudo is currently ON the
    # bridge already has that -- so no password is needed to switch it off. Only
    # fall back to the account password if passwordless isn't working (an
    # already-half-removed state).
    if not password and passwordless_sudo_works():
        cmd = ["sudo", "-n", "bash", str(script), "--remove"]
        stdin = None
    elif password:
        cmd = ["sudo", "-S", "-p", "", "bash", str(script), "--remove"]
        stdin = password + "\n"
    else:
        return False, "Account password required to turn sudo off."
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=60)
    except subprocess.SubprocessError as exc:
        return False, f"Failed: {exc}"
    if proc.returncode == 0:
        return True, "Passwordless sudo turned off. Privileged tool calls now fail."
    stderr = (proc.stderr or "").lower()
    if any(marker in stderr for marker in
           ("authentication failed", "incorrect password", "sorry, try again")):
        return False, "Incorrect password."
    return False, "Could not remove the rule."


def _token_ref(token: str) -> str:
    """A stable short handle for a token that never exposes the secret itself.

    The panel lists and kills tokens by this ref, so the raw bearer value never
    has to travel to the browser or sit in the DOM.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _client_like(client_id: str) -> str:
    # Token/code/pending rows store the owner as `"client_id": "<id>"` inside
    # their JSON blob (json.dumps default separators put a space after the colon).
    return f'%"client_id": "{client_id}"%'


def list_principals() -> dict:
    """Configured OAuth clients and their live access tokens, for the panel.

    Read straight from the SQLite store (like active_oauth_tokens), so it works
    whether the bridge is up or down. Only unexpired access tokens are listed --
    that is what "valid tokens" means to an operator.
    """
    db = STATE_DIR / "bridge.db"
    empty = {"clients": [], "tokens": []}
    if not db.exists():
        return empty
    now = time.time()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            names: dict[str, str] = {}
            clients = []
            for r in conn.execute(
                "SELECT client_id, data, created FROM clients ORDER BY created DESC"
            ):
                try:
                    d = json.loads(r["data"])
                except (ValueError, TypeError):
                    d = {}
                name = d.get("client_name") or "(unnamed)"
                names[r["client_id"]] = name
                live = conn.execute(
                    "SELECT COUNT(*) FROM tokens WHERE kind='access' "
                    "AND expires > ? AND data LIKE ?",
                    (now, _client_like(r["client_id"])),
                ).fetchone()[0]
                clients.append({
                    "client_id": r["client_id"],
                    "client_name": name,
                    "redirect_uris": d.get("redirect_uris", []),
                    "created": int(r["created"]) if r["created"] else None,
                    "live_tokens": live,
                })
            tokens = []
            for r in conn.execute(
                "SELECT token, data, expires FROM tokens "
                "WHERE kind='access' AND expires > ? ORDER BY expires",
                (now,),
            ):
                try:
                    d = json.loads(r["data"])
                except (ValueError, TypeError):
                    d = {}
                cid = d.get("client_id", "")
                tokens.append({
                    "ref": _token_ref(r["token"]),
                    "prefix": r["token"][:10],
                    "client_id": cid,
                    "client_name": names.get(cid) or (cid[:8] if cid else "unknown"),
                    "expires_seconds": round(r["expires"] - now),
                })
            return {"clients": clients, "tokens": tokens}
        finally:
            conn.close()
    except sqlite3.Error:
        return empty


def delete_client(client_id: str) -> tuple[bool, int]:
    """Remove a client registration and cascade every credential it owns.

    Returns (removed, tokens_removed). Live transport sessions for the client are
    torn down too when the bridge is running, so the kill takes effect at once
    instead of waiting for the next (now-401) request.
    """
    db = STATE_DIR / "bridge.db"
    if not db.exists():
        return False, 0
    like = _client_like(client_id)
    try:
        conn = sqlite3.connect(db)
        try:
            existed = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()[0]
            if not existed:
                return False, 0
            ntok = conn.execute(
                "SELECT COUNT(*) FROM tokens WHERE data LIKE ?", (like,)
            ).fetchone()[0]
            conn.execute("DELETE FROM tokens WHERE data LIKE ?", (like,))
            conn.execute("DELETE FROM auth_codes WHERE data LIKE ?", (like,))
            conn.execute("DELETE FROM pending WHERE data LIKE ?", (like,))
            conn.execute("DELETE FROM clients WHERE client_id = ?", (client_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return False, 0
    if is_running():
        admin_request("POST", "/admin/terminate-client", {"client_id": client_id})
    return True, ntok


def revoke_token(ref: str) -> bool:
    """Delete the single access token whose ref matches. Its client keeps its
    registration and any other tokens; the next request on this one gets 401."""
    db = STATE_DIR / "bridge.db"
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(db)
        try:
            target = None
            for (token,) in conn.execute("SELECT token FROM tokens WHERE kind='access'"):
                if _token_ref(token) == ref:
                    target = token
                    break
            if target is None:
                return False
            conn.execute("DELETE FROM tokens WHERE token = ?", (target,))
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


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
