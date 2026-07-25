"""
collector.py
دریافت محتوای خام از منابع مشخص‌شده در data/sources.json (فقط Allowlist).
هیچ منبع ناشناسی به‌صورت خودکار اضافه یا Discover نمی‌شود.

خروجی: یک لیست از دیکشنری {"source_name", "source_url", "content"} که در
مرحله بعد توسط parser.py پردازش می‌شود.
"""
from __future__ import annotations

import html
import re
import sys
import time
import urllib.request
import urllib.error
from typing import List, Dict

from common import load_json

SOURCES_PATH = "data/sources.json"
TELEGRAM_PREVIEW_URL = "https://telegram.me/s/{channel}"

def fetch_url(url: str, timeout: int, max_size: int, retries: int, user_agent: str) -> str:
    """دریافت محتوای یک URL با Timeout، Retry محدود و محدودیت حجم فایل."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read(max_size + 1)
                if len(data) > max_size:
                    raise ValueError("file too large, skipped")
                return data.decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = exc
            time.sleep(1)
    print(f"[collector] failed to fetch {url}: {last_error}", file=sys.stderr)
    return ""


def strip_html(raw_html: str) -> str:
    """حذف تگ‌های HTML و Decode کردن Entity ها تا فقط متن خام پیام‌ها بماند."""
    text = re.sub(r"<br\s*/?>", "\n", raw_html)
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


def collect() -> List[Dict[str, str]]:
    sources = load_json(SOURCES_PATH, {})
    settings = sources.get("settings", {})
    timeout = int(settings.get("request_timeout_seconds", 10))
    max_size = int(settings.get("max_file_size_bytes", 5_000_000))
    retries = int(settings.get("max_retries", 2))
    user_agent = settings.get("user_agent", "config-aggregator-bot/1.0")

    results: List[Dict[str, str]] = []

    for group in ("github_sources", "subscription_sources"):
        for source in sources.get(group, []):
            if not source.get("enabled"):
                continue
            url = source["url"]
            content = fetch_url(url, timeout, max_size, retries, user_agent)
            if content:
                results.append({
                    "source_name": source.get("name", url),
                    "source_url": url,
                    "content": content,
                })

    # منابع دستی (فایل‌های محلی داخل مخزن)
    for source in sources.get("manual_sources", []):
        if not source.get("enabled"):
            continue
        path = source.get("path")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                results.append({
                    "source_name": source.get("name", path),
                    "source_url": path,
                    "content": content,
                })
        except FileNotFoundError:
            continue

    # منابع تلگرام: فقط کانال‌های عمومی، از طریق صفحه‌ی پیش‌نمایش رسمی خود
    # تلگرام (t.me/s/channel) که بدون لاگین یا API Key در دسترس است.
    # هیچ محدودیت دسترسی دور زده نمی‌شود و هیچ اطلاعات حساب کاربری استفاده نمی‌شود.
    for source in sources.get("telegram_sources", []):
        if not source.get("enabled"):
            continue
        channel = source.get("channel")
        if not channel:
            print(f"[collector] telegram source '{source.get('name')}' has no 'channel' field, skipped",
                  file=sys.stderr)
            continue
        content = fetch_telegram_channel(channel, timeout, max_size, retries, user_agent)
        if content:
            results.append({
                "source_name": source.get("name", channel),
                "source_url": TELEGRAM_PREVIEW_URL.format(channel=channel.lstrip("@")),
                "content": content,
            })

    return results


if __name__ == "__main__":
    collected = collect()
    print(f"[collector] fetched {len(collected)} source(s)")
