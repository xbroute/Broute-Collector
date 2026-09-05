"""Run telegram_publisher with live ON/OFF control and bot command polling.

The active publisher is the primary Telegram command consumer. This removes the
critical dependency on GitHub's scheduled workflow for OFF commands: while a
publisher run is alive, it polls pending bot commands during pacing and again
immediately before validation/send. The durable control flag on ``main`` remains
the source of truth, so an OFF command stops the run gracefully without losing
the persisted queue.
"""
from __future__ import annotations

import os
import sys
import time

import telegram_bot_control_safe as bot_control
import telegram_publisher as publisher
from telegram_publisher_control import remote_enabled

CHECK_INTERVAL_SECONDS = max(
    2,
    int(os.environ.get("TELEGRAM_CONTROL_CHECK_INTERVAL_SECONDS", "5")),
)
BOT_POLL_INTERVAL_SECONDS = max(
    2,
    int(os.environ.get("TELEGRAM_BOT_POLL_INTERVAL_SECONDS", "5")),
)


class PublishingDisabled(RuntimeError):
    pass


_last_bot_poll_monotonic = 0.0


def poll_bot_commands(*, force: bool = False) -> None:
    """Process pending publisher-control commands without killing publishing on
    transient Bot/GitHub API failures.

    The remote control flag is checked separately and fail-closed by
    ``remote_enabled``. A temporary inability to poll Telegram should therefore
    not corrupt queue state or turn publishing on by accident.
    """
    global _last_bot_poll_monotonic

    now = time.monotonic()
    if not force and now - _last_bot_poll_monotonic < BOT_POLL_INTERVAL_SECONDS:
        return
    _last_bot_poll_monotonic = now

    try:
        rc = bot_control.main()
        if rc != 0:
            print(
                f"[telegram-control] bot command poll returned {rc}; "
                "publisher will keep honoring the durable control flag.",
                file=sys.stderr,
                flush=True,
            )
    except Exception as exc:
        print(
            f"[telegram-control] bot command poll failed: {exc}; "
            "publisher will keep honoring the durable control flag.",
            file=sys.stderr,
            flush=True,
        )


def ensure_enabled(*, force_bot_poll: bool = False) -> None:
    poll_bot_commands(force=force_bot_poll)
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
                f"(control/bot checked every {CHECK_INTERVAL_SECONDS}s)",
                flush=True,
            )
            first = False

        time.sleep(min(float(CHECK_INTERVAL_SECONDS), remaining))


def main() -> int:
    original_live_validate = publisher.live_validate
    original_send_message = publisher.send_message

    def controlled_live_validate(server):
        ensure_enabled(force_bot_poll=True)
        return original_live_validate(server)

    def controlled_send_message(token, chat_id, topic_id, text):
        ensure_enabled(force_bot_poll=True)
        return original_send_message(token, chat_id, topic_id, text)

    publisher.wait_until_next_slot = controlled_wait_until_next_slot
    publisher.live_validate = controlled_live_validate
    publisher.send_message = controlled_send_message

    try:
        ensure_enabled(force_bot_poll=True)
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
