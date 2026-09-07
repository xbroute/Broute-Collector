from __future__ import annotations

import ipaddress
import socket
from typing import Iterable
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ALLOWED_SCHEMES = {"http", "https"}
MAX_SOURCE_URL_LENGTH = 2048


def _resolved_ips(hostname: str) -> Iterable[ipaddress._BaseAddress]:
    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    seen: set[str] = set()
    for info in infos:
        address = str(info[4][0])
        if address in seen:
            continue
        seen.add(address)
        yield ipaddress.ip_address(address)


def validate_public_http_url(url: str) -> str:
    value = str(url or "").strip()
    if not value or len(value) > MAX_SOURCE_URL_LENGTH:
        raise ValueError("invalid subscription URL length")

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("only http/https subscription URLs are allowed")
    if not parsed.hostname:
        raise ValueError("subscription URL has no hostname")
    if parsed.username or parsed.password:
        raise ValueError("subscription URLs with embedded credentials are not allowed")

    try:
        literal = ipaddress.ip_address(parsed.hostname.strip("[]"))
        addresses = [literal]
    except ValueError:
        try:
            addresses = list(_resolved_ips(parsed.hostname))
        except OSError as exc:
            raise ValueError(f"subscription hostname could not be resolved: {exc}") from exc

    if not addresses:
        raise ValueError("subscription hostname resolved to no IP addresses")
    if any(not address.is_global for address in addresses):
        raise ValueError("private, loopback, link-local or reserved source addresses are not allowed")

    return value


class PublicOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        validate_public_http_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_public_text(
    url: str,
    *,
    timeout: int,
    max_size: int,
    user_agent: str,
) -> str:
    validated = validate_public_http_url(url)
    opener = build_opener(PublicOnlyRedirectHandler())
    request = Request(validated, headers={"User-Agent": user_agent})
    with opener.open(request, timeout=timeout) as response:
        final_url = response.geturl()
        validate_public_http_url(final_url)
        data = response.read(max_size + 1)
        if len(data) > max_size:
            raise ValueError("subscription payload is too large")
        return data.decode("utf-8", errors="ignore")
