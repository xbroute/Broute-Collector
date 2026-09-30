"""
collector.py
دریافت محتوای خام از منابع مشخص‌شده در data/sources.json (فقط Allowlist).
هیچ منبع ناشناسی به‌صورت خودکار اضافه یا Discover نمی‌شود.

خروجی: رکوردهای {"source_name", "source_url", "content"} برای parser.py؛
رکورد دریافت ناموفق همچنین دارای fetch_failed=True است.
"""
from __future__ import annotations

import html
import http.client
import re
import sys
import time
import urllib.request
import urllib.error
from typing import Any, List, Dict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from common import load_json

SOURCES_PATH = "data/sources.json"
TELEGRAM_PREVIEW_URL = "https://telegram.me/s/{channel}"


class SourceFetchError(RuntimeError):
    """A failed request is different from a successfully emptied source."""


def fetch_url(url: str, timeout: int, max_size: int, retries: int, user_agent: str) -> str:
    """دریافت محتوای یک URL با Timeout، Retry محدود و محدودیت حجم فایل."""
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise SourceFetchError("source must use HTTP(S)")
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read(max_size + 1)
                if len(data) > max_size:
                    raise ValueError("file too large, skipped")
                return data.decode("utf-8", errors="ignore")
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            last_error = exc
            if isinstance(exc, ValueError):
                break  # An oversized response cannot improve by retrying.
            if attempt < retries:
                time.sleep(1)
    code = getattr(last_error, "code", None)
    reason = f"HTTP {code}" if code else type(last_error).__name__
    raise SourceFetchError(f"source fetch failed ({reason})") from last_error


def strip_html(raw_html: str) -> str:
    """حذف تگ‌های HTML و Decode کردن Entity ها تا فقط متن خام پیام‌ها بماند."""
    text = re.sub(r"<br\s*/?>", "\n", raw_html)
    text = re.sub(r"</?(?:div|p|pre)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def fetch_telegram_channel(channel: str, timeout: int, max_size: int, retries: int, user_agent: str) -> str:
    """
    محتوای صفحه‌ی پیش‌نمایش عمومی یک کانال تلگرام را می‌گیرد (t.me/s/channel).
    این صفحه توسط خود تلگرام برای مشاهده‌ی عمومی بدون نیاز به لاگین یا API Key
    ارائه می‌شود و فقط برای کانال‌های عمومی (Public) کار می‌کند.
    """
    channel = channel.strip().lstrip("@")
    url = TELEGRAM_PREVIEW_URL.format(channel=channel)
    raw_html = fetch_url(url, timeout, max_size, retries, user_agent)
    if not raw_html:
        return ""
    return strip_html(raw_html)


def collect() -> List[Dict[str, Any]]:
    sources = load_json(SOURCES_PATH, {})
    settings = sources.get("settings", {})
    timeout = int(settings.get("request_timeout_seconds", 10))
    max_size = int(settings.get("max_file_size_bytes", 5_000_000))
    retries = int(settings.get("max_retries", 2))
    user_agent = settings.get("user_agent", "config-aggregator-bot/1.0")

    requests = []
    for group in ("github_sources", "subscription_sources"):
        for source in sources.get(group, []):
            if not source.get("enabled"):
                continue
            requests.append(("url", dict(source)))

    # منابع دستی (فایل‌های محلی داخل مخزن)
    for source in sources.get("manual_sources", []):
        if not source.get("enabled"):
            continue
        requests.append(("manual", dict(source)))

    # منابع تلگرام: فقط کانال‌های عمومی، از طریق صفحه‌ی پیش‌نمایش رسمی خود
    # تلگرام (t.me/s/channel) که بدون لاگین یا API Key در دسترس است.
    # هیچ محدودیت دسترسی دور زده نمی‌شود و هیچ اطلاعات حساب کاربری استفاده نمی‌شود.
    for source in sources.get("telegram_sources", []):
        if not source.get("enabled"):
            continue
        requests.append(("telegram", dict(source)))

    def fetch_source(request):
        kind, source = request
        channel = str(source.get("channel") or "").strip().lstrip("@")
        url = (TELEGRAM_PREVIEW_URL.format(channel=channel) if kind == "telegram"
               else str(source.get("path" if kind == "manual" else "url") or ""))
        result = {"source_name": str(source.get("name") or url),
                  "source_url": url, "content": ""}
        try:
            if kind == "manual":
                with open(url, "r", encoding="utf-8") as f:
                    result["content"] = f.read(max_size + 1)
                if len(result["content"].encode("utf-8")) > max_size:
                    raise SourceFetchError("manual source is too large")
            elif kind == "telegram":
                if not re.fullmatch(r"[A-Za-z0-9_]+", channel):
                    raise SourceFetchError("invalid Telegram channel name")
                result["content"] = fetch_telegram_channel(channel, timeout, max_size, retries, user_agent)
            else:
                result["content"] = fetch_url(url, timeout, max_size, retries, user_agent)
        except (SourceFetchError, OSError, ValueError) as exc:
            result["fetch_failed"] = True
            # URLs may carry credentials. Log the public name and error class.
            print(f"[collector] source {result['source_name']!r} unavailable ({type(exc).__name__})",
                  file=sys.stderr, flush=True)
        return result

    if not requests:
        return []
    workers = min(len(requests), max(1, min(10, int(settings.get("fetch_workers", 5)))))
    # map preserves allowlist order and bounds simultaneous network requests.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(fetch_source, requests))


if __name__ == "__main__":
    collected = collect()
    print(f"[collector] fetched {len(collected)} source(s)")
