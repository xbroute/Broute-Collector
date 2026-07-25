"""
validator.py
بررسی اولیه سرورها: Resolve شدن دامنه، باز بودن پورت TCP، مدت‌زمان پاسخ،
و تشخیص تقریبی کشور از روی IP.

این اسکریپت هیچ تست نفوذ، اسکن گسترده یا حمله‌ای انجام نمی‌دهد؛ فقط یک اتصال
TCP کوتاه برای بررسی زنده‌بودن سرور برقرار و بلافاصله بسته می‌شود.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.request
import urllib.error
from typing import Dict, Optional

CHECK_TIMEOUT_SECONDS = 3
MAX_CONSECUTIVE_FAILURES = 3

# سرویس رایگان و عمومی برای تشخیص تقریبی کشور از روی IP.
# این تشخیص قطعی نیست و صرفاً یک تخمین است؛ در صورت شکست، "Unknown" برگردانده می‌شود.
GEOIP_API_TEMPLATE = "http://ip-api.com/json/{ip}?fields=status,countryCode,country"


def resolve_and_check_tcp(address: str, port: int) -> Dict:
    """DNS resolve و بررسی باز بودن پورت TCP. حمله یا اسکن گسترده انجام نمی‌شود."""
    result = {"resolved": False, "online": False, "latency_ms": None, "ip": None}
    try:
        start = time.time()
        ip = socket.gethostbyname(address)
        result["resolved"] = True
        result["ip"] = ip

        with socket.create_connection((ip, port), timeout=CHECK_TIMEOUT_SECONDS):
            elapsed_ms = int((time.time() - start) * 1000)
            result["online"] = True
            result["latency_ms"] = elapsed_ms
    except (socket.gaierror, socket.timeout, OSError):
        pass
    return result


def lookup_country(ip: str) -> Dict[str, str]:
    """تشخیص تقریبی کشور. در صورت هرگونه خطا مقدار Unknown برگردانده می‌شود."""
    try:
        req = urllib.request.Request(GEOIP_API_TEMPLATE.format(ip=ip))
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("status") == "success":
                return {
                    "country": data.get("countryCode", "XX"),
                    "country_name": data.get("country", "Unknown"),
                }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        pass
    return {"country": "XX", "country_name": "Unknown"}


def validate_server(server: Dict, previous_state: Optional[Dict] = None) -> Dict:
    """
    یک رکورد سرور (دیکشنری سازگار با servers.json) را بررسی و آمار آن را
    به‌روزرسانی می‌کند. previous_state آمار قبلی همان کانفیگ (بر اساس id) است.
    """
    check = resolve_and_check_tcp(server["address"], server["port"])
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    prev = previous_state or {}
    success_count = prev.get("success_count", 0)
    fail_count = prev.get("fail_count", 0)
    consecutive_failures = prev.get("consecutive_failures", 0)

    if check["online"]:
        success_count += 1
        consecutive_failures = 0
        server["status"] = "online"
        server["latency"] = check["latency_ms"]
        server["last_seen"] = now_iso
    else:
        fail_count += 1
        consecutive_failures += 1
        server["status"] = "offline"
        server["latency"] = None

    server["last_checked"] = now_iso
    server["success_count"] = success_count
    server["fail_count"] = fail_count
    server["consecutive_failures"] = consecutive_failures
    server["first_seen"] = prev.get("first_seen", now_iso)

    if check["resolved"] and check["ip"] and not prev.get("country"):
        geo = lookup_country(check["ip"])
        server["country"] = geo["country"]
        server["country_name"] = geo["country_name"]
    elif prev.get("country"):
        server["country"] = prev["country"]
        server["country_name"] = prev.get("country_name", "Unknown")
    else:
        server["country"] = "XX"
        server["country_name"] = "Unknown"

    server["should_remove"] = consecutive_failures >= MAX_CONSECUTIVE_FAILURES
    return server
