"""Multi-destination discovery and private-chat management for Telegram Bot Control."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Tuple

from telegram_destinations import (
    DEFAULT_TEMPLATE,
    MAX_DESTINATIONS,
    DESTINATION_STATE_BRANCH,
    DESTINATION_STATE_PATH,
    DestinationStateError,
    DestinationValidationError,
    decrypt_store,
    destination_key,
    encrypt_store,
    find_destination,
    format_interval,
    normalize_store,
    normalize_template,
    now_iso,
    parse_interval_spec,
)

_PENDING_ARGS: Dict[Tuple[str, int, int, int | None], List[str]] = defaultdict(list)
_PENDING_MEMBERSHIP: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)


def _key(action: str, user_id: int, chat_id: int, thread_id: int | None):
    return action, user_id, chat_id, thread_id


def _stash_arg(action: str, user_id: int, chat_id: int, thread_id: int | None, value: str):
    _PENDING_ARGS[_key(action, user_id, chat_id, thread_id)].append(value)


def _pop_arg(action: str, user_id: int, chat_id: int, thread_id: int | None) -> str:
    key = _key(action, user_id, chat_id, thread_id)
    values = _PENDING_ARGS.get(key, [])
    if not values:
        return ""
    value = values.pop(0)
    if not values:
        _PENDING_ARGS.pop(key, None)
    return value


def _load_store(control: Any) -> Dict[str, Any]:
    payload = control.read_repo_json(DESTINATION_STATE_PATH, DESTINATION_STATE_BRANCH, {})
    try:
        return decrypt_store(payload)
    except DestinationStateError as exc:
        raise control.RetryableCommandError(
            f"could not decrypt Telegram destination state: {exc}"
        ) from exc


def _save_store_verified(control: Any, store: Dict[str, Any], message: str) -> Dict[str, Any]:
    expected = normalize_store(store)
    control.write_repo_json(
        DESTINATION_STATE_PATH,
        DESTINATION_STATE_BRANCH,
        encrypt_store(expected),
        message,
    )
    verify_payload = control.read_repo_json(
        DESTINATION_STATE_PATH,
        DESTINATION_STATE_BRANCH,
        {},
    )
    try:
        persisted = decrypt_store(verify_payload)
    except DestinationStateError as exc:
        raise control.RetryableCommandError(
            f"could not verify persisted Telegram destinations: {exc}"
        ) from exc
    if persisted != expected:
        raise control.RetryableCommandError("Telegram destination read-back mismatch")
    return persisted


def _manager_ids(control: Any) -> set[int]:
    return {
        int(value)
        for value in _load_store(control).get("manager_user_ids", [])
        if str(value).lstrip("-").isdigit()
    }


def _chat_admin_ids(control: Any, chat_id: int) -> set[int]:
    try:
        members = control.telegram_api("getChatAdministrators", {"chat_id": chat_id})
    except Exception as exc:
        raise control.RetryableCommandError(
            "could not verify administrators for the Telegram destination"
        ) from exc
    if not isinstance(members, list):
        raise control.RetryableCommandError(
            "Telegram returned an invalid destination administrator list"
        )
    result: set[int] = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        user = member.get("user")
        if not isinstance(user, dict) or bool(user.get("is_bot", False)):
            continue
        try:
            result.add(int(user.get("id")))
        except (TypeError, ValueError):
            pass
    return result


def _list_text(store: Dict[str, Any]) -> str:
    destinations = store.get("destinations", [])
    if not destinations:
        return (
            "📡 هنوز هیچ مقصدی ثبت نشده.\n\n"
            "1) بات را در گروه یا کانال موردنظر ادمین کن.\n"
            "2) چند دقیقه صبر کن تا my_chat_member پردازش شود.\n"
            "3) دوباره /targets را بزن.\n\n"
            "مقصد تازه به‌صورت پیش‌فرض خاموش ثبت می‌شود."
        )

    lines = ["📡 مقصدهای انتشار:", ""]
    for index, item in enumerate(destinations, 1):
        enabled = item.get("enabled") is True and item.get("bot_status") == "administrator"
        icon = "🟢" if enabled else "🔴"
        title = str(item.get("title") or "بدون نام")
        kind = "کانال" if item.get("chat_type") == "channel" else "گروه"
        interval = format_interval(
            int(item.get("min_delay_seconds", 30)),
            int(item.get("max_delay_seconds", 90)),
        )
        lines.append(f"{index}. {icon} {title} — {kind} — ⏱ {interval}")

    lines.extend(
        [
            "",
            "تنظیم سریع:",
            "/target_on <شماره>",
            "/target_off <شماره>",
            "/target_interval <شماره> 30-90",
            "/target_template <شماره> سپس خط جدید و قالب",
            "/target_template_reset <شماره>",
            "/target_topic <شماره> <topic_id|0>",
            "/target_remove <شماره>",
        ]
    )
    return "\n".join(lines)


def _target_keyboard(item: Dict[str, Any]) -> Dict[str, Any]:
    key = str(item.get("key") or "")
    enabled = item.get("enabled") is True
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔴 خاموش" if enabled else "🟢 روشن",
                    "callback_data": f"target:{'off' if enabled else 'on'}:{key}",
                }
            ],
            [
                {"text": "⏱ 30–90 ثانیه", "callback_data": f"target:interval:{key}:30-90"},
                {"text": "⏱ 1–2 دقیقه", "callback_data": f"target:interval:{key}:1m-2m"},
            ],
            [
                {"text": "⏱ 2–4 دقیقه", "callback_data": f"target:interval:{key}:2m-4m"},
                {"text": "⏱ 5–10 دقیقه", "callback_data": f"target:interval:{key}:5m-10m"},
            ],
            [
                {"text": "📝 نمایش قالب", "callback_data": f"target:template:{key}"},
                {"text": "♻️ قالب پیش‌فرض", "callback_data": f"target:reset:{key}"},
            ],
            [{"text": "⬅️ مقصدها", "callback_data": "targets:list"}],
        ]
    }


def _target_text(item: Dict[str, Any]) -> str:
    title = str(item.get("title") or "بدون نام")
    enabled = item.get("enabled") is True and item.get("bot_status") == "administrator"
    interval = format_interval(
        int(item.get("min_delay_seconds", 30)),
        int(item.get("max_delay_seconds", 90)),
    )
    topic = item.get("message_thread_id")
    return (
        f"{'🟢' if enabled else '🔴'} {title}\n\n"
        f"نوع: {item.get('chat_type') or 'unknown'}\n"
        f"وضعیت بات: {item.get('bot_status') or 'unknown'}\n"
        f"فاصله: {interval}\n"
        f"Topic: {topic if topic else 'General / بدون topic'}\n"
        f"شناسه کوتاه: {item.get('key')}"
    )


def _targets_keyboard(store: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for item in store.get("destinations", []):
        icon = "🟢" if item.get("enabled") is True and item.get("bot_status") == "administrator" else "🔴"
        title = str(item.get("title") or "بدون نام")
        rows.append(
            [{"text": f"{icon} {title[:32]}", "callback_data": f"target:show:{item.get('key')}"}]
        )
    return {"inline_keyboard": rows}


def _resolve(store: Dict[str, Any], selector: str):
    destinations = list(store.get("destinations", []))
    index, item = find_destination(destinations, selector)
    if item is None:
        raise DestinationValidationError(
            "مقصد پیدا نشد؛ /targets را بزن و شماره/شناسه را بررسی کن"
        )
    return destinations, int(index), item


def _split_selector_argument(value: str) -> Tuple[str, str]:
    parts = str(value or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def install(control: Any) -> Callable[[], None]:
    original_update_to_action = control.update_to_action
    original_process_action = control.process_action
    original_register_commands = control.register_commands
    original_menu_keyboard = control.menu_keyboard
    original_is_authorized_admin = control.is_authorized_admin
    original_current_admin_ids = control.current_admin_ids

    def is_authorized_admin(user_id: int) -> bool:
        return int(user_id) in _manager_ids(control)

    def current_admin_ids() -> set[int]:
        return _manager_ids(control)

    def menu_keyboard():
        keyboard = original_menu_keyboard()
        rows = [list(row) for row in keyboard.get("inline_keyboard", []) if isinstance(row, list)]
        if not any(
            isinstance(button, dict) and button.get("callback_data") == "targets:list"
            for row in rows
            for button in row
        ):
            rows.append([{"text": "📡 مقصدهای انتشار", "callback_data": "targets:list"}])
        return {"inline_keyboard": rows}

    def register_commands():
        original_register_commands()
        commands = control.telegram_api("getMyCommands", {})
        if not isinstance(commands, list):
            commands = []
        ours = {
            "targets",
            "target_on",
            "target_off",
            "target_interval",
            "target_template",
            "target_template_reset",
            "target_topic",
            "target_remove",
        }
        cleaned = [
            item
            for item in commands
            if isinstance(item, dict) and str(item.get("command") or "") not in ours
        ]
        cleaned.extend(
            [
                {"command": "targets", "description": "مدیریت گروه‌ها و کانال‌های مقصد"},
                {"command": "target_on", "description": "فعال کردن یک مقصد"},
                {"command": "target_off", "description": "خاموش کردن یک مقصد"},
                {"command": "target_interval", "description": "تنظیم فاصله ارسال مقصد"},
                {"command": "target_template", "description": "نمایش/تغییر قالب پیام مقصد"},
                {"command": "target_template_reset", "description": "بازگردانی قالب پیش‌فرض"},
                {"command": "target_topic", "description": "تنظیم Topic گروه Forum"},
                {"command": "target_remove", "description": "حذف مقصد از کنترل‌پنل"},
            ]
        )
        control.telegram_api("setMyCommands", {"commands": cleaned})

    def update_to_action(update):
        membership = update.get("my_chat_member") if isinstance(update, dict) else None
        if isinstance(membership, dict):
            sender = membership.get("from")
            chat = membership.get("chat")
            new_member = membership.get("new_chat_member")
            if isinstance(sender, dict) and isinstance(chat, dict) and isinstance(new_member, dict):
                try:
                    user_id = int(sender.get("id"))
                    chat_id = int(chat.get("id"))
                except (TypeError, ValueError):
                    return None, None, None, None, None
                if str(chat.get("type") or "") in {"group", "supergroup", "channel"}:
                    _PENDING_MEMBERSHIP[(user_id, chat_id)].append(
                        {
                            "chat_id": chat_id,
                            "title": str(chat.get("title") or ""),
                            "username": str(chat.get("username") or ""),
                            "chat_type": str(chat.get("type") or ""),
                            "bot_status": str(new_member.get("status") or ""),
                        }
                    )
                    return "target_membership", user_id, chat_id, None, None

        callback = update.get("callback_query") if isinstance(update, dict) else None
        if isinstance(callback, dict):
            data = str(callback.get("data") or "")
            sender = callback.get("from")
            message = callback.get("message")
            if isinstance(sender, dict) and isinstance(message, dict):
                try:
                    user_id = int(sender.get("id"))
                    chat_id = int((message.get("chat") or {}).get("id"))
                except (TypeError, ValueError):
                    return None, None, None, None, None
                thread_id = control.message_thread_id(message)
                callback_id = str(callback.get("id") or "")

                if data == "targets:list":
                    return "target_list", user_id, chat_id, thread_id, callback_id

                parts = data.split(":")
                if len(parts) >= 3 and parts[0] == "target":
                    verb, selector = parts[1], parts[2]
                    mapping = {
                        "show": "target_show",
                        "on": "target_on",
                        "off": "target_off",
                        "template": "target_template_show",
                        "reset": "target_template_reset",
                    }
                    if verb == "interval" and len(parts) == 4:
                        _stash_arg(
                            "target_interval",
                            user_id,
                            chat_id,
                            thread_id,
                            f"{selector} {parts[3]}",
                        )
                        return "target_interval", user_id, chat_id, thread_id, callback_id
                    action = mapping.get(verb)
                    if action:
                        _stash_arg(action, user_id, chat_id, thread_id, selector)
                        return action, user_id, chat_id, thread_id, callback_id

        message = update.get("message") if isinstance(update, dict) else None
        if isinstance(message, dict):
            sender = message.get("from")
            chat = message.get("chat")
            if isinstance(sender, dict) and isinstance(chat, dict):
                try:
                    user_id = int(sender.get("id"))
                    chat_id = int(chat.get("id"))
                except (TypeError, ValueError):
                    return None, None, None, None, None
                thread_id = control.message_thread_id(message)
                raw = str(message.get("text") or "")
                command = control.normalize_command(raw)
                mapping = {
                    "/targets": "target_list",
                    "/target_on": "target_on",
                    "/target_off": "target_off",
                    "/target_interval": "target_interval",
                    "/target_template": "target_template_set",
                    "/target_template_reset": "target_template_reset",
                    "/target_topic": "target_topic",
                    "/target_remove": "target_remove",
                }
                action = mapping.get(command)
                if action:
                    parts = raw.split(maxsplit=1)
                    argument = parts[1] if len(parts) > 1 else ""
                    _stash_arg(action, user_id, chat_id, thread_id, argument)
                    return action, user_id, chat_id, thread_id, None

        return original_update_to_action(update)

    def _membership(action_user_id: int, target_chat_id: int) -> None:
        values = _PENDING_MEMBERSHIP.get((action_user_id, target_chat_id), [])
        if not values:
            return
        event = values.pop(0)
        if not values:
            _PENDING_MEMBERSHIP.pop((action_user_id, target_chat_id), None)

        store = _load_store(control)
        destinations = list(store.get("destinations", []))
        index, existing = find_destination(destinations, str(target_chat_id))
        status = str(event.get("bot_status") or "")

        if existing is not None and status != "administrator":
            item = dict(existing)
            item["bot_status"] = status or "unknown"
            item["enabled"] = False
            item["updated_at"] = now_iso()
            destinations[int(index)] = item
            store["destinations"] = destinations
            _save_store_verified(
                control,
                store,
                "chore: disable Telegram destination after bot membership change [automated]",
            )
            print("[bot-control] known Telegram destination became non-admin and was disabled.", flush=True)
            return

        if status != "administrator":
            return

        admins = _chat_admin_ids(control, target_chat_id)
        if action_user_id not in admins:
            return

        managers = [int(x) for x in store.get("manager_user_ids", [])]
        if not managers:
            managers = [action_user_id]
            store["manager_user_ids"] = managers
            print("[bot-control] Telegram destination manager bootstrapped from verified admin.", flush=True)
        elif action_user_id not in managers:
            # Do not let an arbitrary person gain global control merely by adding
            # this public bot to their own group/channel.
            print("[bot-control] ignored untrusted destination promotion event.", flush=True)
            return

        if existing is None and len(destinations) >= MAX_DESTINATIONS:
            print("[bot-control] destination limit reached; discovery ignored.", flush=True)
            return

        if existing is None:
            destinations.append(
                {
                    "key": destination_key(target_chat_id),
                    "chat_id": target_chat_id,
                    "title": event.get("title") or f"chat {target_chat_id}",
                    "username": event.get("username") or "",
                    "chat_type": event.get("chat_type") or "",
                    "bot_status": "administrator",
                    "enabled": False,
                    "min_delay_seconds": 30,
                    "max_delay_seconds": 90,
                    "template": DEFAULT_TEMPLATE,
                    "message_thread_id": None,
                    "discovered_at": now_iso(),
                    "updated_at": now_iso(),
                }
            )
            print("[bot-control] verified Telegram destination discovered (default OFF).", flush=True)
        else:
            item = dict(existing)
            item.update(
                {
                    "title": event.get("title") or item.get("title"),
                    "username": event.get("username") or item.get("username"),
                    "chat_type": event.get("chat_type") or item.get("chat_type"),
                    "bot_status": "administrator",
                    "updated_at": now_iso(),
                }
            )
            destinations[int(index)] = item

        store["destinations"] = destinations
        _save_store_verified(
            control,
            store,
            "chore: register encrypted Telegram destination [automated]",
        )

    def process_action(action, *, user_id, chat_id, thread_id, callback_id=None):
        if action == "target_membership":
            _membership(user_id, chat_id)
            return

        store = _load_store(control)
        managers = {int(x) for x in store.get("manager_user_ids", [])}

        if action == "menu" and user_id not in managers:
            if not managers:
                control.safe_send_text(
                    chat_id,
                    "🔐 هنوز مدیر اصلی بات ثبت نشده.\n\n"
                    "بات را در یکی از گروه‌ها یا کانال‌های خودت ادمین کن. "
                    "وقتی Telegram رویداد ادمین‌شدن را به بات بدهد، همان ادمین "
                    "به‌عنوان مدیر اصلی ثبت می‌شود. بعد دوباره /start را در PV بزن.",
                    thread_id=thread_id,
                )
            else:
                control.safe_send_text(
                    chat_id,
                    "⛔ این حساب اجازه مدیریت مقصدهای این بات را ندارد.",
                    thread_id=thread_id,
                )
            return

        target_actions = {
            "target_list",
            "target_show",
            "target_on",
            "target_off",
            "target_interval",
            "target_template_show",
            "target_template_set",
            "target_template_reset",
            "target_topic",
            "target_remove",
        }
        if action not in target_actions:
            return original_process_action(
                action,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                callback_id=callback_id,
            )

        if user_id not in managers:
            if callback_id:
                control.answer_callback(callback_id, "دسترسی ندارید")
            control.safe_send_text(chat_id, "⛔ دسترسی مدیریت مقصدها ندارید.", thread_id=thread_id)
            return

        if chat_id < 0:
            control.safe_send_text(
                chat_id,
                "🔐 تنظیم مقصدها فقط از Private Chat بات انجام می‌شود.",
                thread_id=thread_id,
            )
            return

        if action == "target_list":
            if callback_id:
                control.answer_callback(callback_id, "لیست مقصدها به‌روز شد")
            control.safe_send_text(
                chat_id,
                _list_text(store),
                thread_id=thread_id,
                keyboard=_targets_keyboard(store),
            )
            return

        argument = _pop_arg(action, user_id, chat_id, thread_id).strip()

        if action in {"target_show", "target_template_show"}:
            destinations, _, item = _resolve(store, argument)
            if action == "target_template_show":
                control.safe_send_text(
                    chat_id,
                    "📝 قالب فعلی این مقصد:\n\n" + str(item.get("template") or DEFAULT_TEMPLATE),
                    thread_id=thread_id,
                    keyboard=_target_keyboard(item),
                )
            else:
                control.safe_send_text(
                    chat_id,
                    _target_text(item),
                    thread_id=thread_id,
                    keyboard=_target_keyboard(item),
                )
            return

        selector, value = _split_selector_argument(argument)
        destinations, index, item = _resolve(store, selector)
        updated = dict(item)

        if action in {"target_on", "target_off"}:
            if action == "target_on" and item.get("bot_status") != "administrator":
                control.safe_send_text(
                    chat_id,
                    "❌ بات در این مقصد ادمین نیست؛ اول دوباره آن را ادمین کن.",
                    thread_id=thread_id,
                )
                return
            updated["enabled"] = action == "target_on"

        elif action == "target_interval":
            if not value:
                control.safe_send_text(
                    chat_id,
                    "مثال: /target_interval 1 60\n"
                    "یا /target_interval 1 30-90\n"
                    "یا /target_interval 1 2m-4m",
                    thread_id=thread_id,
                )
                return
            low, high = parse_interval_spec(value)
            updated["min_delay_seconds"] = low
            updated["max_delay_seconds"] = high

        elif action == "target_template_set":
            if not selector or not value:
                control.safe_send_text(
                    chat_id,
                    "قالب را بعد از شماره مقصد بفرست. مثال:\n"
                    "/target_template 1\n"
                    "🟢 {country}\n{protocol}\n\n{config}\n\n{brand}\n\n"
                    "Placeholderهای مجاز: {flag} {country} {protocol} {security} "
                    "{network} {latency} {config} {brand} {subscription_url}",
                    thread_id=thread_id,
                )
                return
            updated["template"] = normalize_template(value)

        elif action == "target_template_reset":
            updated["template"] = DEFAULT_TEMPLATE

        elif action == "target_topic":
            if not value:
                control.safe_send_text(
                    chat_id,
                    "مثال: /target_topic 1 1944\nبرای ارسال بدون Topic مقدار 0 بده.",
                    thread_id=thread_id,
                )
                return
            try:
                topic = int(value)
            except ValueError as exc:
                raise DestinationValidationError("Topic ID باید عدد باشد") from exc
            if topic < 0:
                raise DestinationValidationError("Topic ID نمی‌تواند منفی باشد")
            updated["message_thread_id"] = topic or None

        elif action == "target_remove":
            removed = destinations.pop(index)
            store["destinations"] = destinations
            _save_store_verified(
                control,
                store,
                "chore: remove encrypted Telegram destination via bot",
            )
            if callback_id:
                control.answer_callback(callback_id, "مقصد حذف شد")
            control.safe_send_text(
                chat_id,
                f"🗑 مقصد «{removed.get('title') or 'بدون نام'}» از کنترل‌پنل حذف شد.",
                keyboard=menu_keyboard(),
            )
            return

        updated["updated_at"] = now_iso()
        destinations[index] = updated
        store["destinations"] = destinations

        try:
            persisted = _save_store_verified(
                control,
                store,
                "chore: update encrypted Telegram destination via bot",
            )
        except Exception as exc:
            raise control.RetryableCommandError(
                f"could not persist destination settings: {exc}"
            ) from exc

        _, _, persisted_item = _resolve(persisted, str(updated.get("key")))
        if persisted_item.get("enabled") is True and control.current_enabled():
            control.ensure_publisher_run()

        if callback_id:
            control.answer_callback(callback_id, "تنظیم مقصد ذخیره شد")
        control.safe_send_text(
            chat_id,
            "✅ تنظیم مقصد ذخیره شد.\n\n" + _target_text(persisted_item),
            keyboard=_target_keyboard(persisted_item),
        )

    control.update_to_action = update_to_action
    control.process_action = process_action
    control.register_commands = register_commands
    control.menu_keyboard = menu_keyboard
    control.is_authorized_admin = is_authorized_admin
    control.current_admin_ids = current_admin_ids

    def restore() -> None:
        control.update_to_action = original_update_to_action
        control.process_action = original_process_action
        control.register_commands = original_register_commands
        control.menu_keyboard = original_menu_keyboard
        control.is_authorized_admin = original_is_authorized_admin
        control.current_admin_ids = original_current_admin_ids
        _PENDING_ARGS.clear()
        _PENDING_MEMBERSHIP.clear()

    return restore
