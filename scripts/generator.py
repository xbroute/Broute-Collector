"""
generator.py
خط لوله اصلی:
جمع‌آوری -> پردازش -> dedupe کامل اتصال -> زمان‌بندی عادلانه validation -> خروجی.

وقتی تعداد candidateها از بودجه‌ی TCP validation بیشتر باشد، snapshot بریده
نمی‌شود. همه‌ی configهای موجود در sourceهای فعلی در servers.json باقی می‌مانند،
اما validation بین sourceها عادلانه تقسیم می‌شود. configهای کاملاً جدید اولویت
دارند و configهای قبلی که این Run نوبتشان نشده آخرین state معتبر را حفظ می‌کنند.
"""
from __future__ import annotations

import base64
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

from collector import collect
from parser import parse_all
from deduplicator import deduplicate, canonical_raw_connection_key
from validator import validate_server
from common import config_to_dict, load_json, save_json, country_flag, rename_raw_config
from validation_scheduler import carry_forward_validation, select_for_validation

MAX_VALIDATIONS_PER_RUN = max(
    1,
    int(os.environ.get("MAX_VALIDATIONS_PER_RUN", "500")),
)
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


def _safe_sources(item: Dict[str, Any]) -> List[Dict[str, str]]:
    raw_sources = item.get("sources")
    sources: List[Dict[str, str]] = []
    seen = set()

    if isinstance(raw_sources, list):
        candidates = raw_sources
    else:
        candidates = [
            {
                "source_name": item.get("source_name", ""),
                "source_url": item.get("source_url", ""),
            }
        ]

    for source in candidates:
        if not isinstance(source, dict):
            continue
        normalized = {
            "source_name": str(source.get("source_name") or ""),
            "source_url": str(source.get("source_url") or ""),
        }
        marker = (normalized["source_name"], normalized["source_url"])
        if marker in seen:
            continue
        seen.add(marker)
        sources.append(normalized)

    return sources


def _previous_by_canonical(
    previous_servers_list: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Map old snapshots onto the new full-connection ID without reset storms."""
    mapped: Dict[str, Dict[str, Any]] = {}
    for server in previous_servers_list:
        raw = str(server.get("raw") or "")
        protocol = str(server.get("protocol") or "")
        if not raw or not protocol:
            continue
        try:
            key = canonical_raw_connection_key(raw, protocol)
        except Exception:
            continue
        mapped.setdefault(key, server)
    return mapped


def _choose_primary_source(
    sources: List[Dict[str, str]],
    previous_record: Dict[str, Any] | None,
) -> Dict[str, str]:
    if not sources:
        return {"source_name": "", "source_url": ""}

    prev = previous_record or {}
    prev_marker = (
        str(prev.get("source_name") or ""),
        str(prev.get("source_url") or ""),
    )
    for source in sources:
        if (source["source_name"], source["source_url"]) == prev_marker:
            return source
    return sources[0]


def build_server_records(
    deduped_items: List[Dict],
    previous_servers: Dict[str, Dict],
    previous_canonical: Dict[str, Dict],
) -> Tuple[List[Dict], Dict[str, Dict]]:
    records: List[Dict] = []
    matched_previous: Dict[str, Dict] = {}
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for item in deduped_items:
        cfg = item["config"]
        record = config_to_dict(cfg)
        connection_id = str(item.get("connection_id") or record["id"])
        record["id"] = connection_id

        previous_record = previous_servers.get(connection_id) or previous_canonical.get(connection_id)
        if previous_record:
            matched_previous[connection_id] = previous_record

        sources = _safe_sources(item)
        primary = _choose_primary_source(sources, previous_record)
        record["sources"] = sources
        record["source_name"] = primary["source_name"]
        record["source_url"] = primary["source_url"]
        record["name"] = cfg.name or f"{cfg.protocol}-{cfg.address}"
        record["secure"] = not cfg.insecure
        record["status"] = "unknown"
        record["latency"] = None
        record["first_seen"] = (previous_record or {}).get("first_seen", now_iso)
        records.append(record)

    return records, matched_previous


def _apply_display_name(record: Dict[str, Any]) -> Dict[str, Any]:
    known_country = record.get("country") not in (None, "", "XX")
    if known_country:
        flag = country_flag(str(record.get("country") or ""))
        country_name = str(record.get("country_name") or "Unknown")
        display_name = f"{flag} {country_name}".strip() if flag else country_name
    else:
        display_name = "@xbroute"

    record["name"] = display_name
    record["raw"] = rename_raw_config(
        str(record.get("raw") or ""),
        str(record.get("protocol") or ""),
        display_name,
    )
    return record


def run_validation(
    records: List[Dict],
    previous_servers: Dict[str, Dict],
) -> Tuple[List[Dict], Dict[str, int]]:
    selected, deferred, stats = select_for_validation(
        records,
        previous_servers,
        MAX_VALIDATIONS_PER_RUN,
    )

    print(
        "[validation-scheduler] "
        f"candidates={stats['candidates']} selected={stats['selected']} "
        f"deferred={stats['deferred']} new_unvalidated={stats['new_unvalidated']} "
        f"budget={MAX_VALIDATIONS_PER_RUN}",
        flush=True,
    )

    validated_by_id: Dict[str, Dict] = {}
    failed_validation_ids = set()

    with ThreadPoolExecutor(max_workers=VALIDATION_WORKERS) as executor:
        future_to_record = {
            executor.submit(
                validate_server,
                dict(record),
                previous_servers.get(str(record["id"])),
            ): record
            for record in selected
        }
        for future in as_completed(future_to_record):
            original = future_to_record[future]
            record_id = str(original["id"])
            try:
                result = future.result()
            except Exception:
                # A validator exception must not delete a still-present config
                # from the snapshot. Preserve its last state and retry fairly.
                failed_validation_ids.add(record_id)
                continue
            validated_by_id[record_id] = _apply_display_name(result)

    output: List[Dict] = []
    selected_ids = {str(record["id"]) for record in selected}

    for record in records:
        record_id = str(record["id"])
        validated = validated_by_id.get(record_id)
        if validated is not None:
            # Keep should_remove=True records in servers.json as tombstones while
            # the source still contains them. That preserves failure counters so
            # a dead config cannot reset to zero and resurrect every few runs.
            output.append(validated)
            continue

        carried = carry_forward_validation(
            record,
            previous_servers.get(record_id),
        )
        output.append(_apply_display_name(carried))

    stats = dict(stats)
    stats["validator_errors"] = len(failed_validation_ids)
    stats["validated_successfully"] = len(validated_by_id)
    stats["deferred"] = len(records) - len(selected_ids)
    return output, stats


def write_output_files(servers: List[Dict]) -> None:
    protocol_lines: Dict[str, List[str]] = {p: [] for p in OUTPUT_FILES}
    all_lines: List[str] = []
    secure_lines: List[str] = []

    for server in servers:
        raw = server.get("raw", "")
        if not raw:
            continue
        # Unknown means never validated; should_remove means repeated failures.
        # Neither belongs in public subscription outputs. Offline configs below
        # the removal threshold keep the existing grace-period behavior.
        if server.get("status") == "unknown" or server.get("should_remove"):
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


def write_status(
    servers: List[Dict],
    active_sources: int,
    validation_stats: Dict[str, int] | None = None,
) -> None:
    online = sum(1 for s in servers if s.get("status") == "online")
    offline = sum(1 for s in servers if s.get("status") == "offline")
    unknown = sum(1 for s in servers if s.get("status") == "unknown")
    removed = sum(1 for s in servers if s.get("should_remove") is True)
    secure = sum(1 for s in servers if s.get("secure"))
    countries = len({s.get("country") for s in servers if s.get("country") and s.get("country") != "XX"})

    status: Dict[str, Any] = {
        "total_configs": len(servers),
        "online_configs": online,
        "offline_configs": offline,
        "unknown_configs": unknown,
        "removed_configs": removed,
        "secure_configs": secure,
        "countries": countries,
        "active_sources": active_sources,
        "last_update": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if validation_stats:
        status["validation"] = {
            "budget": MAX_VALIDATIONS_PER_RUN,
            **{key: int(value) for key, value in validation_stats.items()},
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
    previous_servers_by_id = {
        str(s["id"]): s for s in previous_servers_list if isinstance(s, dict) and s.get("id")
    }
    previous_canonical = _previous_by_canonical(previous_servers_list)

    raw_sources = collect()
    parsed_items = parse_all(raw_sources)
    deduped_items = deduplicate(parsed_items)

    records, matched_previous = build_server_records(
        deduped_items,
        previous_servers_by_id,
        previous_canonical,
    )
    validated_records, validation_stats = run_validation(records, matched_previous)

    # records are derived exclusively from the *current* source contents. A
    # config removed from every live subscription therefore disappears here,
    # while a current config never disappears merely because validation budget
    # was exhausted.
    save_json(SERVERS_PATH, validated_records)

    write_output_files(validated_records)
    write_status(validated_records, count_active_sources(), validation_stats)

    print(
        f"[generator] done. {len(validated_records)} current unique configs written; "
        f"{validation_stats.get('validated_successfully', 0)} validated this run."
    )


if __name__ == "__main__":
    main()
