"""
deduplicator.py
حذف کانفیگ‌های تکراری بر اساس شناسه یکتای اتصال (نه نام کانفیگ).
شناسه یکتا در common.ParsedConfig.unique_key() ساخته می‌شود:
protocol + address + port + uuid/password + transport + host + sni
"""
from __future__ import annotations

from typing import List, Dict


def deduplicate(parsed_items: List[Dict]) -> List[Dict]:
    seen = {}
    for item in parsed_items:
        cfg = item["config"]
        key = cfg.unique_key()
        if key not in seen:
            seen[key] = item
        # اگر تکراری بود، اولین رخداد نگه داشته می‌شود (منبعی که زودتر دیده شده)
    deduped = list(seen.values())
    print(f"[deduplicator] {len(parsed_items)} -> {len(deduped)} after dedup")
    return deduped