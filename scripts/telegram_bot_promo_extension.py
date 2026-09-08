"""Extend Telegram Bot Control with purchase-CTA URL management."""
from __future__ import annotations

from typing import Any, Callable

from telegram_promo_config import DEFAULT_PROMO_URL, PROMO_PATH, normalize_promo_url

_PENDING: dict[tuple[int, int, int | None], tuple[str, str]] = {}


def _key(user_id: int, chat_id: int, thread_id: int | None) -> tuple[int, int, int | None]:
    return user_id, chat_id, thread_id


def _stash(user_id: int, chat_id: int, thread_id: int | None, argument: str, chat_type: str) -> None:
    _PENDING[_key(user_id, chat_id, thread_id)] = (argument, chat_type)


def _pop(user_id: int, chat_id: int, thread_id: int | None) -> tuple[str, str]:
    return _PENDING.pop(_key(user_id, chat_id, thread_id), ("", ""))


def _current_url(control: Any) -> str:
    data = control.read_repo_json(PROMO_PATH, control.CONTROL_BRANCH, {"url": DEFAULT_PROMO_URL})
    try:
        return normalize_promo_url(str(data.get("url") or ""))
    except Exception as exc:
        raise control.RetryableCommandError(f"invalid persisted purchase URL: {exc}") from exc


def _set_url_verified(control: Any, value: str) -> tuple[bool, str]:
    normalized = normalize_promo_url(value)
    before = _current_url(control)
    changed = before != normalized
    if changed:
        control.write_repo_json(
            PROMO_PATH,
            control.CONTROL_BRANCH,
            {"url": normalized},
            "chore: update Telegram purchase CTA URL via bot",
        )
        persisted = _current_url(control)
        if persisted != normalized:
            raise control.RetryableCommandError(
                "purchase CTA URL read-back did not match requested value"
            )
    return changed, normalized


def install(control: Any) -> Callable[[], None]:
    original_update_to_action = control.update_to_action
    original_process_action = control.process_action
    original_register_commands = control.register_commands
    original_menu_keyboard = control.menu_keyboard

    def menu_keyboard():
        keyboard = original_menu_keyboard()
        rows = [list(row) for row in keyboard.get("inline_keyboard", []) if isinstance(row, list)]
        if not any(
            isinstance(button, dict) and button.get("callback_data") == "promo:show"
            for row in rows
            for button in row
        ):
            rows.append([{"text": "🛒 لینک خرید", "callback_data": "promo:show"}])
        return {"inline_keyboard": rows}

    def register_commands():
        # Let previously-installed extensions register their commands first, then
        # read back Telegram's effective command list and add ours without
        # hard-coding the source-management command set here.
        original_register_commands()
        commands = control.telegram_api("getMyCommands", {})
        if not isinstance(commands, list):
            commands = []
        cleaned = [
            item for item in commands
            if isinstance(item, dict) and str(item.get("command") or "") != "buy_link"
        ]
        cleaned.append(
            {"command": "buy_link", "description": "نمایش یا تغییر لینک دکمه خرید"}
        )
        control.telegram_api("setMyCommands", {"commands": cleaned})

    def update_to_action(update):
        callback = update.get("callback_query") if isinstance(update, dict) else None
        if isinstance(callback, dict) and str(callback.get("data") or "") == "promo:show":
            sender = callback.get("from")
            message = callback.get("message")
            if isinstance(sender, dict) and isinstance(message, dict):
                try:
                    user_id = int(sender.get("id"))
                    chat_id = int((message.get("chat") or {}).get("id"))
                except (TypeError, ValueError):
                    return None, None, None, None, None
                return (
                    "promo_link",
                    user_id,
                    chat_id,
                    control.message_thread_id(message),
                    str(callback.get("id") or ""),
                )

        message = update.get("message") if isinstance(update, dict) else None
        if isinstance(message, dict):
            sender = message.get("from")
            chat = message.get("chat")
            if isinstance(sender, dict) and isinstance(chat, dict):
                text = str(message.get("text") or "").strip()
                if control.normalize_command(text) == "/buy_link":
                    try:
                        user_id = int(sender.get("id"))
                        chat_id = int(chat.get("id"))
                    except (TypeError, ValueError):
                        return None, None, None, None, None
                    thread_id = control.message_thread_id(message)
                    parts = text.split(maxsplit=1)
                    argument = parts[1].strip() if len(parts) > 1 else ""
                    _stash(
                        user_id,
                        chat_id,
                        thread_id,
                        argument,
                        str(chat.get("type") or ""),
                    )
                    return "promo_link", user_id, chat_id, thread_id, None

        return original_update_to_action(update)

    def process_action(action, *, user_id, chat_id, thread_id, callback_id=None):
        if action != "promo_link":
            return original_process_action(
                action,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                callback_id=callback_id,
            )

        if not control.is_authorized_admin(user_id):
            if callback_id:
                control.answer_callback(callback_id, "فقط ادمین‌های گروه دسترسی دارند")
            control.safe_send_text(
                chat_id,
                "⛔ فقط ادمین‌های فعلی گروه Broute اجازه تغییر لینک خرید را دارند.",
                thread_id=thread_id,
            )
            return

        argument, chat_type = _pop(user_id, chat_id, thread_id)
        if not argument:
            current = _current_url(control)
            if callback_id:
                control.answer_callback(callback_id, "لینک خرید نمایش داده شد")
            control.safe_send_text(
                chat_id,
                "🛒 لینک فعلی دکمه خرید:\n"
                f"{current}\n\n"
                "برای تغییر، در پیام خصوصی بات بفرست:\n"
                "/buy_link https://example.com/path",
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
            return

        if chat_type != "private":
            control.safe_send_text(
                chat_id,
                "🔐 برای تغییر لینک خرید، دستور /buy_link <url> را فقط در پیام خصوصی بات بفرست.",
                thread_id=thread_id,
            )
            return

        try:
            changed, normalized = _set_url_verified(control, argument)
        except ValueError as exc:
            control.safe_send_text(
                chat_id,
                f"❌ لینک پذیرفته نشد: {exc}",
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
            return
        except Exception as exc:
            raise control.RetryableCommandError(
                f"could not persist/verify purchase CTA URL: {exc}"
            ) from exc

        prefix = "✅ لینک دکمه خرید تغییر کرد." if changed else "ℹ️ همین لینک از قبل تنظیم بود."
        control.safe_send_text(
            chat_id,
            f"{prefix}\n\n🔗 {normalized}\n\n"
            "پیام‌های بعدی کانفیگ از همین لینک استفاده می‌کنند.",
            thread_id=thread_id,
            keyboard=menu_keyboard(),
        )

    control.update_to_action = update_to_action
    control.process_action = process_action
    control.register_commands = register_commands
    control.menu_keyboard = menu_keyboard

    def restore() -> None:
        control.update_to_action = original_update_to_action
        control.process_action = original_process_action
        control.register_commands = original_register_commands
        control.menu_keyboard = original_menu_keyboard
        _PENDING.clear()

    return restore
