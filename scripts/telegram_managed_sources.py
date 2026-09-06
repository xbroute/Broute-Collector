"""Encrypted, admin-managed subscription sources for the Telegram collector.

The repository is public, so raw subscription URLs must never be committed to
main, data/servers.json, logs, or the public bot-state branch. URLs are stored as
one Fernet-encrypted blob on telegram-bot-state. The encryption key is derived
from TELEGRAM_SOURCE_ENCRYPTION_KEY when configured, otherwise from the existing
TELEGRAM_BOT_TOKEN for zero-setup backwards compatibility.

Network validation rejects local/private/link-local/reserved destinations and
re-validates every HTTP redirect before following it. Source content is bounded
and must contain at least one config understood by the existing parser.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from parser import parse_source

SOURCE_STATE_PATH = "telegram_subscription_sources.json"
SOURCE_STATE_BRANCH = "telegram-bot-state"
SOURCE_STATE_VERSION = 1
MAX_MANAGED_SOURCES = 25
MAX_SOURCE_BYTES = 2_000_000
FETCH_TIMEOUT_SECONDS = 15
USER_AGENT = "Broute-Collector/managed-subscription"


class SourceValidationError(ValueError):
    pass


class SourceCryptoError(RuntimeError):
    pass


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_repeats = 2
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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


def validate_public_url(url: str) -> str:
    normalized = normalize_subscription_url(url)
    parts = urlsplit(normalized)
    host = str(parts.hostname or "")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceValidationError("دامنه‌ی subscription قابل resolve نیست") from exc

    addresses = {info[4][0].split("%", 1)[0] for info in infos if info and info[4]}
    if not addresses:
        raise SourceValidationError("دامنه‌ی subscription هیچ IP معتبری ندارد")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise SourceValidationError("IP منبع معتبر نیست") from exc
        if not ip.is_global:
            raise SourceValidationError(
                "منبعی که به IP خصوصی/local/link-local/reserved وصل شود پذیرفته نمی‌شود"
            )

    return normalized


def fetch_subscription(url: str) -> str:
    normalized = validate_public_url(url)
    opener = urllib.request.build_opener(SafeRedirectHandler())
    request = urllib.request.Request(
        normalized,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain, application/octet-stream, */*;q=0.5",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            # urllib may expose a final redirected URL; validate it again after
            # the connection as a second defensive check.
            validate_public_url(response.geturl())
            data = response.read(MAX_SOURCE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError, SourceValidationError) as exc:
        raise SourceValidationError("دریافت subscription ناموفق بود") from exc

    if len(data) > MAX_SOURCE_BYTES:
        raise SourceValidationError("حجم subscription بیشتر از سقف 2MB است")
    text = data.decode("utf-8", errors="ignore")
    if not text.strip():
        raise SourceValidationError("subscription خالی است")
    return text


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
