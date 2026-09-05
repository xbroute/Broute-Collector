"""Run telegram_publisher with a live ON/OFF control switch.

This wrapper leaves the publisher's queue/dedupe/send logic untouched. It only
checks the remote control flag before each send cycle and during pacing sleeps.
If the flag becomes OFF, it exits gracefully without mutating the in-memory
candidate that was popped after the last durable checkpoint, so the persisted
queue remains recoverable when publishing is turned back ON.
"""
from __future__ import annotations

import os
import sys
import time

import telegram_publisher as publisher
from telegram_publisher_control import remote_enabled

CHECK_INTERVAL_SECONDS = max(
    2,
    int(os.environ.get("TELEGRAM_CONTROL_CHECK_INTERVAL_SECONDS", "5")),
)


class PublishingDisabled(RuntimeError):
    pass


def ensure_enabled() -> None:
    if not remote_enabled("."):
        raise PublishingDisabled("Telegram publisher is switched OFF")


def controlled_wait_until_next_slot(next_send_after: float) -> None:
    first = True
    while True:
        ensure_enabled()
        remaining = next_send_after - time.time()
        if remaining <= 0:
            return

        if first:
            print(
                f"[telegram] pacing delay: sleeping {remaining:.1f}s "
                f"(control checked every {CHECK_INTERVAL_SECONDS}s)",
                flush=True,
            )
            first = False

        time.sleep(min(float(CHECK_INTERVAL_SECONDS), remaining))


def main() -> int:
    original_live_validate = publisher.live_validate

    def controlled_live_validate(server):
        # One last remote check immediately before the live network validation
        # and subsequent Telegram send path.
        ensure_enabled()
        return original_live_validate(server)

    publisher.wait_until_next_slot = controlled_wait_until_next_slot
    publisher.live_validate = controlled_live_validate

    try:
        ensure_enabled()
        return publisher.main()
    except PublishingDisabled:
        print(
            "[telegram-control] publisher switched OFF; stopping gracefully. "
            "Persisted queue/state is preserved for the next ON run.",
            flush=True,
        )
        return 0
    except KeyboardInterrupt:
        print("[telegram-control] interrupted.", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
