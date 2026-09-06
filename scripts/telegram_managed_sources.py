"""Encrypted, admin-managed subscription sources for the Telegram collector.

The repository is public, so raw subscription URLs must never be committed to
main, data/servers.json, logs, or the public bot-state branch. URLs are stored as
one Fernet-encrypted blob on telegram-bot-state. The encryption key is derived
from TELEGRAM_SOURCE_ENCRYPTION_KEY when configured, otherwise from the existing
TELEGRAM_BOT_TOKEN for zero-setup backwards compatibility.

Managed-source HTTP(S) fetches are SSRF-hardened: every hop is normalized,
resolved, rejected unless every resolved address is globally routable, and the
actual TCP socket is pinned to one of those already-validated addresses. HTTPS
still verifies the certificate and SNI against the original hostname. Redirects
repeat the same validation/pinning process. Payload size and redirect depth are
bounded and content must contain at least one config understood by the existing
parser.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from parser import parse_source

SOURCE_STATE_PATH = "telegram_subscription_sources.json"
SOURCE_STATE_BRANCH = "telegram-bot-state"
SOURCE_STATE_VERSION = 1
MAX_MANAGED_SOURCES = 25
MAX_SOURCE_BYTES = 2_000_000
FETCH_TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
USER_AGENT = "Broute-Collector/managed-subscription"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class SourceValidationError(ValueError):
    pass


class SourceCryptoError(RuntimeError):
    pass


def source_secret() -> str:
    dedicated = os.environ.get("TELEGRAM_SOURCE_ENCRYPTION_KEY", "").strip()
    if dedicated:
        return dedicated
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SourceCryptoError(
            "TELEGRAM_SOURCE_ENCRYPTION_KEY or TELEGRAM_BOT_TOKEN is required"
        )
    return token


def _fernet(secret: str | None = None) -> Fernet:
    value = (secret if secret is not None else source_secret()).encode("utf-8")
    digest = hashlib.sha256(b"broute-managed-subscriptions-v1\0" + value).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_sources(sources: List[Dict[str, Any]], secret: str | None = None) -> Dict[str, Any]:
    raw = json.dumps(
        {"sources": sources},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = _fernet(secret).encrypt(raw).decode("ascii")
    return {"version": SOURCE_STATE_VERSION, "ciphertext": ciphertext}


def decrypt_sources(payload: Dict[str, Any], secret: str | None = None) -> List[Dict[str, Any]]:
    if not payload:
        return []
    if int(payload.get("version", 0) or 0) != SOURCE_STATE_VERSION:
        raise SourceCryptoError("unsupported managed-source state version")
    ciphertext = str(payload.get("ciphertext") or "")
    if not ciphertext:
        return []
    try:
        decoded = _fernet(secret).decrypt(ciphertext.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise SourceCryptoError(
            "managed-source state could not be decrypted; check the encryption secret"
        ) from exc
    sources = data.get("sources", []) if isinstance(data, dict) else []
    if not isinstance(sources, list):
        raise SourceCryptoError("managed-source plaintext has invalid structure")
    return [dict(item) for item in sources if isinstance(item, dict)]


def normalize_subscription_url(url: str) -> str:
    value = str(url or "").strip()
    if not value or any(ch.isspace() for ch in value):
        raise SourceValidationError("لینک subscription خالی یا دارای فاصله است")

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise SourceValidationError("پورت لینک معتبر نیست") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SourceValidationError("فقط لینک‌های http/https قابل اضافه شدن هستند")
    if parts.username or parts.password:
        raise SourceValidationError("لینک دارای username/password در URL پذیرفته نمی‌شود")
    if not parts.hostname:
        raise SourceValidationError("لینک hostname معتبر ندارد")
    if port is not None and not (1 <= port <= 65535):
        raise SourceValidationError("پورت لینک معتبر نیست")

    host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise SourceValidationError("آدرس local/private به‌عنوان منبع پذیرفته نمی‌شود")

    if ":" in host and not host.startswith("["):
        netloc_host = f"[{host}]"
    else:
        netloc_host = host
    netloc = netloc_host if port is None else f"{netloc_host}:{port}"

    # Fragment never affects the HTTP request and may accidentally expose labels
    # or tokens in later UI, so discard it from the canonical source identity.
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def resolve_public_addresses(url: str) -> Tuple[str, List[str]]:
    """Normalize URL and return only after every DNS result is globally routable."""
    normalized = normalize_subscription_url(url)
    parts = urlsplit(normalized)
    host = str(parts.hostname or "")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceValidationError("دامنه‌ی subscription قابل resolve نیست") from exc

    addresses: List[str] = []
    seen = set()
    for info in infos:
        if not info or not info[4]:
            continue
        address = str(info[4][0]).split("%", 1)[0]
        if address in seen:
            continue
        seen.add(address)
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise SourceValidationError("IP منبع معتبر نیست") from exc
        if not ip.is_global:
            raise SourceValidationError(
                "منبعی که به IP خصوصی/local/link-local/reserved وصل شود پذیرفته نمی‌شود"
            )
        addresses.append(address)

    if not addresses:
        raise SourceValidationError("دامنه‌ی subscription هیچ IP معتبری ندارد")
    return normalized, addresses


def validate_public_url(url: str) -> str:
    normalized, _ = resolve_public_addresses(url)
    return normalized


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose TCP destination cannot change after DNS validation."""

    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: int):
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS pinned to validated IP while retaining hostname SNI/cert checks."""

    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: int):
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)
        except Exception:
            raw_sock.close()
            raise


def _request_target(parts) -> str:
    target = parts.path or "/"
    if parts.query:
        target += f"?{parts.query}"
    return target


def _host_header(parts) -> str:
    hostname = str(parts.hostname or "")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parts.scheme == "https" else 80
    if parts.port is not None and parts.port != default_port:
        return f"{hostname}:{parts.port}"
    return hostname


def _fetch_one_hop(url: str) -> Tuple[int, Dict[str, str], bytes]:
    normalized, addresses = resolve_public_addresses(url)
    parts = urlsplit(normalized)
    host = str(parts.hostname or "")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    last_error: Exception | None = None
    for pinned_ip in addresses:
        connection: http.client.HTTPConnection
        if parts.scheme == "https":
            connection = _PinnedHTTPSConnection(host, pinned_ip, port, FETCH_TIMEOUT_SECONDS)
        else:
            connection = _PinnedHTTPConnection(host, pinned_ip, port, FETCH_TIMEOUT_SECONDS)

        try:
            connection.request(
                "GET",
                _request_target(parts),
                headers={
                    "Host": _host_header(parts),
                    "User-Agent": USER_AGENT,
                    "Accept": "text/plain, application/octet-stream, */*;q=0.5",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = int(response.status)
            headers = {str(k).lower(): str(v) for k, v in response.getheaders()}

            if status in REDIRECT_STATUSES:
                # Redirect bodies are irrelevant and may be arbitrarily large.
                response.close()
                return status, headers, b""

            data = response.read(MAX_SOURCE_BYTES + 1)
            response.close()
            return status, headers, data
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
            last_error = exc
        finally:
            connection.close()

    raise SourceValidationError("اتصال امن به subscription ناموفق بود") from last_error


def fetch_subscription(url: str) -> str:
    current = normalize_subscription_url(url)

    for redirect_count in range(MAX_REDIRECTS + 1):
        try:
            status, headers, data = _fetch_one_hop(current)
        except SourceValidationError:
            raise
        except Exception as exc:
            raise SourceValidationError("دریافت subscription ناموفق بود") from exc

        if status in REDIRECT_STATUSES:
            location = str(headers.get("location") or "").strip()
            if not location:
                raise SourceValidationError("redirect بدون مقصد معتبر دریافت شد")
            if redirect_count >= MAX_REDIRECTS:
                raise SourceValidationError("تعداد redirectهای subscription بیش از حد مجاز است")
            current = normalize_subscription_url(urljoin(current, location))
            # The next loop resolves, validates and pins the redirect destination.
            continue

        if not (200 <= status < 300):
            raise SourceValidationError(f"subscription پاسخ HTTP {status} داد")
        if len(data) > MAX_SOURCE_BYTES:
            raise SourceValidationError("حجم subscription بیشتر از سقف 2MB است")

        text = data.decode("utf-8", errors="ignore")
        if not text.strip():
            raise SourceValidationError("subscription خالی است")
        return text

    raise SourceValidationError("redirect subscription قابل تکمیل نبود")


def valid_config_count(content: str) -> int:
    parsed = parse_source(
        {
            "source_name": "managed-validation",
            "source_url": "managed://validation",
            "content": content,
        }
    )
    return sum(1 for cfg in parsed if getattr(cfg, "valid", False))


def validate_subscription(url: str) -> Tuple[str, int]:
    normalized = validate_public_url(url)
    content = fetch_subscription(normalized)
    count = valid_config_count(content)
    if count < 1:
        raise SourceValidationError(
            "در این subscription هیچ کانفیگ قابل‌شناسایی پیدا نشد"
        )
    return normalized, count


def source_host(url: str) -> str:
    try:
        return str(urlsplit(url).hostname or "unknown")
    except Exception:
        return "unknown"


def new_source_entry(url: str, config_count: int) -> Dict[str, Any]:
    source_id = hashlib.sha256(
        f"{url}\0{time.time_ns()}\0{os.urandom(16).hex()}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "id": source_id,
        "url": url,
        "enabled": True,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_validated_configs": int(config_count),
    }


def load_sources_from_file(path: str, secret: str | None = None) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceCryptoError("managed-source state file is invalid") from exc
    if not isinstance(payload, dict):
        raise SourceCryptoError("managed-source state file has invalid structure")
    return decrypt_sources(payload, secret)


def enabled_sources(sources: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(item) for item in sources if item.get("enabled") is True and item.get("url")]
