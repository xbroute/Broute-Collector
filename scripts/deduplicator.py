"""
deduplicator.py
حذف کانفیگ‌های تکراری بر اساس هویت کامل اتصال، بدون از دست دادن provenance.

هویت اتصال fragment/نام نمایشی را نادیده می‌گیرد، اما پارامترهای واقعی اتصال
مثل path، security، flow، Reality public key، sid و queryهای transport را نگه
می‌دارد. بنابراین provider می‌تواند همان UUID/host را با path یا Reality key
جدید منتشر کند و آن نسخه به اشتباه duplicate نسخه قدیمی محسوب نمی‌شود.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, List
from urllib.parse import parse_qsl, unquote, urlsplit


def _decode_vmess(raw: str) -> Dict[str, Any] | None:
    try:
        payload = raw[len("vmess://"):].split("#", 1)[0].strip()
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def canonical_connection_key(cfg: Any) -> str:
    """Stable ID for the actual connection, excluding only display metadata."""
    raw = str(getattr(cfg, "raw", "") or "").strip()
    protocol = str(getattr(cfg, "protocol", "") or "").lower()
    canonical: Any = None

    if protocol == "vmess" and raw.startswith("vmess://"):
        data = _decode_vmess(raw)
        if data is not None:
            data = dict(data)
            # ps is only the client-visible label; everything else may affect
            # the connection and must participate in identity.
            data.pop("ps", None)
            canonical = data

    if canonical is None and "://" in raw and protocol != "vmess":
        try:
            parsed = urlsplit(raw.split("#", 1)[0])
            query = sorted(
                (str(key).lower(), str(value))
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            )
            canonical = {
                "scheme": parsed.scheme.lower(),
                "username": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "host": (parsed.hostname or "").lower(),
                "port": parsed.port or 0,
                "path": parsed.path or "",
                "query": query,
            }
        except Exception:
            canonical = None

    if canonical is None:
        # Conservative fallback for unusual URI variants. Keep all connection
        # text except the cosmetic fragment so we never collapse a real change.
        canonical = raw.split("#", 1)[0]

    payload = json.dumps(
        {"protocol": protocol, "connection": canonical},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
        key = canonical_connection_key(cfg)
        source = _source_ref(original)

        if key not in seen:
            item = dict(original)
            item["connection_id"] = key
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
