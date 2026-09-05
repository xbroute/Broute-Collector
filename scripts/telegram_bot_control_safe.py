"""Poison-update-safe entrypoint for Telegram publisher bot controls.

Telegram ``getUpdates`` can contain service messages, photos, empty text fields,
inaccessible callback messages, or otherwise incomplete payloads. A single such
update must never prevent later admin commands from being processed forever.

This compatibility layer hardens the existing controller without duplicating its
GitHub/Telegram action logic. Malformed/non-command updates are treated as no-op
updates so the controller can advance its durable ``last_update_id`` normally.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Tuple

import telegram_bot_control as implementation


def safe_normalize_command(text: Any) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    token = value.split(maxsplit=1)[0].lower()
    if token.startswith("/") and "@" in token:
        token = token.split("@", 1)[0]
    return token


_original_update_to_action = implementation.update_to_action


def safe_update_to_action(
    update: Dict[str, Any],
) -> Tuple[str | None, int | None, int | None, int | None, str | None]:
    try:
        return _original_update_to_action(update)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        # This update is still considered consumed by implementation.main(),
        # which advances last_update_id after this function returns. That is
        # deliberate: one malformed Telegram update must not poison the queue.
        try:
            update_id = update.get("update_id")
        except Exception:
            update_id = None
        print(
            f"[bot-control] ignored malformed/non-actionable update "
            f"{update_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None, None, None, None, None


# Patch only parsing. Authorization, state persistence, ON/OFF writes, callback
# answers and GitHub workflow dispatch remain in the original implementation.
implementation.normalize_command = safe_normalize_command
implementation.update_to_action = safe_update_to_action


def main() -> int:
    return implementation.main()


if __name__ == "__main__":
    raise SystemExit(main())
