"""
collector.py
دریافت محتوای خام از منابع ثابت و منابع سابسکریپشنی که ادمین از بات اضافه
می‌کند. همه URLهای HTTP(S) قبل از اتصال و در redirectها بررسی می‌شوند تا مقصد
private/loopback/link-local/reserved پذیرفته نشود.

خروجی: لیستی از {source_name, source_url, content} برای parser.py.
"""
from __future__ import annotations

import html
import re
import sys
import time
from typing import Dict, List

from common import load_json
from source_security import fetch_public_text

SOURCES_PATH = "data/sources.json"
USER_SOURCES_PATH = "data/user-sources.json"
TELEGRAM_PREVIEW_URL = "https://telegram.me/s/{channel}"


def fetch_url(url: str, timeout: int, max_size: int, retries: int, user_agent: str) -> str:
    """Fetch a public HTTP(S) URL with retry, timeout and payload-size limits."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fetch_public_text(
                url,
                timeout=timeout,
                max_size=max_size,
                user_agent=user_agent,
            )
        except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1)
    print(f"[collector] failed to fetch {url}: {last_error}", file=sys.stderr)
    return ""


def strip_html(raw_html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw_html)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def fetch_telegram_channel(
    channel: str,
    timeout: int,
    max_size: int,
    retries: int,
    user_agent: str,
) -> str:
    channel = channel.strip().lstrip("@")
    url = TELEGRAM_PREVIEW_URL.format(channel=channel)
    raw_html = fetch_url(url, timeout, max_size, retries, user_agent)
    if not raw_html:
        return ""
    return strip_html(raw_html)


def _append_http_source(
    results: List[Dict[str, str]],
    source: Dict,
    *,
    timeout: int,
    max_size: int,
    retries: int,
    user_agent: str,
) -> None:
    if not source.get("enabled"):
        return
    url = str(source.get("url") or "").strip()
    if not url:
        return
    content = fetch_url(url, timeout, max_size, retries, user_agent)
    if content:
        results.append({
            "source_name": str(source.get("name") or url),
            "source_url": url,
            "content": content,
        })


def collect() -> List[Dict[str, str]]:
    sources = load_json(SOURCES_PATH, {})
    user_sources = load_json(USER_SOURCES_PATH, {"subscription_sources": []})
    settings = sources.get("settings", {})
    timeout = int(settings.get("request_timeout_seconds", 10))
    max_size = int(settings.get("max_file_size_bytes", 5_000_000))
    retries = int(settings.get("max_retries", 2))
    user_agent = settings.get("user_agent", "config-aggregator-bot/1.0")

    results: List[Dict[str, str]] = []

    for group in ("github_sources", "subscription_sources"):
        for source in sources.get(group, []):
            _append_http_source(
                results,
                source,
                timeout=timeout,
                max_size=max_size,
                retries=retries,
                user_agent=user_agent,
            )

    # منابعی که ادمین از طریق بات اضافه کرده است.
    for source in user_sources.get("subscription_sources", []):
        _append_http_source(
            results,
            source,
            timeout=timeout,
            max_size=max_size,
            retries=retries,
            user_agent=user_agent,
        )

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

    for source in sources.get("telegram_sources", []):
        if not source.get("enabled"):
            continue
        channel = source.get("channel")
        if not channel:
            print(
                f"[collector] telegram source '{source.get('name')}' has no channel, skipped",
                file=sys.stderr,
            )
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
