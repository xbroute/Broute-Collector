"""Production Telegram Bot Control entrypoint.

Combines durable ON/OFF verification, encrypted sources/destinations, purchase
CTA settings and role-aware private administration while preserving
telegram_bot_control.py as the single getUpdates consumer.
"""
from __future__ import annotations

import telegram_bot_control as control
from telegram_bot_control_verified import verified_set_enabled
from telegram_bot_destination_extension import install as install_destination_extension
from telegram_bot_promo_extension import install as install_promo_extension
from telegram_bot_source_extension import install as install_source_extension
from telegram_bot_admin_extension import install as install_admin_extension


def main() -> int:
    original_set_enabled = control.set_enabled
    restore_sources = install_source_extension(control)
    restore_promo = install_promo_extension(control)
    # Keep legacy integrations available for discovery, then enforce the role
    # policy at the outermost layer for every production command/callback.
    restore_destinations = install_destination_extension(control)
    restore_admin = install_admin_extension(control)
    control.set_enabled = verified_set_enabled
    try:
        return control.main()
    finally:
        control.set_enabled = original_set_enabled
        restore_admin()
        restore_destinations()
        restore_promo()
        restore_sources()


if __name__ == "__main__":
    raise SystemExit(main())
