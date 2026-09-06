"""
deduplicator.py
حذف کانفیگ‌های تکراری بر اساس شناسه یکتای اتصال، بدون از دست دادن
provenance منبع. اگر یک کانفیگ در چند subscription/channel دیده شود، فقط یک
config برای validation نگه می‌داریم ولی همه‌ی source membershipها ثبت می‌شوند.
"""
from __future__ import annotations

from typing import Any, Dict, List


def _source_ref(item: Dict[str, Any]) -> Dict[str, str]:
    return {
        "source_name": str(item.get("source_name") or ""),
        "source_url": str(item.get("source_url") or ""),
    }


def _append_source(item: Dict[str, Any], source: Dict[str, str]) -> None:
    sources = item.setdefault("sources", [])
    if not isinstance(sources, list):
        sources = []
        item["sources"] = sources

    marker = (source.get("source_name", ""), source.get("source_url", ""))
    for existing in sources:
        if not isinstance(existing, dict):
            continue
        existing_marker = (
            str(existing.get("source_name") or ""),
            str(existing.get("source_url") or ""),
        )
        if existing_marker == marker:
            return
    sources.append(source)


def deduplicate(parsed_items: List[Dict]) -> List[Dict]:
    seen: Dict[str, Dict] = {}

    for original in parsed_items:
        cfg = original["config"]
        key = cfg.unique_key()
        source = _source_ref(original)

        if key not in seen:
            item = dict(original)
            item["sources"] = []
            _append_source(item, source)
            seen[key] = item
        else:
            _append_source(seen[key], source)

    deduped = list(seen.values())
    shared = sum(1 for item in deduped if len(item.get("sources", [])) > 1)
    print(
        f"[deduplicator] {len(parsed_items)} -> {len(deduped)} after dedup; "
        f"{shared} config(s) seen in multiple sources"
    )
    return deduped
