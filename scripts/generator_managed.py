"""Run generator.py with encrypted Telegram-managed subscription sources.

The original collector remains unchanged for its static allowlist. This wrapper
adds admin-managed subscriptions from telegram-bot-state before generator.py
binds collector.collect, so they pass through the exact same parser, deduper,
validator and output pipeline as every other source.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

import collector
from telegram_managed_sources import (
    SourceCryptoError,
    SourceValidationError,
    enabled_sources,
    fetch_subscription,
    load_sources_from_file,
)

MANAGED_STATE_PATH = os.environ.get(
    "TELEGRAM_SOURCE_STATE_PATH",
    "botstate/telegram_subscription_sources.json",
)
MANAGED_FETCH_WORKERS = 5

_original_collect = collector.collect
_cached_sources: List[Dict[str, Any]] | None = None


def managed_sources() -> List[Dict[str, Any]]:
    global _cached_sources
    if _cached_sources is None:
        # If an encrypted state file exists but cannot be decrypted, fail the
        # collector instead of silently pretending user-managed sources vanished.
        _cached_sources = enabled_sources(load_sources_from_file(MANAGED_STATE_PATH))
    return [dict(item) for item in _cached_sources]


def _fetch_managed_source(source: Dict[str, Any]) -> Dict[str, str] | None:
    source_id = str(source.get("id") or "unknown")[:12]
    try:
        content = fetch_subscription(str(source.get("url") or ""))
    except SourceValidationError as exc:
        print(
            f"[collector] managed source {source_id} skipped: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None

    if not content.strip():
        return None

    # Never expose the raw subscription URL to data/servers.json or logs.
    return {
        "source_name": f"BotSub-{source_id}",
        "source_url": f"managed://{source_id}",
        "content": content,
    }


def collect_with_managed() -> List[Dict[str, str]]:
    results = _original_collect()
    sources = managed_sources()

    if sources:
        workers = min(MANAGED_FETCH_WORKERS, len(sources))
        # executor.map preserves source order while overlapping network waits, so
        # one dead source does not make every following source wait 15 seconds.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            managed_results = list(executor.map(_fetch_managed_source, sources))
        results.extend(item for item in managed_results if item is not None)

    print(
        f"[collector] loaded {len(sources)} enabled bot-managed subscription source(s)",
        flush=True,
    )
    return results


def main() -> int:
    try:
        managed_sources()
    except SourceCryptoError as exc:
        print(f"[collector] managed-source state error: {exc}", file=sys.stderr, flush=True)
        return 1

    # generator.py imports `collect` by value (`from collector import collect`).
    # Patch collector before importing generator so the existing generator uses
    # our combined collector without invasive edits to its core pipeline.
    collector.collect = collect_with_managed

    import generator

    original_count_active_sources = generator.count_active_sources

    def count_active_sources_with_managed() -> int:
        return original_count_active_sources() + len(managed_sources())

    generator.count_active_sources = count_active_sources_with_managed
    generator.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
