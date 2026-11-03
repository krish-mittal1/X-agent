"""Security utilities for external URL retrieval and SSRF mitigation."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.logging_config import get_logger

logger = get_logger(__name__)

# Blocked IP networks: loopback, private RFC1918, link-local, carrier-grade NAT, cloud metadata
BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local & cloud metadata (169.254.169.254)
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),  # Multicast
    ipaddress.ip_network("240.0.0.0/4"),  # Reserved
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 Unique Local
    ipaddress.ip_network("fe80::/10"),  # IPv6 Link-Local
]

BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "instance-data",
    "169.254.169.254",
}


def is_ip_blocked(ip_addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address belongs to private, loopback, or link-local ranges."""
    if (
        ip_addr.is_loopback
        or ip_addr.is_private
        or ip_addr.is_link_local
        or ip_addr.is_multicast
        or ip_addr.is_reserved
        or ip_addr.is_unspecified
    ):
        return True
    return any(ip_addr in net for net in BLOCKED_NETWORKS)


def is_safe_url(url: str) -> bool:
    """Validate that a URL is safe for external research retrieval (anti-SSRF)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        hostname = (parsed.hostname or "").strip().lower()
        if not hostname:
            return False

        if hostname in BLOCKED_HOSTNAMES:
            return False

        # Attempt to parse as literal IP address
        try:
            ip = ipaddress.ip_address(hostname)
            if is_ip_blocked(ip):
                return False
        except ValueError:
            # It's a domain name; resolve DNS to inspect target IP addresses
            try:
                addr_info = socket.getaddrinfo(hostname, None)
                for entry in addr_info:
                    sockaddr = entry[4]
                    ip_str = sockaddr[0]
                    resolved_ip = ipaddress.ip_address(ip_str)
                    if is_ip_blocked(resolved_ip):
                        return False
            except (socket.gaierror, socket.herror):
                # DNS failure: reject as unreachable/unsafe
                return False

        return True
    except Exception as exc:
        logger.warning("SSRF validation error", extra={"extra_fields": {"url": url, "error": str(exc)}})
        return False


def validate_safe_url(url: str) -> str:
    """Validate URL safety, raising ValueError if unsafe."""
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe or private network destination: {url}")
    return url

