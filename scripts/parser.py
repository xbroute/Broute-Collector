"""
parser.py
از محتوای خام هر منبع (متن ساده یا Base64) کانفیگ‌های تکی را استخراج،
پروتکل هر خط را تشخیص و به ParsedConfig تبدیل می‌کند.
"""
from __future__ import annotations

from typing import List, Dict

from common import ParsedConfig, detect_protocol, is_probably_base64, safe_b64decode, parse_config_line


def extract_lines(content: str) -> List[str]:
    """محتوای یک منبع را به خطوط کانفیگ خام تبدیل می‌کند و متن‌های غیرمرتبط را حذف می‌کند."""
    if is_probably_base64(content):
        decoded = safe_b64decode(content)
        if decoded:
            content = decoded

    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if detect_protocol(line):
            lines.append(line)
        # خطوطی که پروتکل شناخته‌شده ندارند (کامنت، توضیح، متن فارسی و غیره) نادیده گرفته می‌شوند
    return lines


def parse_source(source: Dict[str, str]) -> List[ParsedConfig]:
    """یک منبع را پردازش می‌کند و لیست ParsedConfig معتبر یا نامعتبر برمی‌گرداند."""
    parsed_configs: List[ParsedConfig] = []
    lines = extract_lines(source["content"])
    for line in lines:
        cfg = parse_config_line(line)
        if cfg is None:
            continue
        cfg.raw_source_name = source.get("source_name", "")  # type: ignore[attr-defined]
        parsed_configs.append(cfg)
    return parsed_configs


def parse_all(sources: List[Dict[str, str]]) -> List[Dict]:
    """
    همه منابع را پردازش می‌کند و برای هر کانفیگ معتبر یک دیکشنری همراه با
    اطلاعات منبع اصلی برمی‌گرداند. کانفیگ‌های نامعتبر شمارش می‌شوند اما در
    خروجی نهایی قرار نمی‌گیرند.
    """
    output = []
    invalid_count = 0
    for source in sources:
        for cfg in parse_source(source):
            if not cfg.valid:
                invalid_count += 1
                continue
            output.append({
                "config": cfg,
                "source_name": source.get("source_name", ""),
                "source_url": source.get("source_url", ""),
            })
    print(f"[parser] {len(output)} valid, {invalid_count} rejected")
    return output
