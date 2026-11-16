"""Outbound host guard for org-supplied endpoints (SMTP). In prod a DNS name must not resolve to a
loopback, private, link-local or otherwise internal address (SSRF). IP literals are checked by the
schema. The check runs when settings are saved and again right before a connection is made, which
narrows (but cannot close) the DNS-rebinding window; production egress rules are the backstop."""

import asyncio
import ipaddress
import socket

from app.core.config import settings
from app.core.errors import validation_error


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0].split("%")[0] for info in infos]


def _internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback or ip.is_private or ip.is_reserved or ip.is_link_local
        or ip.is_unspecified or ip.is_multicast
    )  # fmt: skip


async def ensure_public_host(host: str, field: str = "host") -> None:
    if settings.app_env != "prod":
        return
    try:
        ipaddress.ip_address(host)
        return  # a literal: already validated by the schema
    except ValueError:
        pass
    try:
        addresses = await _resolve(host)
    except OSError:
        raise validation_error([{"field": field, "code": "unresolvable"}]) from None
    if not addresses or any(_internal(ipaddress.ip_address(a)) for a in addresses):
        raise validation_error([{"field": field, "code": "internal_address_not_allowed"}])
