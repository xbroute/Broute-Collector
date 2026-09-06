"""Production Telegram Bot Control entrypoint.

Combines durable ON/OFF verification with encrypted subscription-source
management while preserving telegram_bot_control.py as the single getUpdates
consumer.
"""
from __future__ import annotations

import telegram_bot_control as control
from telegram_bot_control_verified import verified_set_enabled
from telegram_bot_source_extension import install as install_source_extension


def main() -> int:
    original_set_enabled = control.set_enabled
    restore_sources = install_source_extension(control)
    control.set_enabled = verified_set_enabled
    try:
        return control.main()
    finally:
        control.set_enabled = original_set_enabled
        restore_sources()


if __name__ == "__main__":
    raise SystemExit(main())
