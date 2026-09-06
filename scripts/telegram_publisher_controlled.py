"""Run telegram_publisher with live ON/OFF control, cycles, and copy-friendly output.

Telegram commands are consumed exclusively by the dedicated Telegram Bot Control
workflow. This wrapper NEVER calls getUpdates. It only reads the durable control
flag from main every few seconds and immediately before validation/send, so an
OFF command that has been persisted by the bot poller stops publishing cleanly
without losing queue state.

For outgoing configs, the complete config is rendered as Telegram HTML <pre>.
When the exact config is at most 256 characters, the payload also receives the
Bot API native CopyTextButton. Longer configs are never truncated.

Publishing state is cycle-aware: all-time history remains durable, while each
round has its own sent/fingerprint set. Once every currently publishable config
has been exhausted, the next round is automatically prepared from the freshest
servers.json snapshot.
"""
from __future__ import annotations

import os
import sys
import time

import telegram_publisher as publisher
from telegram_copy_format import decorate_send_payload, make_copyable_message
from telegram_cycle_state import install_cycle_state
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
    original_build_message = publisher.build_message
    original_live_validate = publisher.live_validate
    original_send_message = publisher.send_message
    original_telegram_request_once = publisher._telegram_request_once

    def copyable_build_message(server):
        plain_message = original_build_message(server)
        protocol = str(server.get("protocol") or "").lower()
        config = publisher.brand_raw_config(str(server.get("raw") or ""), protocol)
        return make_copyable_message(plain_message, config)

    def controlled_live_validate(server):
        ensure_enabled()
        return original_live_validate(server)

    def controlled_send_message(token, chat_id, topic_id, text):
        ensure_enabled()
        return original_send_message(token, chat_id, topic_id, text)

    def decorated_telegram_request_once(token, payload):
        # Keep presentation decoration at the last possible point so the
        # publisher's queue/dedupe/state logic continues to operate on the exact
        # config and Telegram's visible 4096-character limit.
        source_text = payload.get("text")
        config_chars = len(getattr(source_text, "copy_text", ""))
        decorated = decorate_send_payload(payload)
        if config_chars:
            print(
                "[telegram-copy] preformatted=yes "
                f"native_copy_button={'yes' if 'reply_markup' in decorated else 'no'} "
                f"config_chars={config_chars}",
                flush=True,
            )
        return original_telegram_request_once(token, decorated)

    publisher.build_message = copyable_build_message
    publisher.wait_until_next_slot = controlled_wait_until_next_slot
    publisher.live_validate = controlled_live_validate
    publisher.send_message = controlled_send_message
    publisher._telegram_request_once = decorated_telegram_request_once

    # Install after the base module is fully imported but before main() reads
    # state. Existing sent/sent_fingerprints become all-time history and are
    # migrated to cycle #1 without an immediate resend storm.
    install_cycle_state(publisher)

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
