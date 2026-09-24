from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from collectors.base import InvalidURLError, Platform, SOURCE_CONFIGS

Resolver = Callable[[str, int], Awaitable[list[tuple]]]


def _default_resolver(host: str, port: int) -> Awaitable[list[tuple]]:
    return asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)


def validate_url_shape(url: str, platform: Platform) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise InvalidURLError("URL is not allowed")
    if parsed.port not in (None, 80, 443):
        raise InvalidURLError("URL port is not allowed")
    host = parsed.hostname.rstrip(".").lower()
    config = SOURCE_CONFIGS[platform]
    if not any(host == domain or host.endswith("." + domain) for domain in config.domains):
        raise InvalidURLError("URL domain is not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return parsed._replace(fragment="").geturl()
    if not address.is_global or address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
        raise InvalidURLError("URL address is not public")
    return parsed._replace(fragment="").geturl()


async def validate_public_dns(url: str, resolver: Resolver = _default_resolver) -> None:
    host = urlparse(url).hostname
    if not host:
        raise InvalidURLError("URL host is missing")
    try:
        addresses = await resolver(host, urlparse(url).port or (443 if urlparse(url).scheme == "https" else 80))
    except (OSError, socket.gaierror) as exc:
        raise InvalidURLError("URL DNS lookup failed") from exc
    ips = {item[4][0] for item in addresses if len(item) > 4 and item[4]}
    if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback or ipaddress.ip_address(ip).is_link_local or ipaddress.ip_address(ip).is_multicast or ipaddress.ip_address(ip).is_reserved or ipaddress.ip_address(ip).is_unspecified for ip in ips):
        raise InvalidURLError("URL resolves to a non-public address")
