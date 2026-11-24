"""SSRF protection for user-configurable provider URLs.

Provider endpoints (Ollama, whisper-server, Piper, Asterisk ARI) are configurable from
the UI. They must point at loopback or private-network hosts unless the operator sets
``VA_ALLOW_REMOTE_PROVIDER_HOSTS=true``. Link-local / cloud-metadata ranges, multicast
and unspecified addresses are always refused.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


_ALWAYS_BLOCKED = [
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. cloud metadata 169.254.169.254
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba metadata
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IPv6 metadata
]


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"host '{host}' does not resolve") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def validate_provider_url(url: str, *, allow_remote: bool = False, schemes: tuple[str, ...] = ("http", "https")) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in schemes:
        raise UnsafeURLError(f"URL scheme must be one of {', '.join(schemes)}")
    if not parsed.hostname:
        raise UnsafeURLError("URL must include a host")
    if parsed.username or parsed.password:
        raise UnsafeURLError("credentials must not be embedded in the URL")
    if parsed.query or parsed.fragment:
        raise UnsafeURLError("URL must not contain a query string or fragment")
    addrs = _resolve(parsed.hostname)
    for addr in addrs:
        if any(addr in net for net in _ALWAYS_BLOCKED) or addr.is_unspecified or addr.is_multicast:
            raise UnsafeURLError(f"address {addr} is not allowed")
        if not allow_remote and not (addr.is_loopback or addr.is_private):
            raise UnsafeURLError(
                f"{parsed.hostname} resolves to a public address ({addr}); provider endpoints must be local. "
                "Set VA_ALLOW_REMOTE_PROVIDER_HOSTS=true to override."
            )
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def is_loopback_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return all(a.is_loopback for a in _resolve(host))
    except UnsafeURLError:
        return False
