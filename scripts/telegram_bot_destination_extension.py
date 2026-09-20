"""Dynamic multi-destination management for Telegram Bot Control."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Callable, Dict, List, Tuple

from telegram_destinations import (
    DESTINATION_STATE_BRANCH,
    DESTINATION_STATE_PATH,
    DestinationCryptoError,
    DestinationValidationError,
    active_destinations,
    decrypt_state,
    encrypt_state,
    find_destination,
    normalize_state,
    upsert_destination,
    validate_delay,
)

BOOTSTRAP_PATH = ".github/telegram-bot-bootstrap.json"
_PENDING: Dict[Tuple[str, int, int, int | None], List[str]] = defaultdict(list)


def _key(action: str, user_id: int, chat_id: int, thread_id: int | None):
    return action, user_id, chat_id, thread_id


def _stash(action: str, user_id: int, chat_id: int, thread_id: int | None, value: str) -> None:
    _PENDING[_key(action, user_id, chat_id, thread_id)].append(str(value or ""))


def _pop(action: str, user_id: int, chat_id: int, thread_id: int | None) -> str:
    key = _key(action, user_id, chat_id, thread_id)
    values = _PENDING.get(key, [])
    if not values:
        return ""
    value = values.pop(0)
    if not values:
        _PENDING.pop(key, None)
    return value


def _load(control: Any) -> Dict[str, Any]:
    payload = control.read_repo_json(DESTINATION_STATE_PATH, DESTINATION_STATE_BRANCH, {})
    try:
        return decrypt_state(payload)
    except DestinationCryptoError as exc:
        raise control.RetryableCommandError(f"could not decrypt Telegram destination state: {exc}") from exc


def _save_verified(control: Any, state: Dict[str, Any], message: str) -> Dict[str, Any]:
    expected = normalize_state(state)
    control.write_repo_json(
        DESTINATION_STATE_PATH,
        DESTINATION_STATE_BRANCH,
        encrypt_state(expected),
        message,
    )
    payload = control.read_repo_json(DESTINATION_STATE_PATH, DESTINATION_STATE_BRANCH, {})
    try:
        persisted = decrypt_state(payload)
    except DestinationCryptoError as exc:
        raise control.RetryableCommandError(f"could not verify destination state: {exc}") from exc
    if persisted != expected:
        raise control.RetryableCommandError("Telegram destination state read-back mismatch")

    # Public main only receives a non-sensitive count summary. Chat IDs, titles,
    # usernames and owner IDs remain encrypted on telegram-bot-state.
    summary = {
        "active_count": len(active_destinations(persisted)),
        "total_count": len(persisted.get("destinations", [])),
        "updated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    }
    control.write_repo_json(
        ".github/telegram-destination-summary.json",
        control.CONTROL_BRANCH,
        summary,
        "chore: update Telegram destination summary [automated]",
    )
    verified_summary = control.read_repo_json(
        ".github/telegram-destination-summary.json",
        control.CONTROL_BRANCH,
        {},
    )
    if int(verified_summary.get("active_count", -1)) != summary["active_count"] or int(
        verified_summary.get("total_count", -1)
    ) != summary["total_count"]:
        raise control.RetryableCommandError("Telegram destination summary read-back mismatch")
    return persisted


def _owners(control: Any) -> set[int]:
    return {int(x) for x in _load(control).get("owners", [])}


def _is_owner(control: Any, user_id: int) -> bool:
    return int(user_id) in _owners(control)


def _bot_identity(control: Any) -> int:
    me = control.telegram_api("getMe", {})
    try:
        return int(me.get("id"))
    except Exception as exc:
        raise control.RetryableCommandError("Telegram getMe returned an invalid bot identity") from exc


def _chat_snapshot(control: Any, chat_ref: int | str) -> Dict[str, Any]:
    chat = control.telegram_api("getChat", {"chat_id": chat_ref})
    if not isinstance(chat, dict):
        raise control.RetryableCommandError("Telegram returned invalid chat information")
    return chat


def _admin_snapshot(control: Any, chat_id: int) -> List[Dict[str, Any]]:
    admins = control.telegram_api(
        "getChatAdministrators",
        {"chat_id": chat_id, "return_bots": True},
    )
    if not isinstance(admins, list):
        raise control.RetryableCommandError("Telegram returned invalid administrator information")
    return [item for item in admins if isinstance(item, dict)]


def _validate_owner_and_bot_admin(control: Any, user_id: int, chat_id: int) -> tuple[bool, str]:
    admins = _admin_snapshot(control, chat_id)
    bot_id = _bot_identity(control)
    user_is_admin = False
    bot_member: Dict[str, Any] | None = None

    for item in admins:
        user = item.get("user") if isinstance(item, dict) else None
        if not isinstance(user, dict):
            continue
        try:
            uid = int(user.get("id"))
        except (TypeError, ValueError):
            continue
        if uid == int(user_id):
            user_is_admin = True
        if uid == bot_id:
            bot_member = item

    if not user_is_admin:
        return False, "خودت باید ادمین این مقصد باشی"
    if not bot_member or str(bot_member.get("status") or "") not in {"administrator", "creator"}:
        return False, "بات باید در این مقصد ادمین باشد"

    chat = _chat_snapshot(control, chat_id)
    if str(chat.get("type") or "") == "channel" and bot_member.get("can_post_messages") is not True:
        return False, "برای کانال باید دسترسی Post Messages را به بات بدهی"

    return True, ""


def _destination_text(state: Dict[str, Any]) -> str:
    destinations = state.get("destinations", [])
    if not destinations:
        return (
            "📡 هنوز مقصدی ثبت نشده.\n\n"
            "بات را در کانال/گروه ادمین کن. اگر خودت آن را ادمین کنی، مقصد خودکار ثبت می‌شود.\n"
            "برای گروه هم می‌توانی داخل همان گروه /dest_here بفرستی."
        )

    lines = ["📡 مقصدهای انتشار:", ""]
    for index, item in enumerate(destinations, 1):
        state_icon = "🟢" if item.get("enabled") is True and item.get("status") == "ready" else "🔴"
        kind = {"channel": "کانال", "supergroup": "گروه", "group": "گروه"}.get(
            str(item.get("type") or ""), str(item.get("type") or "مقصد")
        )
        thread = f" · topic {item.get('thread_id')}" if item.get("thread_id") is not None else ""
        lines.append(
            f"{index}. {state_icon} {item.get('title') or 'بدون نام'} · {kind}{thread}\n"
            f"   ⏱ {item.get('min_delay')}–{item.get('max_delay')} ثانیه"
        )
    lines.extend(
        [
            "",
            "روشن/خاموش: /dest_on <شماره> یا /dest_off <شماره>",
            "فاصله اختصاصی: /dest_delay <شماره> <حداقل> <حداکثر>",
            "فاصله همه مقصدها: /delay <حداقل> <حداکثر>",
            "حذف: /dest_remove <شماره>",
        ]
    )
    return "\n".join(lines)


def _dest_keyboard(state: Dict[str, Any]) -> Dict[str, Any]:
    rows: List[List[Dict[str, str]]] = []
    for index, item in enumerate(state.get("destinations", [])[:12], 1):
        enabled = item.get("enabled") is True and item.get("status") == "ready"
        label = f"{'🔴 خاموش' if enabled else '🟢 روشن'} · {index}"
        rows.append([{"text": label, "callback_data": f"dest:toggle:{item.get('id')}"}])
    rows.append([{"text": "🔄 تازه‌سازی مقصدها", "callback_data": "dest:list"}])
    return {"inline_keyboard": rows}


def install(control: Any) -> Callable[[], None]:
    original_update_to_action = control.update_to_action
    original_process_action = control.process_action
    original_register_commands = control.register_commands
    original_menu_keyboard = control.menu_keyboard
    original_status_text = control.status_text
    original_is_authorized_admin = control.is_authorized_admin
    original_current_admin_ids = control.current_admin_ids

    def is_authorized_admin(user_id: int) -> bool:
        return _is_owner(control, user_id)

    def current_admin_ids() -> set[int]:
        return _owners(control)

    def menu_keyboard():
        keyboard = original_menu_keyboard()
        rows = [list(row) for row in keyboard.get("inline_keyboard", []) if isinstance(row, list)]
        if not any(
            isinstance(button, dict) and button.get("callback_data") == "dest:list"
            for row in rows for button in row
        ):
            rows.append([{"text": "📡 مقصدها و فاصله ارسال", "callback_data": "dest:list"}])
        return {"inline_keyboard": rows}

    def register_commands():
        original_register_commands()
        commands = control.telegram_api("getMyCommands", {})
        if not isinstance(commands, list):
            commands = []
        ours = {
            "owner_claim", "destinations", "dest_add", "dest_here", "dest_on",
            "dest_off", "dest_remove", "dest_delay", "delay",
        }
        cleaned = [
            item for item in commands
            if isinstance(item, dict) and str(item.get("command") or "") not in ours
        ]
        cleaned.extend(
            [
                {"command": "owner_claim", "description": "ثبت مالک اصلی بات (فقط یک‌بار)"},
                {"command": "destinations", "description": "مدیریت کانال‌ها و گروه‌های مقصد"},
                {"command": "dest_add", "description": "افزودن مقصد با @username یا chat id"},
                {"command": "dest_here", "description": "ثبت همین گروه/تاپیک به‌عنوان مقصد"},
                {"command": "dest_on", "description": "فعال کردن مقصد"},
                {"command": "dest_off", "description": "غیرفعال کردن مقصد"},
                {"command": "dest_remove", "description": "حذف مقصد"},
                {"command": "dest_delay", "description": "تنظیم فاصله یک مقصد"},
                {"command": "delay", "description": "تنظیم فاصله همه مقصدها"},
            ]
        )
        control.telegram_api("setMyCommands", {"commands": cleaned})

    def update_to_action(update):
        if isinstance(update, dict):
            member_update = update.get("my_chat_member")
            if isinstance(member_update, dict):
                actor = member_update.get("from")
                chat = member_update.get("chat")
                if isinstance(actor, dict) and isinstance(chat, dict):
                    try:
                        user_id = int(actor.get("id"))
                        chat_id = int(chat.get("id"))
                    except (TypeError, ValueError):
                        return None, None, None, None, None
                    _stash("dest_member_event", user_id, chat_id, None, str(update.get("update_id") or ""))
                    # Keep the full update only in memory; no sensitive IDs are logged by the base consumer.
                    _EVENTS[(user_id, chat_id)] = member_update
                    return "dest_member_event", user_id, chat_id, None, None

            callback = update.get("callback_query")
            if isinstance(callback, dict):
                data = str(callback.get("data") or "")
                sender = callback.get("from")
                message = callback.get("message")
                if isinstance(sender, dict) and isinstance(message, dict):
                    try:
                        user_id = int(sender.get("id"))
                        chat_id = int((message.get("chat") or {}).get("id"))
                    except (TypeError, ValueError):
                        user_id = 0
                        chat_id = 0
                    thread_id = control.message_thread_id(message)
                    if data == "dest:list" and user_id and chat_id:
                        return "dest_list", user_id, chat_id, thread_id, str(callback.get("id") or "")
                    if data.startswith("dest:toggle:") and user_id and chat_id:
                        _stash("dest_toggle", user_id, chat_id, thread_id, data.split(":", 2)[2])
                        return "dest_toggle", user_id, chat_id, thread_id, str(callback.get("id") or "")

            message = update.get("message")
            if isinstance(message, dict):
                sender = message.get("from")
                chat = message.get("chat")
                if isinstance(sender, dict) and isinstance(chat, dict):
                    try:
                        user_id = int(sender.get("id"))
                        chat_id = int(chat.get("id"))
                    except (TypeError, ValueError):
                        return original_update_to_action(update)
                    thread_id = control.message_thread_id(message)
                    text = str(message.get("text") or "").strip()
                    command = control.normalize_command(text)
                    rest = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""

                    mapping = {
                        "/owner_claim": "owner_claim",
                        "/destinations": "dest_list",
                        "/dest_add": "dest_add",
                        "/dest_here": "dest_here",
                        "/dest_on": "dest_on",
                        "/dest_off": "dest_off",
                        "/dest_remove": "dest_remove",
                        "/dest_delay": "dest_delay",
                        "/delay": "delay_all",
                    }
                    action = mapping.get(command)
                    if action:
                        _stash(action, user_id, chat_id, thread_id, rest)
                        return action, user_id, chat_id, thread_id, None

        return original_update_to_action(update)

    def _claim_owner(user_id: int, chat_id: int, thread_id: int | None, argument: str) -> None:
        state = _load(control)
        if int(user_id) in {int(x) for x in state.get("owners", [])}:
            control.safe_send_text(chat_id, "✅ این اکانت از قبل مالک بات است.", thread_id=thread_id, keyboard=menu_keyboard())
            return
        if state.get("owners"):
            control.safe_send_text(chat_id, "⛔ مالک اصلی بات قبلاً ثبت شده است.", thread_id=thread_id)
            return
        bootstrap = control.read_repo_json(BOOTSTRAP_PATH, control.CONTROL_BRANCH, {})
        expected = str(bootstrap.get("claim_sha256") or "")
        provided = hashlib.sha256(str(argument or "").encode("utf-8")).hexdigest()
        if not expected or provided != expected:
            control.safe_send_text(chat_id, "❌ کد مالک معتبر نیست.", thread_id=thread_id)
            return
        state["owners"] = [int(user_id)]
        _save_verified(control, state, "chore: claim Telegram bot owner [automated]")
        control.write_repo_json(
            BOOTSTRAP_PATH,
            control.CONTROL_BRANCH,
            {"claim_sha256": "", "claimed": True},
            "chore: consume Telegram owner bootstrap [automated]",
        )
        control.safe_send_text(
            chat_id,
            "✅ مالک اصلی بات ثبت شد. حالا بات را در کانال/گروه موردنظر ادمین کن و /destinations را بزن.",
            thread_id=thread_id,
            keyboard=menu_keyboard(),
        )

    def _resolve_selector(state: Dict[str, Any], selector: str):
        item = find_destination(state, selector)
        if item is None:
            raise DestinationValidationError("مقصد با این شماره/شناسه پیدا نشد")
        return item

    def _persist_and_wake(state: Dict[str, Any], message: str) -> Dict[str, Any]:
        persisted = _save_verified(control, state, message)
        if control.current_enabled() and active_destinations(persisted):
            control.ensure_publisher_run()
        return persisted

    def process_action(action, *, user_id, chat_id, thread_id, callback_id=None):
        destination_actions = {
            "menu", "owner_claim", "dest_list", "dest_add", "dest_here", "dest_on", "dest_off",
            "dest_remove", "dest_delay", "delay_all", "dest_toggle", "dest_member_event",
        }
        if action not in destination_actions:
            return original_process_action(
                action,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                callback_id=callback_id,
            )

        if action == "owner_claim":
            argument = _pop(action, user_id, chat_id, thread_id).strip()
            try:
                claim_chat = _chat_snapshot(control, chat_id)
            except Exception as exc:
                raise control.RetryableCommandError(f"could not verify owner claim chat: {exc}") from exc
            if str(claim_chat.get("type") or "") != "private":
                control.safe_send_text(chat_id, "🔐 ثبت مالک فقط در پیام خصوصی بات انجام می‌شود.", thread_id=thread_id)
                return
            _claim_owner(user_id, chat_id, thread_id, argument)
            return

        if action == "menu":
            if not _is_owner(control, user_id):
                control.safe_send_text(
                    chat_id,
                    "🔐 کنترل جدید هنوز مالک ندارد.\n\n"
                    "در پیام خصوصی بات، کد یک‌بارمصرف مالک را با این دستور ثبت کن:\n"
                    "/owner_claim <کد>\n\n"
                    "بعد از آن بات را در هر کانال یا گروهی که می‌خواهی ادمین کن.",
                    thread_id=thread_id,
                )
                return
            control.safe_send_text(
                chat_id,
                "کنترل انتشار کانفیگ‌های رایگان:",
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
            return

        if action == "dest_member_event":
            event = _EVENTS.pop((user_id, chat_id), {})
            new_member = event.get("new_chat_member") if isinstance(event, dict) else {}
            chat = event.get("chat") if isinstance(event, dict) else {}
            status = str((new_member or {}).get("status") or "")
            state = _load(control)
            existing = next((x for x in state.get("destinations", []) if int(x.get("chat_id", 0)) == int(chat_id) and x.get("thread_id") is None), None)

            # Registration is automatic only when the already-claimed owner performed the promotion.
            if status in {"administrator", "creator"}:
                if not _is_owner(control, user_id):
                    return
                ready = not (
                    str((chat or {}).get("type") or "") == "channel"
                    and (new_member or {}).get("can_post_messages") is not True
                )
                upsert_destination(
                    state,
                    chat_id=chat_id,
                    chat_type=str((chat or {}).get("type") or "unknown"),
                    title=str((chat or {}).get("title") or (chat or {}).get("username") or "بدون نام"),
                    username=str((chat or {}).get("username") or ""),
                    ready=ready,
                )
                _save_verified(control, state, "chore: register Telegram destination [automated]")
                return

            if existing is not None and status in {"left", "kicked", "member", "restricted"}:
                existing["enabled"] = False
                existing["status"] = "removed" if status in {"left", "kicked"} else "missing_permission"
                _save_verified(control, state, "chore: disable unavailable Telegram destination [automated]")
            return

        if not _is_owner(control, user_id):
            if action == "dest_list" and not _owners(control):
                control.safe_send_text(
                    chat_id,
                    "🔐 هنوز مالک اصلی بات ثبت نشده. ابتدا در پیام خصوصی /owner_claim <کد> را بفرست.",
                    thread_id=thread_id,
                )
                return
            if callback_id:
                control.answer_callback(callback_id, "فقط مالک اصلی بات دسترسی دارد")
            control.safe_send_text(chat_id, "⛔ فقط مالک اصلی بات اجازه این تغییر را دارد.", thread_id=thread_id)
            return

        state = _load(control)

        if action == "dest_list":
            if callback_id:
                control.answer_callback(callback_id, "لیست مقصدها به‌روز شد")
            control.safe_send_text(chat_id, _destination_text(state), thread_id=thread_id, keyboard=_dest_keyboard(state))
            return

        if action == "dest_here":
            chat_info = _chat_snapshot(control, chat_id)
            if str(chat_info.get("type") or "") == "private":
                control.safe_send_text(chat_id, "این دستور را داخل گروه/سوپرگروه مقصد بفرست.", thread_id=thread_id)
                return
            ok, reason = _validate_owner_and_bot_admin(control, user_id, chat_id)
            if not ok:
                control.safe_send_text(chat_id, f"❌ مقصد ثبت نشد: {reason}", thread_id=thread_id)
                return
            upsert_destination(
                state,
                chat_id=chat_id,
                chat_type=str(chat_info.get("type") or "unknown"),
                title=str(chat_info.get("title") or chat_info.get("username") or "بدون نام"),
                username=str(chat_info.get("username") or ""),
                thread_id=thread_id,
                ready=True,
            )
            persisted = _persist_and_wake(state, "chore: register current Telegram destination [automated]")
            control.safe_send_text(chat_id, "✅ این گروه/تاپیک به‌عنوان مقصد ثبت شد و فعلاً خاموش است. /destinations", thread_id=thread_id)
            return

        if action == "dest_add":
            argument = _pop(action, user_id, chat_id, thread_id).strip()
            if not argument:
                control.safe_send_text(chat_id, "مثال: /dest_add @channelusername", thread_id=thread_id)
                return
            ref: int | str = argument
            try:
                if argument.lstrip("-").isdigit():
                    ref = int(argument)
                chat_info = _chat_snapshot(control, ref)
                target_id = int(chat_info.get("id"))
                ok, reason = _validate_owner_and_bot_admin(control, user_id, target_id)
            except Exception as exc:
                raise control.RetryableCommandError(f"could not inspect Telegram destination: {exc}") from exc
            if not ok:
                control.safe_send_text(chat_id, f"❌ مقصد ثبت نشد: {reason}", thread_id=thread_id)
                return
            upsert_destination(
                state,
                chat_id=target_id,
                chat_type=str(chat_info.get("type") or "unknown"),
                title=str(chat_info.get("title") or chat_info.get("username") or "بدون نام"),
                username=str(chat_info.get("username") or ""),
                ready=True,
            )
            _persist_and_wake(state, "chore: add Telegram destination [automated]")
            control.safe_send_text(chat_id, "✅ مقصد ثبت شد و برای جلوگیری از ارسال ناخواسته فعلاً خاموش است. /destinations", thread_id=thread_id)
            return

        if action == "delay_all":
            argument = _pop(action, user_id, chat_id, thread_id)
            parts = argument.split()
            if len(parts) != 2:
                control.safe_send_text(chat_id, "مثال: /delay 30 90", thread_id=thread_id)
                return
            try:
                low, high = validate_delay(int(parts[0]), int(parts[1]))
            except (ValueError, DestinationValidationError) as exc:
                control.safe_send_text(chat_id, f"❌ بازه معتبر نیست: {exc}", thread_id=thread_id)
                return
            state["default_min_delay"], state["default_max_delay"] = low, high
            for item in state.get("destinations", []):
                item["min_delay"], item["max_delay"] = low, high
            persisted = _persist_and_wake(state, "chore: update Telegram destination delays [automated]")
            control.safe_send_text(chat_id, f"✅ فاصله همه مقصدها روی {low} تا {high} ثانیه تنظیم شد.", thread_id=thread_id, keyboard=_dest_keyboard(persisted))
            return

        if action == "dest_delay":
            argument = _pop(action, user_id, chat_id, thread_id)
            parts = argument.split()
            if len(parts) != 3:
                control.safe_send_text(chat_id, "مثال: /dest_delay 2 60 120", thread_id=thread_id)
                return
            try:
                item = _resolve_selector(state, parts[0])
                low, high = validate_delay(int(parts[1]), int(parts[2]))
            except (ValueError, DestinationValidationError) as exc:
                control.safe_send_text(chat_id, f"❌ تنظیم انجام نشد: {exc}", thread_id=thread_id)
                return
            item["min_delay"], item["max_delay"] = low, high
            persisted = _persist_and_wake(state, "chore: update Telegram destination delay [automated]")
            control.safe_send_text(chat_id, f"✅ فاصله «{item.get('title')}» روی {low} تا {high} ثانیه تنظیم شد.", thread_id=thread_id, keyboard=_dest_keyboard(persisted))
            return

        if action in {"dest_on", "dest_off", "dest_toggle", "dest_remove"}:
            argument = _pop(action, user_id, chat_id, thread_id).strip()
            try:
                item = _resolve_selector(state, argument)
            except DestinationValidationError as exc:
                control.safe_send_text(chat_id, f"❌ {exc}", thread_id=thread_id)
                return

            if action == "dest_remove":
                state["destinations"] = [x for x in state.get("destinations", []) if x.get("id") != item.get("id")]
                persisted = _save_verified(control, state, "chore: remove Telegram destination [automated]")
                control.safe_send_text(chat_id, f"🗑 مقصد «{item.get('title')}» حذف شد.", thread_id=thread_id, keyboard=_dest_keyboard(persisted))
                return

            target_enabled = not bool(item.get("enabled")) if action == "dest_toggle" else action == "dest_on"
            if target_enabled and item.get("status") != "ready":
                control.safe_send_text(chat_id, "❌ این مقصد فعلاً permission لازم برای ارسال ندارد.", thread_id=thread_id)
                return
            item["enabled"] = target_enabled
            persisted = _persist_and_wake(state, f"chore: turn Telegram destination {'ON' if target_enabled else 'OFF'} [automated]")
            if callback_id:
                control.answer_callback(callback_id, "مقصد روشن شد" if target_enabled else "مقصد خاموش شد")
            control.safe_send_text(
                chat_id,
                f"{'✅' if target_enabled else '⛔'} مقصد «{item.get('title')}» {'فعال' if target_enabled else 'غیرفعال'} شد.",
                thread_id=thread_id,
                keyboard=_dest_keyboard(persisted),
            )
            return

    def status_text():
        try:
            registry = _load(control)
            active_count = len(active_destinations(registry))
            total = len(registry.get("destinations", []))
            low = registry.get("default_min_delay")
            high = registry.get("default_max_delay")

            publisher_state = control.read_repo_json(
                control.PUBLISHER_STATE_PATH,
                control.PUBLISHER_STATE_BRANCH,
                {"version": 2, "destinations": {}, "pending_total": 0},
            )
            target_states = publisher_state.get("destinations", {})
            delivered = 0
            if isinstance(target_states, dict):
                for target in target_states.values():
                    if isinstance(target, dict) and isinstance(target.get("sent"), list):
                        delivered += len(target["sent"])
            pending = int(publisher_state.get("pending_total", 0) or 0)

            try:
                from telegram_managed_sources import (
                    SOURCE_STATE_BRANCH,
                    SOURCE_STATE_PATH,
                    decrypt_sources,
                )
                source_payload = control.read_repo_json(
                    SOURCE_STATE_PATH,
                    SOURCE_STATE_BRANCH,
                    {},
                )
                source_count = len(decrypt_sources(source_payload))
                source_text = str(source_count)
            except Exception:
                source_text = "نامشخص"

            enabled = control.current_enabled()
            runs = control.publisher_run_count()
            return (
                f"{'🟢' if enabled else '🔴'} انتشار کلی: "
                f"{'روشن' if enabled else 'خاموش'}\n\n"
                f"📡 مقصد فعال: {active_count}/{total}\n"
                f"⏱ فاصله پیش‌فرض: {low}–{high} ثانیه\n"
                f"📤 تحویل ثبت‌شده بین مقصدها: {delivered}\n"
                f"⏳ مجموع در صف مقصدهای فعال: {pending}\n"
                f"📚 منابع اضافه از بات: {source_text}\n"
                f"⚙️ Run فعال/منتظر: {runs}"
            )
        except Exception:
            return "⚠️ وضعیت مقصدها موقتاً قابل خواندن نیست."

    control.update_to_action = update_to_action
    control.process_action = process_action
    control.register_commands = register_commands
    control.menu_keyboard = menu_keyboard
    control.status_text = status_text
    control.is_authorized_admin = is_authorized_admin
    control.current_admin_ids = current_admin_ids

    def restore() -> None:
        control.update_to_action = original_update_to_action
        control.process_action = original_process_action
        control.register_commands = original_register_commands
        control.menu_keyboard = original_menu_keyboard
        control.status_text = original_status_text
        control.is_authorized_admin = original_is_authorized_admin
        control.current_admin_ids = original_current_admin_ids
        _PENDING.clear()
        _EVENTS.clear()

    return restore


_EVENTS: Dict[Tuple[int, int], Dict[str, Any]] = {}
