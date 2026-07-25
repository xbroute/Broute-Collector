"""
generator.py
اسکریپت اصلی که کل خط لوله را اجرا می‌کند:
جمع‌آوری -> پردازش -> حذف تکراری -> بررسی اولیه -> تولید فایل‌های خروجی.

اجرا:
    python scripts/generator.py
"""
from __future__ import annotations

import base64
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

from collector import collect
from parser import parse_all
from deduplicator import deduplicate
from validator import validate_server
from common import config_to_dict, load_json, save_json, country_flag, rename_raw_config
MAX_SERVERS_PER_RUN = 500
VALIDATION_WORKERS = 25

SERVERS_PATH = "data/servers.json"
STATUS_PATH = "data/status.json"
SOURCES_PATH = "data/sources.json"

OUTPUT_FILES = {
    "vless": "data/vless.txt",
    "vmess": "data/vmess.txt",
    "trojan": "data/trojan.txt",
    "shadowsocks": "data/shadowsocks.txt",
    "hysteria2": "data/hysteria2.txt",
}
SUB_PATH = "data/sub.txt"
SUB_B64_PATH = "data/sub-base64.txt"
SECURE_PATH = "data/secure.txt"
ALL_PATH = "data/all.txt"


def build_server_records(deduped_items: List[Dict], previous_servers: Dict[str, Dict]) -> List[Dict]:
    records = []
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for item in deduped_items:
        cfg = item["config"]
        record = config_to_dict(cfg)
        record["name"] = cfg.name or f"{cfg.protocol}-{cfg.address}"
        record["source_name"] = item.get("source_name", "")
        record["source_url"] = item.get("source_url", "")
        record["secure"] = not cfg.insecure
        record["status"] = "unknown"
        record["latency"] = None
        record["first_seen"] = previous_servers.get(record["id"], {}).get("first_seen", now_iso)
        records.append(record)
    return records


def run_validation(records: List[Dict], previous_servers: Dict[str, Dict]) -> List[Dict]:
    records = records[:MAX_SERVERS_PER_RUN]
    validated = []
    with ThreadPoolExecutor(max_workers=VALIDATION_WORKERS) as executor:
        future_to_record = {
            executor.submit(validate_server, record, previous_servers.get(record["id"])): record
            for record in records
        }
        for future in as_completed(future_to_record):
            try:
                result = future.result()
            except Exception:
                continue
            if result.get("should_remove"):
                continue
            known_country = result.get("country") not in (None, "", "XX")
            if known_country:
                flag = country_flag(result.get("country", ""))
                country_name = result.get("country_name") or "Unknown"
                display_name = f"{flag} {country_name}".strip() if flag else country_name
            else:
                display_name = "یام‌یام پروکسی | @YamYamProxy"
            result["name"] = display_name
            result["raw"] = rename_raw_config(result.get("raw", ""), result.get("protocol", ""), display_name)
            validated.append(result)
    return validated


def write_output_files(servers: List[Dict]) -> None:
    protocol_lines: Dict[str, List[str]] = {p: [] for p in OUTPUT_FILES}
    all_lines: List[str] = []
    secure_lines: List[str] = []

    for server in servers:
        raw = server.get("raw", "")
        if not raw:
            continue
        all_lines.append(raw)
        protocol = server.get("protocol", "")
        if protocol in protocol_lines:
            protocol_lines[protocol].append(raw)
        if server.get("secure"):
            secure_lines.append(raw)

    for protocol, path in OUTPUT_FILES.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(protocol_lines[protocol]) + ("\n" if protocol_lines[protocol] else ""))

    with open(SUB_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines) + ("\n" if all_lines else ""))

    with open(SECURE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(secure_lines) + ("\n" if secure_lines else ""))

    with open(ALL_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines) + ("\n" if all_lines else ""))

    joined = "\n".join(all_lines)
    encoded = base64.b64encode(joined.encode("utf-8")).decode("utf-8")
    with open(SUB_B64_PATH, "w", encoding="utf-8") as f:
        f.write(encoded)


def write_status(servers: List[Dict], active_sources: int) -> None:
    online = sum(1 for s in servers if s.get("status") == "online")
    offline = sum(1 for s in servers if s.get("status") == "offline")
    secure = sum(1 for s in servers if s.get("secure"))
    countries = len({s.get("country") for s in servers if s.get("country") and s.get("country") != "XX"})

    status = {
        "total_configs": len(servers),
        "online_configs": online,
        "offline_configs": offline,
        "secure_configs": secure,
        "countries": countries,
        "active_sources": active_sources,
        "last_update": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_json(STATUS_PATH, status)


def count_active_sources() -> int:
    sources = load_json(SOURCES_PATH, {})
    count = 0
    for group in ("github_sources", "subscription_sources", "manual_sources", "telegram_sources"):
        count += sum(1 for s in sources.get(group, []) if s.get("enabled"))
    return count


def main() -> None:
    previous_servers_list = load_json(SERVERS_PATH, [])
    previous_servers = {s["id"]: s for s in previous_servers_list if "id" in s}

    raw_sources = collect()
    parsed_items = parse_all(raw_sources)
    deduped_items = deduplicate(parsed_items)

    records = build_server_records(deduped_items, previous_servers)
    validated_records = run_validation(records, previous_servers)

    # حذف فیلد raw از خروجی JSON عمومی برای پاکیزگی؛ raw فقط برای تولید sub.txt استفاده می‌شود
    save_json(SERVERS_PATH, validated_records)

    write_output_files(validated_records)
    write_status(validated_records, count_active_sources())

    print(f"[generator] done. {len(validated_records)} servers written.")


if __name__ == "__main__":
    main()
