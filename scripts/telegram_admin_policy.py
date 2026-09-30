"""Pure administration policy shared by the private panel and publisher.

No Telegram/GitHub calls belong here. Legacy destinations retain unrestricted
publishing unless an administrator explicitly configures a policy.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

ROLES = {"owner", "admin", "operator", "viewer"}
ROLE_PERMISSIONS = {
    "owner": {"view", "operate", "configure", "team", "backup"},
    "admin": {"view", "operate", "configure"},
    "operator": {"view", "operate"},
    "viewer": {"view"},
}
MAX_ADMINS = 50
MAX_AUDIT = 200
MAX_CONFIRMATIONS = 30
MAX_SESSIONS = 50
MAX_BLOCKED = 500
MAX_BACKUP_BYTES = 256_000
CONFIRM_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 20 * 60
PROTOCOLS = {"vless", "vmess", "trojan", "ss", "hysteria2", "hy2", "tuic"}
TRANSPORTS = {"tcp", "ws", "grpc", "http", "h2", "httpupgrade", "xhttp", "quic", "kcp"}
POLICY_FIELDS = ("filters", "schedule", "daily_limit", "pause_until", "retry_generation", "queue_revision", "delivery_resolutions")


class PolicyError(ValueError):
    pass


def integer(value: Any, low: int = 0, high: int = 2**53 - 1) -> int:
    try:
        if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
            raise ValueError
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PolicyError("مقدار عددی معتبر نیست") from None
    if not low <= result <= high:
        raise PolicyError(f"عدد باید بین {low} و {high} باشد")
    return result


def owner_ids_from_env() -> set[int]:
    raw = os.environ.get("TELEGRAM_OWNER_USER_IDS", "").strip()
    return {integer(item, 1) for item in re.split(r"[,\s]+", raw) if item}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def normalize_admin(raw: Any, legacy_managers: list[int]) -> dict:
    if not isinstance(raw, dict):
        raise PolicyError("ساختار تنظیمات مدیریت معتبر نیست")
    if raw.get("version", 1) != 1:
        raise PolicyError("نسخه تنظیمات مدیریت پشتیبانی نمی‌شود")
    roles = raw.get("roles", {str(uid): "owner" for uid in legacy_managers})
    if not isinstance(roles, dict) or len(roles) > MAX_ADMINS:
        raise PolicyError("فهرست مدیران معتبر نیست")
    normalized_roles = {}
    for uid, role in roles.items():
        user_id = integer(uid, 1)
        if role not in ROLES:
            raise PolicyError("نقش مدیر معتبر نیست")
        normalized_roles[str(user_id)] = role
    result = {"version": 1, "roles": normalized_roles,
              "pause_until": integer(raw.get("pause_until", 0)),
              "blocked_ids": [], "audit": [], "confirmations": {}, "sessions": {}, "versions": {}}
    blocked = raw.get("blocked_ids", [])
    if not isinstance(blocked, list) or len(blocked) > MAX_BLOCKED:
        raise PolicyError("فهرست مسدودی معتبر نیست")
    for server_id in blocked:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(server_id)):
            raise PolicyError("شناسه کانفیگ معتبر نیست")
        if server_id not in result["blocked_ids"]:
            result["blocked_ids"].append(server_id)
    audit = raw.get("audit", [])
    if not isinstance(audit, list):
        raise PolicyError("سابقه مدیریت معتبر نیست")
    for entry in audit[-MAX_AUDIT:]:
        if not isinstance(entry, dict):
            raise PolicyError("رویداد مدیریت معتبر نیست")
        # Only opaque identifiers/actions are recorded; never command arguments.
        result["audit"].append({"at": integer(entry.get("at", 0)),
                                "actor": integer(entry.get("actor", 0)),
                                "action": str(entry.get("action", ""))[:64],
                                "resource": str(entry.get("resource", ""))[:128]})
    for name, limit in (("confirmations", MAX_CONFIRMATIONS), ("sessions", MAX_SESSIONS)):
        items = raw.get(name, {})
        if not isinstance(items, dict) or len(items) > limit:
            raise PolicyError("تعداد درخواست‌های مدیریت معتبر نیست")
        if len(json.dumps(items).encode()) > MAX_BACKUP_BYTES:
            raise PolicyError("درخواست مدیریت بیش از حد بزرگ است")
        result[name] = copy.deepcopy(items)
    versions = raw.get("versions", {})
    if not isinstance(versions, dict) or len(versions) > 20:
        raise PolicyError("سابقه تنظیمات مقصد معتبر نیست")
    for key, items in versions.items():
        if not isinstance(items, list) or len(items) > 8:
            raise PolicyError("سابقه تنظیمات مقصد معتبر نیست")
        result["versions"][str(key)] = copy.deepcopy(items)
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BACKUP_BYTES:
        raise PolicyError("حجم تنظیمات مدیریت بیش از حد مجاز است")
    return result


def administration(store: dict) -> dict:
    return normalize_admin(store.get("administration", {}), store.get("manager_user_ids", []))


def effective_roles(store: dict, env_owners: set[int] | None = None) -> dict[str, str]:
    roles = dict(administration(store)["roles"])
    for uid in owner_ids_from_env() if env_owners is None else env_owners:
        roles[str(uid)] = "owner"
    return roles


def allowed(store: dict, user_id: int, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(effective_roles(store).get(str(user_id)), set())


def audit(admin: dict, actor: int, action: str, resource: str, now: int) -> None:
    if not re.fullmatch(r"[a-z_]{1,64}", action) or not re.fullmatch(r"[A-Za-z0-9_:-]{0,128}", resource):
        raise PolicyError("رویداد قابل ثبت نیست")
    admin["audit"] = (admin["audit"] + [{"at": now, "actor": actor,
                                          "action": action, "resource": resource}])[-MAX_AUDIT:]


def _values(raw: Any, valid: set[str] | None, upper: bool = False) -> list[str]:
    if not isinstance(raw, list):
        raise PolicyError("فهرست فیلتر معتبر نیست")
    values = list(dict.fromkeys(str(v).upper() if upper else str(v).lower() for v in raw))
    if len(values) > 250 or any(v not in valid if valid is not None else
                               re.fullmatch(r"[A-Z]{2}", v) is None for v in values):
        raise PolicyError("مقدار فیلتر پشتیبانی نمی‌شود")
    return values


def normalize_policy(item: dict) -> dict:
    filters = item.get("filters", {})
    if not isinstance(filters, dict) or set(filters) - {"protocols", "countries", "transports", "tls_only", "max_latency_ms"}:
        raise PolicyError("ساختار فیلتر معتبر نیست")
    tls = filters.get("tls_only", False)
    if not isinstance(tls, bool):
        raise PolicyError("TLS باید روشن یا خاموش باشد")
    result = {"filters": {"protocols": _values(filters.get("protocols", []), PROTOCOLS),
                           "countries": _values(filters.get("countries", []), None, True),
                           "transports": _values(filters.get("transports", []), TRANSPORTS),
                           "tls_only": tls,
                           "max_latency_ms": integer(filters.get("max_latency_ms", 0), 0, 60_000)},
              "daily_limit": integer(item.get("daily_limit", 0), 0, 1000),
              "pause_until": integer(item.get("pause_until", 0)),
              "retry_generation": integer(item.get("retry_generation", 0)),
              "queue_revision": integer(item.get("queue_revision", 0)),
              "schedule": None, "delivery_resolutions": {}}
    resolutions = item.get("delivery_resolutions", {})
    if not isinstance(resolutions, dict) or len(resolutions) > 100:
        raise PolicyError("فهرست تعیین تکلیف ارسال معتبر نیست")
    for server_id, entry in resolutions.items():
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(server_id)) or not isinstance(entry, dict)
                or entry.get("decision") not in {"sent", "retry"}
                or not re.fullmatch(r"[a-f0-9]{16}", str(entry.get("nonce", "")))):
            raise PolicyError("درخواست تعیین تکلیف ارسال معتبر نیست")
        result["delivery_resolutions"][str(server_id)] = {"decision": entry["decision"], "nonce": entry["nonce"]}
    schedule = item.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict) or set(schedule) - {"start", "end", "utc_offset_minutes"}:
            raise PolicyError("ساختار زمان‌بندی معتبر نیست")
        start = integer(schedule.get("start"), 0, 1439)
        end = integer(schedule.get("end"), 0, 1439)
        if start == end:
            raise PolicyError("شروع و پایان یکسان نیست؛ برای ارسال دائمی از off استفاده کنید")
        result["schedule"] = {"start": start, "end": end,
                              "utc_offset_minutes": integer(schedule.get("utc_offset_minutes", 210), -720, 840)}
    return result


def parse_filters(text: str, current: dict) -> dict:
    result = copy.deepcopy(current)
    parts = text.split()
    if not parts:
        raise PolicyError("مثال: protocol=vless,trojan country=DE,NL tls=on latency=250")
    mapping = {"protocol": "protocols", "country": "countries", "transport": "transports",
               "tls": "tls_only", "latency": "max_latency_ms"}
    for part in parts:
        name, sep, value = part.partition("=")
        if not sep or name not in mapping or not value:
            raise PolicyError("فیلتر معتبر نیست؛ از protocol/country/transport/tls/latency استفاده کنید")
        field = mapping[name]
        if name == "tls":
            if value.lower() not in {"on", "off"}:
                raise PolicyError("tls=on یا tls=off")
            result[field] = value.lower() == "on"
        elif name == "latency":
            result[field] = integer(value, 0, 60_000)
        else:
            result[field] = [] if value in {"*", "any"} else value.split(",")
    return normalize_policy({"filters": result})["filters"]


def parse_schedule(text: str) -> dict | None:
    if text.strip().lower() in {"off", "always"}:
        return None
    match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})(?:\s+UTC([+-])(\d{2}):(\d{2}))?", text.strip(), re.I)
    if not match:
        raise PolicyError("مثال زمان‌بندی: 09:00-23:00 UTC+03:30 یا off")
    h1, m1, h2, m2 = [int(v) for v in match.groups()[:4]]
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise PolicyError("ساعت معتبر نیست")
    offset = 210
    if match.group(5):
        if int(match.group(7)) > 59:
            raise PolicyError("اختلاف ساعت معتبر نیست")
        offset = (int(match.group(6)) * 60 + int(match.group(7))) * (1 if match.group(5) == "+" else -1)
    return normalize_policy({"schedule": {"start": h1 * 60 + m1, "end": h2 * 60 + m2,
                                           "utc_offset_minutes": offset}})["schedule"]


def duration(text: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd])", text.strip().lower())
    if not match:
        raise PolicyError("مدت معتبر نیست؛ مثال: 30m یا 2h یا 1d")
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return integer(seconds, 15, 30 * 86400)


def server_matches(server: dict, destination: dict, blocked: set[str] | None = None) -> bool:
    if str(server.get("id", "")) in (blocked or set()):
        return False
    filters = normalize_policy(destination)["filters"]
    protocol = str(server.get("protocol", "")).lower()
    if protocol == "hy2":
        protocol = "hysteria2"
    protocols = ["hysteria2" if p == "hy2" else p for p in filters["protocols"]]
    if protocols and protocol not in protocols:
        return False
    if filters["countries"] and str(server.get("country", "")).upper() not in filters["countries"]:
        return False
    if filters["transports"] and str(server.get("transport") or "tcp").lower() not in filters["transports"]:
        return False
    if filters["tls_only"] and server.get("tls") is not True:
        return False
    if filters["max_latency_ms"]:
        try:
            latency = float(server["latency"])
        except (KeyError, ValueError, TypeError, OverflowError):
            return False
        if not math.isfinite(latency) or latency < 0 or latency > filters["max_latency_ms"]:
            return False
    return True


def local_day(destination: dict, now: float) -> str:
    schedule = destination.get("schedule") or {}
    offset = schedule.get("utc_offset_minutes", 210)
    return datetime.fromtimestamp(now, timezone.utc).astimezone(timezone(timedelta(minutes=offset))).date().isoformat()


def next_allowed_at(destination: dict, target: dict, now: float, global_pause: int = 0) -> float:
    policy = normalize_policy(destination)
    due = max(now, float(policy["pause_until"]), float(global_pause),
              float(target.get("next_send_after", 0) or 0), float(target.get("suspended_until", 0) or 0))
    schedule = policy["schedule"]
    tz = timezone(timedelta(minutes=(schedule or {}).get("utc_offset_minutes", 210)))
    day = local_day(destination, due)
    if policy["daily_limit"] and target.get("daily_date") == day and int(target.get("daily_sent", 0)) >= policy["daily_limit"]:
        dt = datetime.fromtimestamp(due, tz)
        due = (dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    if schedule:
        dt = datetime.fromtimestamp(due, tz)
        minute = dt.hour * 60 + dt.minute
        start, end = schedule["start"], schedule["end"]
        inside = start <= minute < end if start < end else minute >= start or minute < end
        if not inside:
            opening = dt.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
            if opening.timestamp() <= due:
                opening += timedelta(days=1)
            due = opening.timestamp()
    return due


def record_send(target: dict, destination: dict, now: float) -> None:
    day = local_day(destination, now)
    target["daily_sent"] = (int(target.get("daily_sent", 0)) if target.get("daily_date") == day else 0) + 1
    target["daily_date"] = day
    target["total_sent"] = int(target.get("total_sent", 0)) + 1
    target["last_sent_at"] = int(now)
    target["consecutive_failures"] = 0


def issue_confirmation(admin: dict, *, actor: int, chat: int, action: str,
                       payload: dict, resource_digest: str, now: int) -> str:
    admin["confirmations"] = {key: value for key, value in admin["confirmations"].items()
                              if isinstance(value, dict) and int(value.get("expires", 0)) > now}
    if len(admin["confirmations"]) >= MAX_CONFIRMATIONS:
        raise PolicyError("درخواست‌های تأیید زیاد است؛ چند دقیقه صبر کنید")
    token = secrets.token_hex(8)
    admin["confirmations"][token] = {"actor": actor, "chat": chat, "action": action,
                                       "payload": copy.deepcopy(payload), "digest": resource_digest,
                                       "expires": now + CONFIRM_TTL_SECONDS}
    return token


def confirmation(admin: dict, token: str, actor: int, chat: int, now: int) -> dict:
    item = admin["confirmations"].get(token)
    if not isinstance(item, dict) or item.get("actor") != actor or item.get("chat") != chat or int(item.get("expires", 0)) <= now:
        raise PolicyError("این تأیید معتبر نیست یا منقضی شده است؛ دستور را دوباره بفرستید")
    return copy.deepcopy(item)


def _backup_fernet(secret: str) -> Fernet:
    if not secret:
        raise PolicyError("کلید پشتیبان تنظیم نشده است")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"broute-admin-backup-v1\0" + secret.encode()).digest()))


def encrypt_backup(payload: dict, secret: str) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_BACKUP_BYTES:
        raise PolicyError("حجم پشتیبان بیش از حد مجاز است")
    return b"BROUTE-BACKUP-1\n" + _backup_fernet(secret).encrypt(raw)


def decrypt_backup(data: bytes, candidates: list[str]) -> dict:
    if len(data) > MAX_BACKUP_BYTES * 2 or not data.startswith(b"BROUTE-BACKUP-1\n"):
        raise PolicyError("فایل پشتیبان معتبر نیست یا بیش از حد بزرگ است")
    for secret in dict.fromkeys(candidates):
        if not secret:
            continue
        try:
            raw = _backup_fernet(secret).decrypt(data.split(b"\n", 1)[1])
            if len(raw) > MAX_BACKUP_BYTES:
                raise PolicyError("حجم پشتیبان بیش از حد مجاز است")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("version") != 1:
                raise PolicyError("نسخه پشتیبان معتبر نیست")
            return value
        except (InvalidToken, ValueError, UnicodeError):
            continue
    raise PolicyError("پشتیبان با کلیدهای فعلی قابل بازگشایی نیست")
