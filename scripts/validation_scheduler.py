"""Fair validation scheduling for frequently changing subscription sources.

The collector may discover more configs than it can reasonably TCP-check in one
GitHub Actions run. This module prevents early sources from monopolizing the
validation budget:

* never-validated configs are always considered before refreshes;
* candidates are selected round-robin across every source that contains them;
* previously validated configs are refreshed least-recently-checked first;
* duplicate configs may belong to multiple sources but are validated only once;
* records that are still present but not selected this run can safely carry
  forward their previous validation state until their next fair turn.

No extra cursor file is required: after a record is checked its ``last_checked``
becomes newer, so the remaining older/unvalidated records naturally move ahead
on the following run.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple


DYNAMIC_FIELDS = (
    "status",
    "latency",
    "last_seen",
    "last_checked",
    "success_count",
    "fail_count",
    "consecutive_failures",
    "first_seen",
    "country",
    "country_name",
    "should_remove",
)


def _source_identity(source: Dict[str, Any]) -> str:
    url = str(source.get("source_url") or "").strip()
    name = str(source.get("source_name") or "").strip()
    return url or f"name:{name}" or "source:unknown"


def record_source_keys(record: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    seen: Set[str] = set()

    sources = record.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            key = _source_identity(source)
            if key not in seen:
                seen.add(key)
                keys.append(key)

    if not keys:
        fallback = _source_identity(record)
        keys.append(fallback)

    return keys


def _last_checked(previous: Dict[str, Dict[str, Any]], record: Dict[str, Any]) -> str:
    prev = previous.get(str(record.get("id") or ""), {})
    return str(prev.get("last_checked") or "")


def _round_robin(
    candidates: Sequence[Dict[str, Any]],
    previous: Dict[str, Dict[str, Any]],
    limit: int,
    already_selected: Set[str],
) -> List[Dict[str, Any]]:
    """Select candidates fairly across sources, without duplicate IDs."""
    if limit <= 0 or not candidates:
        return []

    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in candidates:
        for key in record_source_keys(record):
            buckets[key].append(record)

    # Within each source, oldest validation wins. Empty timestamp means this
    # config has never been checked and therefore sorts first.
    for key, bucket in buckets.items():
        bucket.sort(
            key=lambda record: (
                0 if not _last_checked(previous, record) else 1,
                _last_checked(previous, record),
                str(record.get("id") or ""),
            )
        )

    source_keys = sorted(buckets)
    positions = {key: 0 for key in source_keys}
    chosen: List[Dict[str, Any]] = []

    made_progress = True
    while len(chosen) < limit and made_progress:
        made_progress = False
        for key in source_keys:
            bucket = buckets[key]
            pos = positions[key]
            while pos < len(bucket):
                record = bucket[pos]
                pos += 1
                record_id = str(record.get("id") or "")
                if not record_id or record_id in already_selected:
                    continue
                already_selected.add(record_id)
                chosen.append(record)
                made_progress = True
                break
            positions[key] = pos
            if len(chosen) >= limit:
                break

    # Shared configs can exhaust several buckets at once. Fill any remaining
    # capacity globally by oldest/unvalidated priority so budget is not wasted.
    if len(chosen) < limit:
        remaining = sorted(
            candidates,
            key=lambda record: (
                0 if not _last_checked(previous, record) else 1,
                _last_checked(previous, record),
                str(record.get("id") or ""),
            ),
        )
        for record in remaining:
            record_id = str(record.get("id") or "")
            if not record_id or record_id in already_selected:
                continue
            already_selected.add(record_id)
            chosen.append(record)
            if len(chosen) >= limit:
                break

    return chosen


def select_for_validation(
    records: Sequence[Dict[str, Any]],
    previous: Dict[str, Dict[str, Any]],
    budget: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """Choose up to ``budget`` records without starving late/managed sources.

    Phase 1 spends as much budget as necessary on records that have never been
    validated before. If unseen configs exceed the budget they are themselves
    selected round-robin across sources. Phase 2 uses any remaining capacity to
    refresh already-known configs, oldest first and still source-fair.
    """
    budget = max(0, int(budget))
    if budget == 0:
        return [], list(records), {
            "candidates": len(records),
            "selected": 0,
            "deferred": len(records),
            "new_unvalidated": sum(
                1 for r in records if not _last_checked(previous, r)
            ),
        }

    unseen = [record for record in records if not _last_checked(previous, record)]
    known = [record for record in records if _last_checked(previous, record)]

    selected_ids: Set[str] = set()
    selected: List[Dict[str, Any]] = []

    selected.extend(
        _round_robin(
            unseen,
            previous,
            min(budget, len(unseen)),
            selected_ids,
        )
    )

    remaining_budget = budget - len(selected)
    if remaining_budget > 0:
        selected.extend(
            _round_robin(
                known,
                previous,
                remaining_budget,
                selected_ids,
            )
        )

    selected_set = {str(record.get("id") or "") for record in selected}
    deferred = [
        record
        for record in records
        if str(record.get("id") or "") not in selected_set
    ]

    stats = {
        "candidates": len(records),
        "selected": len(selected),
        "deferred": len(deferred),
        "new_unvalidated": len(unseen),
    }
    return selected, deferred, stats


def carry_forward_validation(
    record: Dict[str, Any],
    previous_record: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Keep current source/config data while preserving last validation result."""
    carried = dict(record)
    prev = previous_record or {}

    for field in DYNAMIC_FIELDS:
        if field in prev:
            carried[field] = prev[field]

    if not prev:
        carried.setdefault("status", "unknown")
        carried.setdefault("latency", None)
        carried.setdefault("success_count", 0)
        carried.setdefault("fail_count", 0)
        carried.setdefault("consecutive_failures", 0)
        carried.setdefault("country", "XX")
        carried.setdefault("country_name", "Unknown")
        carried.setdefault("should_remove", False)

    return carried
