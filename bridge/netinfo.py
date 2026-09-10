"""Network helpers for picking a sane bind address."""

from __future__ import annotations

import ipaddress
import socket

import psutil

# Virtual bridges created by docker/libvirt are not the LAN.
_VIRTUAL_PREFIXES = ("docker", "br-", "virbr", "veth", "lo", "tun", "tap", "wg")


def primary_lan_ip() -> str | None:
    """The address this host uses to reach the default gateway."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("1.1.1.1", 80))  # no packets sent; just resolves the route
            return s.getsockname()[0]
    except OSError:
        return None


def candidate_addresses() -> list[dict[str, str]]:
    """Real, usable IPv4 addresses, best candidate first."""
    primary = primary_lan_ip()
    out: list[dict[str, str]] = []
    for iface, addrs in psutil.net_if_addrs().items():
        if iface.startswith(_VIRTUAL_PREFIXES):
            continue
        stats = psutil.net_if_stats().get(iface)
        if stats is None or not stats.isup:
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address
            if ip.startswith("127."):
                continue
            out.append({
                "interface": iface,
                "address": ip,
                "primary": "yes" if ip == primary else "no",
            })
    out.sort(key=lambda a: a["primary"] != "yes")
    return out


def is_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def resolve_bind(mode: str) -> str:
    """Map a bind mode to a concrete address to listen on."""
    if mode == "loopback":
        return "127.0.0.1"
    if mode == "all":
        return "0.0.0.0"
    if mode == "lan":
        return primary_lan_ip() or "0.0.0.0"
    return mode  # an explicit address was configured


def advertised_host(bind: str) -> str:
    """The address other machines should actually connect to."""
    if bind in ("0.0.0.0", "::"):
        return primary_lan_ip() or "127.0.0.1"
    return bind
