"""Extend Telegram Bot Control with durable purchase-CTA URL + label management."""
from __future__ import annotations

from typing import Any, Callable

from telegram_promo_config import (
    DEFAULT_BUTTON_TEXT,
    DEFAULT_PROMO_URL,
    PROMO_PATH,
    normalize_button_text,
    normalize_promo_url,
    promo_from_data,
)

_PENDING: dict[tuple[int, int, int | None], tuple[str, str]] = {}


def _key(user_id: int, chat_id: int, thread_id: int | None) -> tuple[int, int, int | None]:
    return user_id, chat_id, thread_id


def _stash(user_id: int, chat_id: int, thread_id: int | None, argument: str, chat_type: str) -> None:
    _PENDING[_key(user_id, chat_id, thread_id)] = (argument, chat_type)


def _pop(user_id: int, chat_id: int, thread_id: int | None) -> tuple[str, str]:
    return _PENDING.pop(_key(user_id, chat_id, thread_id), ("", ""))


def _current_promo(control: Any) -> dict[str, str]:
    data = control.read_repo_json(
        PROMO_PATH,
        control.CONTROL_BRANCH,
        {"url": DEFAULT_PROMO_URL, "text": DEFAULT_BUTTON_TEXT},
    )
    try:
        return promo_from_data(data)
    except Exception as exc:
        raise control.RetryableCommandError(f"invalid persisted purchase CTA config: {exc}") from exc


def _persist_verified(control: Any, *, url: str, text: str, message: str) -> dict[str, str]:
    expected = {
        "url": normalize_promo_url(url),
        "text": normalize_button_text(text),
    }
    control.write_repo_json(
        PROMO_PATH,
        control.CONTROL_BRANCH,
        expected,
        message,
    )
    persisted = _current_promo(control)
    if persisted != expected:
        raise control.RetryableCommandError(
            "purchase CTA read-back did not match requested URL/text"
        )
    return persisted


def _set_url_verified(control: Any, value: str) -> tuple[bool, dict[str, str]]:
    current = _current_promo(control)
    normalized = normalize_promo_url(value)
    changed = current["url"] != normalized
    if not changed:
        return False, current
    persisted = _persist_verified(
        control,
        url=normalized,
        text=current["text"],
        message="chore: update Telegram purchase CTA URL via bot",
    )
    return True, persisted


def _set_text_verified(control: Any, value: str) -> tuple[bool, dict[str, str]]:
    current = _current_promo(control)
    normalized = normalize_button_text(value)
    changed = current["text"] != normalized
    if not changed:
        return False, current
    persisted = _persist_verified(
        control,
        url=current["url"],
        text=normalized,
        message="chore: update Telegram purchase CTA label via bot",
    )
    return True, persisted


def _settings_text(control: Any) -> str:
    current = _current_promo(control)
    return (
        "🛒 تنظیمات دکمه خرید\n\n"
        f"📝 نام دکمه: {current['text']}\n"
        f"🔗 لینک: {current['url']}\n\n"
        "برای تغییر در پیام خصوصی بات:\n"
        "/buy_text متن جدید دکمه\n"
        "/buy_link https://example.com/path"
    )


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
            rows.append([{"text": "🛒 تنظیمات خرید", "callback_data": "promo:show"}])
        return {"inline_keyboard": rows}

    def register_commands():
        # Preserve commands registered by previously-installed extensions.
        original_register_commands()
        commands = control.telegram_api("getMyCommands", {})
        if not isinstance(commands, list):
            commands = []
        ours = {"buy_button", "buy_link", "buy_text"}
        cleaned = [
            item for item in commands
            if isinstance(item, dict) and str(item.get("command") or "") not in ours
        ]
        cleaned.extend(
            [
                {"command": "buy_button", "description": "نمایش تنظیمات دکمه خرید"},
                {"command": "buy_link", "description": "نمایش یا تغییر لینک دکمه خرید"},
                {"command": "buy_text", "description": "نمایش یا تغییر نام دکمه خرید"},
            ]
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
                    "promo_show",
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
                raw_text = str(message.get("text") or "").strip()
                command = control.normalize_command(raw_text)
                action_by_command = {
                    "/buy_button": "promo_show",
                    "/buy_link": "promo_link",
                    "/buy_text": "promo_text",
                }
                action = action_by_command.get(command)
                if action:
                    try:
                        user_id = int(sender.get("id"))
                        chat_id = int(chat.get("id"))
                    except (TypeError, ValueError):
                        return None, None, None, None, None
                    thread_id = control.message_thread_id(message)
                    parts = raw_text.split(maxsplit=1)
                    argument = parts[1].strip() if len(parts) > 1 else ""
                    _stash(
                        user_id,
                        chat_id,
                        thread_id,
                        argument,
                        str(chat.get("type") or ""),
                    )
                    return action, user_id, chat_id, thread_id, None

        return original_update_to_action(update)

    def process_action(action, *, user_id, chat_id, thread_id, callback_id=None):
        if action not in {"promo_show", "promo_link", "promo_text"}:
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
                "⛔ فقط ادمین‌های فعلی گروه Broute اجازه تغییر تنظیمات خرید را دارند.",
                thread_id=thread_id,
            )
            return

        argument, chat_type = _pop(user_id, chat_id, thread_id)

        if action == "promo_show" or not argument:
            if callback_id:
                control.answer_callback(callback_id, "تنظیمات خرید نمایش داده شد")
            control.safe_send_text(
                chat_id,
                _settings_text(control),
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
            return

        if chat_type != "private":
            control.safe_send_text(
                chat_id,
                "🔐 تغییر نام یا لینک دکمه خرید فقط در پیام خصوصی بات مجاز است.",
                thread_id=thread_id,
            )
            return

        try:
            if action == "promo_link":
                changed, persisted = _set_url_verified(control, argument)
                label = "لینک"
            else:
                changed, persisted = _set_text_verified(control, argument)
                label = "نام"
        except ValueError as exc:
            control.safe_send_text(
                chat_id,
                f"❌ مقدار پذیرفته نشد: {exc}",
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
            return
        except Exception as exc:
            raise control.RetryableCommandError(
                f"could not persist/verify purchase CTA config: {exc}"
            ) from exc

        prefix = f"✅ {label} دکمه خرید تغییر کرد." if changed else f"ℹ️ همین {label} از قبل تنظیم بود."
        control.safe_send_text(
            chat_id,
            f"{prefix}\n\n"
            f"📝 نام: {persisted['text']}\n"
            f"🔗 لینک: {persisted['url']}\n\n"
            "پیام‌های بعدی کانفیگ از همین تنظیمات استفاده می‌کنند.",
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
