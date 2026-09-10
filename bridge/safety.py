"""Catastrophic-command guardrails.

This bridge is configured for full shell access including sudo, so this is
deliberately NOT an allowlist and not a security boundary -- anything with a
shell can trivially evade a regex. It is a seatbelt against a model (or a
fat-fingered paste) issuing an irreversible, machine-destroying command by
accident. Set MCP_BRIDGE_GUARDRAILS=false to remove it entirely.
"""

from __future__ import annotations

import getpass
import re
from dataclasses import dataclass

_ME = re.escape(getpass.getuser())

# (compiled pattern, human explanation)
_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rR][a-zA-Z]*f?[a-zA-Z]*\s+/(\s|$|\*)"),
        "recursive delete of the filesystem root",
    ),
    (
        re.compile(r"\brm\s+.*(-[a-zA-Z]*[rR]).*\s+(/|/etc|/usr|/var|/boot|/home)(\s|/?\*|$)"),
        "recursive delete of a critical system directory",
    ),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem creation (destroys a device)"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/(sd|nvme|vd|hd|mmcblk)"), "raw write to a block device"),
    (re.compile(r"\b(shred|wipefs)\b[^|;&]*/dev/"), "destructive wipe of a device"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)\w*"), "shell redirect onto a block device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bchmod\s+(-[a-zA-Z]*[rR][a-zA-Z]*\s+)?0*777\s+/(\s|$)"), "chmod 777 on /"),
    (re.compile(r"\bchown\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+\S+\s+/(\s|$)"), "recursive chown of /"),
    (re.compile(r"\b(halt|poweroff)\b|\bshutdown\b(?!.*-c)|\bsystemctl\s+(poweroff|halt)\b"),
     "powering the machine off (you would lose the tunnel and all access)"),
    (re.compile(rf"\buserdel\s+.*\b{_ME}\b|\bpasswd\s+-l\s+{_ME}\b"), "locking or deleting your own account"),
    (re.compile(r"\bufw\s+.*\bdeny\b.*\bout\b|\biptables\s+-[AI]\s+OUTPUT\s+.*DROP"),
     "blocking outbound traffic (would kill the tunnel)"),
    (re.compile(r"\bsystemctl\s+(stop|disable|mask)\s+.*(cloudflared|mcp-bridge)"),
     "stopping the bridge or tunnel service (would cut your own connection)"),
    (re.compile(r"\b>\s*/etc/(passwd|shadow|sudoers)\b"), "overwriting a critical auth file"),
]

# Paths whose contents are never returned by the structured read_file tool.
SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(\.ssh/(id_\w+|.*_key)$|\.aws/credentials$|\.gnupg/|shadow$|gshadow$"
    r"|\.env$|\.netrc$|\.pgpass$|credentials\.json$|token\.json$)"
)

_SECRET_LINE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|bearer|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*\S+"
)


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""


def check_command(command: str) -> Verdict:
    """Return a Verdict for a shell command against the catastrophic list."""
    normalized = " ".join(command.split())
    for pattern, explanation in _RULES:
        if pattern.search(normalized):
            return Verdict(False, explanation)
    return Verdict(True)


def redact(text: str) -> str:
    """Mask obvious secret assignments in captured output."""
    return _SECRET_LINE_RE.sub(
        lambda m: m.group(0).split("=")[0].split(":")[0] + "=***redacted***", text
    )
