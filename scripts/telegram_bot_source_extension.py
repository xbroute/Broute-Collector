"""Extend Telegram Bot Control with encrypted subscription-source management."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Tuple

from telegram_managed_sources import (
    MAX_MANAGED_SOURCES,
    SOURCE_STATE_BRANCH,
    SOURCE_STATE_PATH,
    SourceCryptoError,
    SourceValidationError,
    decrypt_sources,
    encrypt_sources,
    new_source_entry,
    normalize_subscription_url,
    source_host,
    validate_subscription,
)

ACTIVE_RUN_STATUSES = {"queued", "in_progress", "pending", "requested", "waiting"}
COLLECTOR_WORKFLOW = "update-subscription.yml"

# The base control main() deliberately passes only a compact action tuple to
# process_action. Keep sensitive URL arguments in memory, keyed by the clean
# action/user/chat tuple, so the base logger never prints a raw subscription URL.
_PENDING_ARGS: Dict[Tuple[str, int, int, int | None], List[str]] = defaultdict(list)


def _key(action: str, user_id: int, chat_id: int, thread_id: int | None):
    return action, user_id, chat_id, thread_id


def _stash_arg(action: str, user_id: int, chat_id: int, thread_id: int | None, value: str) -> None:
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


def _load_sources(control: Any) -> List[Dict[str, Any]]:
    payload = control.read_repo_json(SOURCE_STATE_PATH, SOURCE_STATE_BRANCH, {})
    try:
        return decrypt_sources(payload)
    except SourceCryptoError as exc:
        raise control.RetryableCommandError(f"could not decrypt subscription sources: {exc}") from exc


def _save_sources_verified(control: Any, sources: List[Dict[str, Any]], message: str) -> None:
    payload = encrypt_sources(sources)
    control.write_repo_json(SOURCE_STATE_PATH, SOURCE_STATE_BRANCH, payload, message)

    # Never tell the admin that a mutation succeeded until the encrypted state
    # can be read back and the exact source IDs match.
    verify_payload = control.read_repo_json(SOURCE_STATE_PATH, SOURCE_STATE_BRANCH, {})
    try:
        persisted = decrypt_sources(verify_payload)
    except SourceCryptoError as exc:
        raise control.RetryableCommandError(f"could not verify persisted sources: {exc}") from exc

    expected_ids = [str(item.get("id") or "") for item in sources]
    persisted_ids = [str(item.get("id") or "") for item in persisted]
    if persisted_ids != expected_ids:
        raise control.RetryableCommandError("subscription source read-back mismatch")


def _workflow_active(control: Any, workflow: str) -> bool:
    _, data = control.github_api(f"/actions/workflows/{workflow}/runs?per_page=30")
    if not isinstance(data, dict):
        return False
    return any(
        str(run.get("status") or "") in ACTIVE_RUN_STATUSES
        for run in data.get("workflow_runs", [])
        if isinstance(run, dict)
    )


def _ensure_collector(control: Any) -> None:
    if _workflow_active(control, COLLECTOR_WORKFLOW):
        return
    control.github_api(
        f"/actions/workflows/{COLLECTOR_WORKFLOW}/dispatches",
        method="POST",
        payload={"ref": "main"},
    )


def _source_list_text(sources: List[Dict[str, Any]]) -> str:
    if not sources:
        return (
            "📚 هنوز subscription اضافه‌ای ثبت نشده.\n\n"
            "لینک را مستقیم در Private Chat بفرست یا از این دستور استفاده کن:\n"
            "/source_add https://example.com/sub"
        )

    lines = ["📚 منابع subscription اضافه‌شده:", ""]
    for index, source in enumerate(sources, 1):
        host = source_host(str(source.get("url") or ""))
        count = int(source.get("last_validated_configs", 0) or 0)
        state = "🟢" if source.get("enabled") is True else "⚪"
        lines.append(f"{index}. {state} {host} — {count} کانفیگ هنگام ثبت")
    lines.extend(
        [
            "",
            "➕ افزودن: لینک را مستقیم بفرست یا /source_add <url>",
            "🗑 حذف: /source_remove <شماره>",
        ]
    )
    return "\n".join(lines)


def install(control: Any) -> Callable[[], None]:
    original_update_to_action = control.update_to_action
    original_process_action = control.process_action
    original_register_commands = control.register_commands
    original_menu_keyboard = control.menu_keyboard
    original_status_text = control.status_text

    def menu_keyboard():
        keyboard = original_menu_keyboard()
        rows = list(keyboard.get("inline_keyboard", []))
        rows.append([{"text": "📚 منابع", "callback_data": "source:list"}])
        return {"inline_keyboard": rows}

    def register_commands():
        # Replace the base set in one request so Telegram's command menu remains
        # deterministic and includes the new source-management commands.
        commands = [
            {"command": "publisher", "description": "کنترل انتشار کانفیگ‌ها"},
            {"command": "publisher_on", "description": "روشن کردن انتشار"},
            {"command": "publisher_off", "description": "خاموش کردن انتشار"},
            {"command": "publisher_status", "description": "وضعیت انتشار"},
            {"command": "source_add", "description": "افزودن لینک subscription"},
            {"command": "source_list", "description": "نمایش subscriptionهای اضافه"},
            {"command": "source_remove", "description": "حذف subscription با شماره"},
        ]
        control.telegram_api("setMyCommands", {"commands": commands})

    def update_to_action(update):
        callback = update.get("callback_query") if isinstance(update, dict) else None
        if isinstance(callback, dict) and str(callback.get("data") or "") == "source:list":
            sender = callback.get("from")
            message = callback.get("message")
            if isinstance(sender, dict) and isinstance(message, dict):
                try:
                    user_id = int(sender.get("id"))
                    chat_id = int((message.get("chat") or {}).get("id"))
                except (TypeError, ValueError):
                    return None, None, None, None, None
                return (
                    "source_list",
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
                try:
                    user_id = int(sender.get("id"))
                    chat_id = int(chat.get("id"))
                except (TypeError, ValueError):
                    user_id = 0
                    chat_id = 0

                thread_id = control.message_thread_id(message)
                text = str(message.get("text") or "").strip()
                command = control.normalize_command(text)
                rest = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""

                if command in {"/source_list", "/sources"} and user_id and chat_id:
                    return "source_list", user_id, chat_id, thread_id, None

                if command == "/source_add" and user_id and chat_id:
                    _stash_arg("source_add", user_id, chat_id, thread_id, rest)
                    return "source_add", user_id, chat_id, thread_id, None

                if command == "/source_remove" and user_id and chat_id:
                    _stash_arg("source_remove", user_id, chat_id, thread_id, rest)
                    return "source_remove", user_id, chat_id, thread_id, None

                # Convenience mode requested by the admin: in a private chat,
                # pasting a bare subscription URL is exactly /source_add <url>.
                if (
                    str(chat.get("type") or "") == "private"
                    and user_id
                    and chat_id
                    and (text.startswith("https://") or text.startswith("http://"))
                ):
                    _stash_arg("source_add", user_id, chat_id, thread_id, text)
                    return "source_add", user_id, chat_id, thread_id, None

        return original_update_to_action(update)

    def status_text():
        enabled = control.current_enabled()
        state = control.read_repo_json(
            control.PUBLISHER_STATE_PATH,
            control.PUBLISHER_STATE_BRANCH,
            {"queue": [], "sent": [], "cycle": 1, "cycle_sent": []},
        )
        queue = state.get("queue", []) if isinstance(state.get("queue"), list) else []
        sent = state.get("sent", []) if isinstance(state.get("sent"), list) else []
        cycle_sent = state.get("cycle_sent", []) if isinstance(state.get("cycle_sent"), list) else []
        try:
            cycle = max(1, int(state.get("cycle", 1) or 1))
        except (TypeError, ValueError):
            cycle = 1
        active = control.publisher_run_count()
        try:
            source_count = len(_load_sources(control))
        except Exception:
            source_count = -1
        source_text = str(source_count) if source_count >= 0 else "نامشخص"
        return (
            f"{'🟢' if enabled else '🔴'} انتشار کانفیگ: "
            f"{'روشن' if enabled else 'خاموش'}\n\n"
            f"🔄 دور انتشار: {cycle}\n"
            f"📤 ارسال‌شده در این دور: {len(cycle_sent)}\n"
            f"🧾 کانفیگ یکتای ارسال‌شده: {len(sent)}\n"
            f"⏳ در صف: {len(queue)}\n"
            f"📚 منابع اضافه از بات: {source_text}\n"
            f"⚙️ Run فعال/منتظر: {active}"
        )

    def process_action(action, *, user_id, chat_id, thread_id, callback_id=None):
        if action not in {"source_add", "source_list", "source_remove"}:
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
                "⛔ فقط ادمین‌های فعلی گروه Broute اجازه مدیریت منابع را دارند.",
                thread_id=thread_id,
            )
            return

        if action == "source_list":
            sources = _load_sources(control)
            if callback_id:
                control.answer_callback(callback_id, "لیست منابع به‌روز شد")
            control.safe_send_text(
                chat_id,
                _source_list_text(sources),
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
            return

        argument = _pop_arg(action, user_id, chat_id, thread_id).strip()

        if action == "source_add":
            if not argument:
                control.safe_send_text(
                    chat_id,
                    "لینک subscription را مستقیم بفرست یا بنویس:\n/source_add https://example.com/sub",
                    thread_id=thread_id,
                )
                return

            try:
                canonical = normalize_subscription_url(argument)
            except SourceValidationError as exc:
                control.safe_send_text(chat_id, f"❌ لینک پذیرفته نشد: {exc}", thread_id=thread_id)
                return

            sources = _load_sources(control)
            if len(sources) >= MAX_MANAGED_SOURCES:
                control.safe_send_text(
                    chat_id,
                    f"❌ سقف {MAX_MANAGED_SOURCES} منبع اضافه پر شده است.",
                    thread_id=thread_id,
                )
                return

            if any(str(item.get("url") or "") == canonical for item in sources):
                control.safe_send_text(
                    chat_id,
                    "ℹ️ این subscription از قبل در منابع وجود دارد.",
                    thread_id=thread_id,
                    keyboard=menu_keyboard(),
                )
                return

            control.safe_send_text(
                chat_id,
                "🔎 در حال بررسی subscription و استخراج نمونه کانفیگ‌ها…",
                thread_id=thread_id,
            )
            try:
                canonical, count = validate_subscription(canonical)
            except SourceValidationError as exc:
                control.safe_send_text(
                    chat_id,
                    f"❌ subscription اضافه نشد: {exc}",
                    thread_id=thread_id,
                )
                return

            entry = new_source_entry(canonical, count)
            sources.append(entry)
            try:
                _save_sources_verified(
                    control,
                    sources,
                    "chore: add encrypted Telegram-managed subscription [automated]",
                )
                _ensure_collector(control)
            except Exception as exc:
                raise control.RetryableCommandError(
                    f"could not persist/activate subscription source: {exc}"
                ) from exc

            control.safe_send_text(
                chat_id,
                "✅ subscription اضافه شد و Collector برای بررسی کامل اجرا شد.\n\n"
                f"🌐 میزبان: {source_host(canonical)}\n"
                f"🧩 کانفیگ قابل‌شناسایی هنگام ثبت: {count}\n"
                "🔐 URL خام به‌صورت عمومی در ریپو ذخیره نشده است.",
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
            return

        if action == "source_remove":
            if not argument:
                control.safe_send_text(
                    chat_id,
                    "شماره منبع را بده؛ مثال: /source_remove 2",
                    thread_id=thread_id,
                )
                return

            sources = _load_sources(control)
            if not sources:
                control.safe_send_text(chat_id, "📚 لیست منابع خالی است.", thread_id=thread_id)
                return

            index = None
            try:
                candidate = int(argument)
                if 1 <= candidate <= len(sources):
                    index = candidate - 1
            except ValueError:
                for i, source in enumerate(sources):
                    if str(source.get("id") or "").startswith(argument):
                        index = i
                        break

            if index is None:
                control.safe_send_text(
                    chat_id,
                    "❌ منبع با این شماره/شناسه پیدا نشد. /source_list را ببین.",
                    thread_id=thread_id,
                )
                return

            removed = sources.pop(index)
            try:
                _save_sources_verified(
                    control,
                    sources,
                    "chore: remove encrypted Telegram-managed subscription [automated]",
                )
                _ensure_collector(control)
            except Exception as exc:
                raise control.RetryableCommandError(
                    f"could not remove/refresh subscription source: {exc}"
                ) from exc

            control.safe_send_text(
                chat_id,
                f"🗑 منبع {source_host(str(removed.get('url') or ''))} حذف شد و Collector refresh شد.",
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )

    control.update_to_action = update_to_action
    control.process_action = process_action
    control.register_commands = register_commands
    control.menu_keyboard = menu_keyboard
    control.status_text = status_text

    def restore() -> None:
        control.update_to_action = original_update_to_action
        control.process_action = original_process_action
        control.register_commands = original_register_commands
        control.menu_keyboard = original_menu_keyboard
        control.status_text = original_status_text
        _PENDING_ARGS.clear()

    return restore
