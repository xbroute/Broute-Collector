"""Encrypted Telegram destination registry for multi-destination publishing."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any, Dict, Iterable, List

from cryptography.fernet import Fernet, InvalidToken
from telegram_managed_sources import source_secret

DESTINATION_STATE_PATH = "telegram_destinations.json"
DESTINATION_STATE_BRANCH = "telegram-bot-state"
DESTINATION_STATE_VERSION = 1
DEFAULT_MIN_DELAY = 30
DEFAULT_MAX_DELAY = 90
MIN_DELAY_SECONDS = 10
MAX_DELAY_SECONDS = 86400


class DestinationCryptoError(RuntimeError):
    pass


class DestinationValidationError(ValueError):
    pass


def _fernet(secret: str | None = None) -> Fernet:
    value = (secret if secret is not None else source_secret()).encode("utf-8")
    digest = hashlib.sha256(b"broute-telegram-destinations-v1\0" + value).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def default_state() -> Dict[str, Any]:
    return {
        "owners": [],
        "default_min_delay": DEFAULT_MIN_DELAY,
        "default_max_delay": DEFAULT_MAX_DELAY,
        "destinations": [],
    }


def normalize_state(data: Dict[str, Any] | None) -> Dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    owners = []
    for value in data.get("owners", []):
        try:
            owner = int(value)
        except (TypeError, ValueError):
            continue
        if owner > 0 and owner not in owners:
            owners.append(owner)

    try:
        default_min = int(data.get("default_min_delay", DEFAULT_MIN_DELAY))
        default_max = int(data.get("default_max_delay", DEFAULT_MAX_DELAY))
        default_min, default_max = validate_delay(default_min, default_max)
    except (TypeError, ValueError, DestinationValidationError):
        default_min, default_max = DEFAULT_MIN_DELAY, DEFAULT_MAX_DELAY

    destinations: List[Dict[str, Any]] = []
    seen = set()
    for raw in data.get("destinations", []):
        if not isinstance(raw, dict):
            continue
        try:
            chat_id = int(raw.get("chat_id"))
        except (TypeError, ValueError):
            continue
        thread_id = raw.get("thread_id")
        try:
            thread_id = int(thread_id) if thread_id is not None else None
        except (TypeError, ValueError):
            thread_id = None
        dest_id = str(raw.get("id") or destination_id(chat_id, thread_id))
        if not dest_id or dest_id in seen:
            continue
        seen.add(dest_id)
        try:
            min_delay, max_delay = validate_delay(
                int(raw.get("min_delay", default_min)),
                int(raw.get("max_delay", default_max)),
            )
        except (TypeError, ValueError, DestinationValidationError):
            min_delay, max_delay = default_min, default_max

        destinations.append(
            {
                "id": dest_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "type": str(raw.get("type") or "unknown")[:32],
                "title": str(raw.get("title") or "بدون نام")[:160],
                "username": str(raw.get("username") or "")[:64],
                "enabled": raw.get("enabled") is True,
                "status": str(raw.get("status") or "ready")[:64],
                "min_delay": min_delay,
                "max_delay": max_delay,
                "added_at": str(raw.get("added_at") or ""),
                "updated_at": str(raw.get("updated_at") or ""),
            }
        )

    return {
        "owners": owners,
        "default_min_delay": default_min,
        "default_max_delay": default_max,
        "destinations": destinations,
    }


def encrypt_state(state: Dict[str, Any], secret: str | None = None) -> Dict[str, Any]:
    raw = json.dumps(normalize_state(state), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "version": DESTINATION_STATE_VERSION,
        "ciphertext": _fernet(secret).encrypt(raw).decode("ascii"),
    }


def decrypt_state(payload: Dict[str, Any], secret: str | None = None) -> Dict[str, Any]:
    if not payload:
        return default_state()
    if int(payload.get("version", 0) or 0) != DESTINATION_STATE_VERSION:
        raise DestinationCryptoError("unsupported Telegram destination state version")
    ciphertext = str(payload.get("ciphertext") or "")
    if not ciphertext:
        return default_state()
    try:
        decoded = _fernet(secret).decrypt(ciphertext.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise DestinationCryptoError(
            "Telegram destination state could not be decrypted; check the encryption secret"
        ) from exc
    if not isinstance(data, dict):
        raise DestinationCryptoError("Telegram destination plaintext has invalid structure")
    return normalize_state(data)


def load_state_file(path: str, secret: str | None = None) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise DestinationCryptoError("Telegram destination state file is invalid") from exc
    if not isinstance(payload, dict):
        raise DestinationCryptoError("Telegram destination state file has invalid structure")
    return decrypt_state(payload, secret)


def destination_id(chat_id: int, thread_id: int | None = None) -> str:
    return hashlib.sha256(f"{int(chat_id)}:{int(thread_id or 0)}".encode("utf-8")).hexdigest()[:12]


def validate_delay(min_delay: int, max_delay: int) -> tuple[int, int]:
    try:
        low = int(min_delay)
        high = int(max_delay)
    except (TypeError, ValueError) as exc:
        raise DestinationValidationError("بازه زمانی باید عدد صحیح بر حسب ثانیه باشد") from exc
    if low < MIN_DELAY_SECONDS:
        raise DestinationValidationError(f"حداقل فاصله نمی‌تواند کمتر از {MIN_DELAY_SECONDS} ثانیه باشد")
    if high > MAX_DELAY_SECONDS:
        raise DestinationValidationError(f"حداکثر فاصله نمی‌تواند بیشتر از {MAX_DELAY_SECONDS} ثانیه باشد")
    if high < low:
        raise DestinationValidationError("حداکثر فاصله باید بزرگ‌تر یا مساوی حداقل باشد")
    return low, high


def active_destinations(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    normalized = normalize_state(state)
    return [
        dict(item)
        for item in normalized["destinations"]
        if item.get("enabled") is True and item.get("status") == "ready"
    ]


def find_destination(state: Dict[str, Any], selector: str) -> Dict[str, Any] | None:
    destinations = normalize_state(state)["destinations"]
    token = str(selector or "").strip()
    if not token:
        return None
    try:
        index = int(token)
        if 1 <= index <= len(destinations):
            return destinations[index - 1]
    except ValueError:
        pass
    matches = [item for item in destinations if str(item.get("id") or "").startswith(token)]
    return matches[0] if len(matches) == 1 else None


def upsert_destination(
    state: Dict[str, Any],
    *,
    chat_id: int,
    chat_type: str,
    title: str,
    username: str = "",
    thread_id: int | None = None,
    ready: bool = True,
    preserve_enabled: bool = True,
) -> Dict[str, Any]:
    normalized = normalize_state(state)
    dest_id = destination_id(chat_id, thread_id)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    existing = next((item for item in normalized["destinations"] if item["id"] == dest_id), None)

    if existing is None:
        existing = {
            "id": dest_id,
            "chat_id": int(chat_id),
            "thread_id": int(thread_id) if thread_id is not None else None,
            "type": str(chat_type or "unknown"),
            "title": str(title or "بدون نام"),
            "username": str(username or ""),
            "enabled": False,
            "status": "ready" if ready else "missing_permission",
            "min_delay": normalized["default_min_delay"],
            "max_delay": normalized["default_max_delay"],
            "added_at": now,
            "updated_at": now,
        }
        normalized["destinations"].append(existing)
    else:
        was_enabled = existing.get("enabled") is True
        existing.update(
            {
                "chat_id": int(chat_id),
                "thread_id": int(thread_id) if thread_id is not None else None,
                "type": str(chat_type or existing.get("type") or "unknown"),
                "title": str(title or existing.get("title") or "بدون نام"),
                "username": str(username or existing.get("username") or ""),
                "status": "ready" if ready else "missing_permission",
                "updated_at": now,
            }
        )
        if not ready:
            existing["enabled"] = False
        elif preserve_enabled:
            existing["enabled"] = was_enabled

    state.clear()
    state.update(normalize_state(normalized))
    return next(item for item in state["destinations"] if item["id"] == dest_id)
