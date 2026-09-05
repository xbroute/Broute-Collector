"""Durability guard for Telegram publisher ON/OFF commands.

This wrapper keeps telegram_bot_control.py as the single update consumer, but
replaces set_enabled() with a write/read-back implementation. A Telegram ON/OFF
command is only considered successful after the value stored on main is read
back and matches the requested state. A mismatch raises RetryableCommandError,
so telegram_bot_control.main() deliberately does not advance last_update_id and
the same command is retried on the next poll.
"""
from __future__ import annotations

import telegram_bot_control as control


def verified_set_enabled(enabled: bool) -> bool:
    before = control.current_enabled()
    changed = before != enabled

    if changed:
        control.write_repo_json(
            control.CONTROL_PATH,
            control.CONTROL_BRANCH,
            {"enabled": enabled},
            f"chore: turn Telegram publisher {'ON' if enabled else 'OFF'} via bot",
        )

        try:
            persisted = control.current_enabled()
        except Exception as exc:
            raise control.RetryableCommandError(
                f"could not verify persisted publisher state: {exc}"
            ) from exc

        if persisted is not enabled:
            raise control.RetryableCommandError(
                "publisher control read-back did not match requested state"
            )

    if enabled:
        control.ensure_publisher_run()

    return changed


def main() -> int:
    original_set_enabled = control.set_enabled
    control.set_enabled = verified_set_enabled
    try:
        return control.main()
    finally:
        control.set_enabled = original_set_enabled


if __name__ == "__main__":
    raise SystemExit(main())
