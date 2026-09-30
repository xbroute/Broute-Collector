"""
common.py
ابزارهای مشترک برای تمام اسکریپت‌های پروژه: تشخیص پروتکل، پارس کردن لینک‌ها،
تشخیص IP خصوصی/رزروشده، و ساخت شناسه یکتا برای هر کانفیگ.

هیچ اطلاعات حساسی (Token, API Key, پنل مدیریتی) در این ماژول ذخیره یا لاگ نمی‌شود.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import tempfile
import re
import socket
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote, quote

# ---------------------------------------------------------------------------
# پروتکل‌های پشتیبانی‌شده و Scheme متناظرشان
# ---------------------------------------------------------------------------
SUPPORTED_SCHEMES = {
    "vless": "vless",
    "vmess": "vmess",
    "trojan": "trojan",
    "ss": "shadowsocks",
    "hysteria2": "hysteria2",
    "hy2": "hysteria2",
    "tuic": "tuic",
    "wireguard": "wireguard",
    "socks": "socks",
    "socks5": "socks",
    "http": "http",
    "https": "http",
}

# دامنه‌ها/الگوهای مشکوک که کانفیگ‌های حاوی آن‌ها حذف می‌شوند
SUSPICIOUS_DOMAIN_PATTERNS = [
    r"\.local$",
    r"\.internal$",
    r"panel",
    r"admin",
    r"manage",
    r"dashboard",
]

# پارامترهای کوئری‌ای که نشانه توکن مدیریتی/API هستند
MANAGEMENT_TOKEN_KEYS = {"token", "apikey", "api_key", "secret", "adminpass"}

MAX_REASONABLE_PORT = 65535


@dataclass
class ParsedConfig:
    raw: str
    protocol: str
    address: str = ""
    port: int = 0
    uuid_or_password: str = ""
    transport: str = "tcp"
    security: str = "none"  # none | tls | reality
    tls: bool = False
    sni: str = ""
    host: str = ""
    name: str = ""
    insecure: bool = True
    valid: bool = False
    reject_reason: str = ""

    def unique_key(self) -> str:
        """
        شناسه یکتا فقط بر اساس اطلاعات اصلی اتصال ساخته می‌شود، نه نام کانفیگ،
        تا کانفیگ‌های تکراری با نام‌های متفاوت هم تشخیص داده شوند.
        """
        raw_key = "|".join([
            self.protocol,
            self.address.lower(),
            str(self.port),
            self.uuid_or_password,
            self.transport,
            self.host.lower(),
            self.sni.lower(),
        ])
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]


def is_probably_base64(text: str) -> bool:
    """تشخیص تقریبی این‌که آیا محتوای یک فایل به‌صورت کامل Base64 است یا خیر."""
    sample = text.strip().replace("\n", "").replace("\r", "")
    if len(sample) < 20:
        return False
    if re.search(r"://", text):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", sample))


def safe_b64decode(text: str) -> Optional[str]:
    try:
        padded = re.sub(r"\s+", "", text)
        padded += "=" * (-len(padded) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return decoded.decode("utf-8", errors="ignore")
    except Exception:
        return None


def is_private_or_reserved(address: str) -> bool:
    """تشخیص آدرس‌های Localhost، خصوصی و رزروشده."""
    host = address.strip("[]")
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global or ip.is_multicast
    except ValueError:
        # دامنه است نه IP؛ فقط localhost صریح را رد می‌کنیم
        return host.lower() in {"localhost", "0.0.0.0", "127.0.0.1"}


def has_suspicious_domain(address: str) -> bool:
    addr = address.lower()
    return any(re.search(pattern, addr) for pattern in SUSPICIOUS_DOMAIN_PATTERNS)


def has_management_token(url: str) -> bool:
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        return any(key.lower() in MANAGEMENT_TOKEN_KEYS for key in query)
    except Exception:
        return False


def detect_protocol(line: str) -> Optional[str]:
    line = line.strip()
    if not line or "://" not in line:
        return None
    scheme = line.split("://", 1)[0].lower()
    return SUPPORTED_SCHEMES.get(scheme)


def _parse_vmess(line: str) -> ParsedConfig:
    cfg = ParsedConfig(raw=line, protocol="vmess")
    payload = line[len("vmess://"):].split("#", 1)[0]
    decoded = safe_b64decode(payload)
    if not decoded:
        cfg.reject_reason = "vmess base64 decode failed"
        return cfg
    try:
        data = json.loads(decoded)
    except Exception:
        cfg.reject_reason = "vmess json parse failed"
        return cfg
    if not isinstance(data, dict):
        cfg.reject_reason = "vmess payload must be a JSON object"
        return cfg
    cfg.address = str(data.get("add") or "")
    try:
        cfg.port = int(data.get("port", 0))
    except (TypeError, ValueError, OverflowError):
        cfg.port = 0
    cfg.uuid_or_password = str(data.get("id") or "")
    cfg.transport = str(data.get("net", "tcp"))
    cfg.host = str(data.get("host", ""))
    cfg.sni = str(data.get("sni", cfg.host))
    tls_val = str(data.get("tls", "")).lower()
    cfg.tls = tls_val in {"tls", "reality"}
    cfg.security = tls_val if tls_val else "none"
    cfg.name = str(data.get("ps", "")) or f"vmess-{cfg.address}"
    cfg.valid = bool(cfg.address and cfg.port and cfg.uuid_or_password)
    return cfg


def _parse_generic_uri(line: str, protocol: str) -> ParsedConfig:
    """
    پارسر عمومی برای vless / trojan / hysteria2 / tuic که ساختار مشابهی دارند:
    scheme://userinfo@host:port?query#name
    """
    cfg = ParsedConfig(raw=line, protocol=protocol)
    try:
        parsed = urlparse(line)
        cfg.address = parsed.hostname or ""
        port = parsed.port
        cfg.port = port if port is not None else (443 if protocol == "hysteria2" else 0)
        cfg.uuid_or_password = unquote(parsed.username or "")
    except (ValueError, Exception):
        # می‌تونه به‌خاطر IPv6 بدون براکت یا فرمت غیراستاندارد رخ بده؛
        # به‌جای متوقف‌کردن کل اجرا، فقط همین کانفیگ رد می‌شود.
        cfg.reject_reason = "url parse failed (possibly malformed IPv6 address)"
        return cfg

    cfg.name = unquote(parsed.fragment or "") or f"{protocol}-{cfg.address}"

    query = parse_qs(parsed.query)
    default_transport = "quic" if protocol in {"hysteria2", "tuic"} else "tcp"
    cfg.transport = (query.get("type") or query.get("network") or [default_transport])[0]
    default_security = "tls" if protocol in {"trojan", "hysteria2", "tuic"} else "none"
    security = (query.get("security") or [default_security])[0].lower()
    cfg.security = security
    cfg.tls = security in {"tls", "reality"}
    cfg.sni = (query.get("sni") or [""])[0]
    cfg.host = (query.get("host") or [cfg.sni])[0]

    needs_credentials = protocol in {"vless", "trojan", "tuic"}
    cfg.valid = bool(cfg.address and cfg.port and (cfg.uuid_or_password or not needs_credentials))
    return cfg


def shadowsocks_uri_parts(line: str):
    """Decode SIP002 or legacy credentials before parsing the endpoint.

    Legacy Base64 is case-sensitive connection data, never a DNS hostname.
    Percent decoding applies to plain SIP002 userinfo, not a decoded password.
    """
    if "://" not in line:
        raise ValueError("shadowsocks scheme is missing")
    body = line.split("://", 1)[1].split("#", 1)[0]
    if "@" in body:
        userinfo, hostpart = body.rsplit("@", 1)
        userinfo = unquote(userinfo)
        decoded_userinfo = userinfo if ":" in userinfo else safe_b64decode(userinfo)
    else:
        payload, _, query = body.partition("?")
        decoded_all = safe_b64decode(payload)
        if not decoded_all or "@" not in decoded_all:
            raise ValueError("shadowsocks decode failed")
        decoded_userinfo, hostpart = decoded_all.rsplit("@", 1)
        if query:
            hostpart += ("&" if "?" in hostpart else "?") + query

    if not decoded_userinfo or ":" not in decoded_userinfo:
        raise ValueError("shadowsocks missing method:password")
    method, password = decoded_userinfo.split(":", 1)
    endpoint = urlparse("ss://" + hostpart)
    # Accessing port validates range and supports bracketed IPv6.
    if not method or not password or not endpoint.hostname or not endpoint.port:
        raise ValueError("shadowsocks missing connection fields")
    return method, password, endpoint


def _parse_shadowsocks(line: str) -> ParsedConfig:
    cfg = ParsedConfig(raw=line, protocol="shadowsocks")
    try:
        _, cfg.uuid_or_password, endpoint = shadowsocks_uri_parts(line)
        cfg.address = endpoint.hostname or ""
        cfg.port = endpoint.port or 0
    except ValueError as exc:
        cfg.reject_reason = str(exc)
        return cfg

    name = unquote(line.split("#", 1)[1]) if "#" in line else ""
    cfg.name = name or f"ss-{cfg.address}"
    cfg.security = "none"
    cfg.tls = False
    cfg.valid = bool(cfg.address and cfg.port and cfg.uuid_or_password)
    return cfg


def _parse_wireguard(line: str) -> ParsedConfig:
    cfg = ParsedConfig(raw=line, protocol="wireguard")
    try:
        parsed = urlparse(line)
        cfg.address = parsed.hostname or ""
        cfg.port = parsed.port or 0
        cfg.uuid_or_password = unquote(parsed.username or "")
        cfg.name = unquote(parsed.fragment or "") or f"wg-{cfg.address}"
        cfg.security = "none"
        cfg.tls = False
        cfg.valid = bool(cfg.address and cfg.port)
    except Exception:
        cfg.reject_reason = "wireguard parse failed"
    return cfg


def _parse_proxy(line: str, protocol: str) -> ParsedConfig:
    """پارسر برای socks:// و http(s):// که رمزنگاری داخلی ندارند."""
    cfg = ParsedConfig(raw=line, protocol=protocol)
    try:
        parsed = urlparse(line)
        cfg.address = parsed.hostname or ""
        cfg.port = parsed.port or 0
        cfg.uuid_or_password = unquote(parsed.username or "")
        cfg.name = unquote(parsed.fragment or "") or f"{protocol}-{cfg.address}"
        cfg.security = "tls" if protocol == "http" and parsed.scheme == "https" else "none"
        cfg.tls = cfg.security == "tls"
        cfg.valid = bool(cfg.address and cfg.port)
    except Exception:
        cfg.reject_reason = "proxy parse failed"
    return cfg


def parse_config_line(line: str) -> Optional[ParsedConfig]:
    """نقطه ورود اصلی: یک خط کانفیگ خام می‌گیرد و ParsedConfig برمی‌گرداند."""
    line = line.strip()
    protocol = detect_protocol(line)
    if not protocol:
        return None

    if protocol == "vmess":
        cfg = _parse_vmess(line)
    elif protocol == "shadowsocks":
        cfg = _parse_shadowsocks(line)
    elif protocol == "wireguard":
        cfg = _parse_wireguard(line)
    elif protocol in {"socks", "http"}:
        cfg = _parse_proxy(line, protocol)
    else:
        cfg = _parse_generic_uri(line, protocol)

    if not cfg.valid:
        return cfg

    # اعتبارسنجی پورت
    if not (0 < cfg.port <= MAX_REASONABLE_PORT):
        cfg.valid = False
        cfg.reject_reason = "invalid port"
        return cfg

    # فیلترهای امنیتی: آدرس‌های خصوصی/رزروشده/لوکال
    if is_private_or_reserved(cfg.address):
        cfg.valid = False
        cfg.reject_reason = "private or reserved address"
        return cfg

    if has_suspicious_domain(cfg.address):
        cfg.valid = False
        cfg.reject_reason = "suspicious domain"
        return cfg

    if has_management_token(line):
        cfg.valid = False
        cfg.reject_reason = "contains management token"
        return cfg

    # کانفیگ بدون TLS/Reality حذف نمی‌شود اما insecure علامت می‌خورد
    cfg.insecure = not cfg.tls

    return cfg


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config_to_dict(cfg: ParsedConfig) -> dict:
    d = asdict(cfg)
    d["id"] = cfg.unique_key()
    return d


def country_flag(country_code: str) -> str:
    """تبدیل کد دو حرفی کشور (مثل DE) به ایموجی پرچم. اگر نامعتبر بود، رشته خالی برمی‌گرداند."""
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


def rename_raw_config(raw: str, protocol: str, display_name: str) -> str:
    """
    نام نمایشی کانفیگ (بخشی که در اپ‌های کلاینت دیده می‌شود) را با display_name
    جایگزین می‌کند. برای vmess نام داخل JSON بازنویسی می‌شود؛ برای بقیه پروتکل‌ها
    بخش fragment (بعد از #) بازنویسی می‌شود. در صورت هر خطایی، raw اصلی بدون تغییر برمی‌گردد.
    """
    try:
        if protocol == "vmess":
            payload = raw[len("vmess://"):]
            # بعضی کانفیگ‌های vmess یک #نام بیرون از Base64 هم دارند
            # (فرمت غیررسمی ولی رایج) که باید قبل از Decode حذف شود.
            payload = payload.split("#", 1)[0]
            decoded = safe_b64decode(payload)
            if not decoded:
                return raw
            data = json.loads(decoded)
            data["ps"] = display_name
            new_json = json.dumps(data, ensure_ascii=False)
            new_b64 = base64.b64encode(new_json.encode("utf-8")).decode("utf-8")
            return f"vmess://{new_b64}"
        else:
            base = raw.split("#", 1)[0]
            return f"{base}#{quote(display_name)}"
    except Exception:
        return raw
