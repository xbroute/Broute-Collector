"""Run telegram_publisher with live ON/OFF control only.

Telegram commands are consumed exclusively by the dedicated Telegram Bot Control
workflow. This wrapper NEVER calls getUpdates. It only reads the durable control
flag from main every few seconds and immediately before validation/send, so an
OFF command that has been persisted by the bot poller stops publishing cleanly
without losing queue state.
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
    original_send_message = publisher.send_message

    def controlled_live_validate(server):
        ensure_enabled()
        return original_live_validate(server)

    def controlled_send_message(token, chat_id, topic_id, text):
        ensure_enabled()
        return original_send_message(token, chat_id, topic_id, text)

    publisher.wait_until_next_slot = controlled_wait_until_next_slot
    publisher.live_validate = controlled_live_validate
    publisher.send_message = controlled_send_message

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
