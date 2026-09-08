"""Production Telegram Bot Control entrypoint.

Combines durable ON/OFF verification, encrypted subscription-source management,
and purchase-CTA URL management while preserving telegram_bot_control.py as the
single getUpdates consumer.
"""
from __future__ import annotations

import telegram_bot_control as control
from telegram_bot_control_verified import verified_set_enabled
from telegram_bot_promo_extension import install as install_promo_extension
from telegram_bot_source_extension import install as install_source_extension


def main() -> int:
    original_set_enabled = control.set_enabled
    restore_sources = install_source_extension(control)
    restore_promo = install_promo_extension(control)
    control.set_enabled = verified_set_enabled
    try:
        return control.main()
    finally:
        control.set_enabled = original_set_enabled
        restore_promo()
        restore_sources()


if __name__ == "__main__":
    raise SystemExit(main())
