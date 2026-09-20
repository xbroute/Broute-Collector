"""Encrypted multi-destination configuration for Telegram publishing."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from string import Formatter
from typing import Any, Dict, List, Tuple

from cryptography.fernet import Fernet, InvalidToken

DESTINATION_STATE_PATH = "telegram_destinations.json"
DESTINATION_STATE_BRANCH = "telegram-bot-state"
DESTINATION_STATE_VERSION = 1
MAX_DESTINATIONS = 20
MIN_DELAY_SECONDS = 15
MAX_DELAY_SECONDS = 6 * 60 * 60
MAX_TEMPLATE_CHARS = 3000

DEFAULT_TEMPLATE = """🟢 کانفیگ رایگان

{flag} کشور: {country}
🔹 پروتکل: {protocol}
{security}
🔌 شبکه: {network}
⚡ تأخیر تست: {latency}

{config}

🔗 {brand}

🔄 لینک سابسکریپشن همیشه‌به‌روز
{subscription_url}"""

ALLOWED_TEMPLATE_FIELDS = {
    "flag",
    "country",
    "protocol",
    "security",
    "network",
    "latency",
    "config",
    "brand",
    "subscription_url",
    "destination",
}

_INTERVAL_RE = re.compile(
    r"^\s*(\d+)\s*([smhSMH]?)\s*(?:-\s*(\d+)\s*([smhSMH]?))?\s*$"
)


class DestinationStateError(RuntimeError):
    pass


class DestinationValidationError(ValueError):
    pass


def _secret() -> str:
    dedicated = os.environ.get("TELEGRAM_DESTINATION_ENCRYPTION_KEY", "").strip()
    if dedicated:
        return dedicated
    shared = os.environ.get("TELEGRAM_SOURCE_ENCRYPTION_KEY", "").strip()
    if shared:
        return shared
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise DestinationStateError(
            "TELEGRAM_DESTINATION_ENCRYPTION_KEY, TELEGRAM_SOURCE_ENCRYPTION_KEY "
            "or TELEGRAM_BOT_TOKEN is required"
        )
    return token


def _fernet(secret: str | None = None) -> Fernet:
    raw = (secret if secret is not None else _secret()).encode("utf-8")
    digest = hashlib.sha256(b"broute-telegram-destinations-v1\0" + raw).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def default_store() -> Dict[str, Any]:
    return {"manager_user_ids": [], "destinations": []}


def encrypt_store(store: Dict[str, Any], secret: str | None = None) -> Dict[str, Any]:
    normalized = normalize_store(store)
    raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "version": DESTINATION_STATE_VERSION,
        "ciphertext": _fernet(secret).encrypt(raw).decode("ascii"),
    }


def decrypt_store(payload: Dict[str, Any], secret: str | None = None) -> Dict[str, Any]:
    if not payload:
        return default_store()
    if int(payload.get("version", 0) or 0) != DESTINATION_STATE_VERSION:
        raise DestinationStateError("unsupported Telegram destination state version")
    token = str(payload.get("ciphertext") or "")
    if not token:
        return default_store()
    try:
        raw = _fernet(secret).decrypt(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise DestinationStateError(
            "Telegram destination state could not be decrypted"
        ) from exc
    if not isinstance(data, dict):
        raise DestinationStateError("Telegram destination plaintext is invalid")
    return normalize_store(data)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def destination_key(chat_id: int | str) -> str:
    return hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:10]


def normalize_template(template: str) -> str:
    value = str(template or "")
    if not value.strip():
        raise DestinationValidationError("قالب پیام نباید خالی باشد")
    if len(value) > MAX_TEMPLATE_CHARS:
        raise DestinationValidationError(
            f"طول قالب بیشتر از {MAX_TEMPLATE_CHARS} کاراکتر است"
        )

    fields: List[str] = []
    try:
        for _, field_name, format_spec, conversion in Formatter().parse(value):
            if field_name is None:
                continue
            if format_spec or conversion:
                raise DestinationValidationError(
                    "format specifier/conversion در قالب پشتیبانی نمی‌شود"
                )
            fields.append(field_name)
    except ValueError as exc:
        raise DestinationValidationError(f"ساختار آکولادهای قالب معتبر نیست: {exc}") from exc

    unknown = sorted(set(fields) - ALLOWED_TEMPLATE_FIELDS)
    if unknown:
        raise DestinationValidationError(
            "placeholder ناشناخته: " + ", ".join("{" + item + "}" for item in unknown)
        )
    if fields.count("config") != 1:
        raise DestinationValidationError("قالب باید دقیقاً یک {config} داشته باشد")
    return value


def _unit_multiplier(unit: str) -> int:
    return {"": 1, "s": 1, "m": 60, "h": 3600}[unit.lower()]


def parse_interval_spec(spec: str) -> Tuple[int, int]:
    match = _INTERVAL_RE.match(str(spec or ""))
    if not match:
        raise DestinationValidationError(
            "فاصله معتبر نیست؛ مثال: 60 یا 30-90 یا 2m یا 2m-4m"
        )

    first = int(match.group(1)) * _unit_multiplier(match.group(2) or "")
    second_raw = match.group(3)
    if second_raw is None:
        low = max(MIN_DELAY_SECONDS, int(round(first * 0.8)))
        high = max(low, int(round(first * 1.2)))
    else:
        second_unit = match.group(4) or match.group(2) or ""
        second = int(second_raw) * _unit_multiplier(second_unit)
        low, high = sorted((first, second))

    if low < MIN_DELAY_SECONDS:
        raise DestinationValidationError(
            f"حداقل فاصله مجاز {MIN_DELAY_SECONDS} ثانیه است"
        )
    if high > MAX_DELAY_SECONDS:
        raise DestinationValidationError(
            f"حداکثر فاصله مجاز {MAX_DELAY_SECONDS // 3600} ساعت است"
        )
    return low, high


def format_interval(low: int, high: int) -> str:
    def fmt(value: int) -> str:
        if value % 3600 == 0:
            return f"{value // 3600}h"
        if value % 60 == 0:
            return f"{value // 60}m"
        return f"{value}s"
    return fmt(low) if low == high else f"{fmt(low)}–{fmt(high)}"


def normalize_destination(item: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = _as_int(item.get("chat_id"))
    if chat_id == 0:
        raise DestinationStateError("destination chat_id is invalid")

    low = max(MIN_DELAY_SECONDS, _as_int(item.get("min_delay_seconds"), 30))
    high = max(low, _as_int(item.get("max_delay_seconds"), 90))
    high = min(high, MAX_DELAY_SECONDS)
    low = min(low, high)

    template = str(item.get("template") or DEFAULT_TEMPLATE)
    try:
        template = normalize_template(template)
    except DestinationValidationError:
        template = DEFAULT_TEMPLATE

    thread_raw = item.get("message_thread_id")
    thread_id = _as_int(thread_raw) if thread_raw not in (None, "", 0, "0") else None

    return {
        "key": str(item.get("key") or destination_key(chat_id)),
        "chat_id": chat_id,
        "title": str(item.get("title") or f"chat {chat_id}")[:200],
        "username": str(item.get("username") or "")[:100],
        "chat_type": str(item.get("chat_type") or "")[:32],
        "bot_status": str(item.get("bot_status") or "unknown")[:32],
        "enabled": item.get("enabled") is True,
        "min_delay_seconds": low,
        "max_delay_seconds": high,
        "template": template,
        "message_thread_id": thread_id,
        "discovered_at": str(item.get("discovered_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
    }


def normalize_store(store: Dict[str, Any]) -> Dict[str, Any]:
    managers: List[int] = []
    seen_managers = set()
    for raw in store.get("manager_user_ids", []):
        value = _as_int(raw)
        if value and value not in seen_managers:
            seen_managers.add(value)
            managers.append(value)

    destinations: List[Dict[str, Any]] = []
    seen_chats = set()
    for raw in store.get("destinations", []):
        if not isinstance(raw, dict):
            continue
        try:
            item = normalize_destination(raw)
        except DestinationStateError:
            continue
        if item["chat_id"] in seen_chats:
            continue
        seen_chats.add(item["chat_id"])
        destinations.append(item)

    return {"manager_user_ids": managers, "destinations": destinations[:MAX_DESTINATIONS]}


def find_destination(
    destinations: List[Dict[str, Any]], selector: str
) -> Tuple[int, Dict[str, Any]] | Tuple[None, None]:
    value = str(selector or "").strip()
    if not value:
        return None, None

    try:
        index = int(value)
        if 1 <= index <= len(destinations):
            return index - 1, destinations[index - 1]
    except ValueError:
        pass

    lowered = value.lower()
    for index, item in enumerate(destinations):
        if str(item.get("key") or "").lower().startswith(lowered):
            return index, item
        if str(item.get("chat_id")) == value:
            return index, item
        username = str(item.get("username") or "").lstrip("@").lower()
        if username and username == lowered.lstrip("@"):
            return index, item
    return None, None


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
