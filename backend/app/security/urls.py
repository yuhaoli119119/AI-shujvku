from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx


class UnsafeOutboundURL(ValueError):
    pass


def validate_public_http_url(url: str) -> str:
    value = (url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeOutboundURL("Only public http/https URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeOutboundURL("Credentials in outbound URLs are not allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeOutboundURL("Local network URLs are not allowed")
    try:
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        addresses = {item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(hostname, parsed.port or default_port)}
    except OSError as exc:
        raise UnsafeOutboundURL("Outbound URL hostname could not be resolved") from exc
    if not addresses:
        raise UnsafeOutboundURL("Outbound URL hostname did not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise UnsafeOutboundURL("Local, private, link-local, or reserved URLs are not allowed")
    return value


def get_public_url(
    client: httpx.Client,
    url: str,
    *,
    max_redirects: int = 5,
    **request_kwargs,
) -> httpx.Response:
    current = validate_public_http_url(url)
    for redirect_count in range(max_redirects + 1):
        kwargs = request_kwargs if redirect_count == 0 else {
            key: value for key, value in request_kwargs.items() if key == "headers"
        }
        response = client.get(current, follow_redirects=False, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        if redirect_count >= max_redirects:
            raise UnsafeOutboundURL("Too many outbound redirects")
        location = response.headers.get("location")
        if not location:
            return response
        current = validate_public_http_url(urljoin(current, location))
    raise UnsafeOutboundURL("Too many outbound redirects")
