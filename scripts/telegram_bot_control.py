"""Control the Telegram config publisher from the existing Telegram bot.

This script is designed for a short GitHub Actions poller. It reads Bot API
updates, authorizes a single controller via an admin-only claim in the target
Telegram group, and then lets that controller turn the publisher ON/OFF or read
its status from either the group or a private chat with the bot.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "xbroute/Broute-Collector").strip()
TARGET_CHAT_ID = int(os.environ.get("TELEGRAM_CONTROL_CHAT_ID", "-1004469276021"))

CONTROL_PATH = ".github/telegram-publisher-control.json"
CONTROL_BRANCH = "main"
BOT_STATE_PATH = "telegram_bot_control_state.json"
BOT_STATE_BRANCH = "telegram-bot-state"
PUBLISHER_STATE_PATH = "telegram_state.json"
PUBLISHER_STATE_BRANCH = "telegram-state"
PUBLISHER_WORKFLOW = "publish-telegram.yml"

API_VERSION = "2022-11-28"
ACTIVE_RUN_STATUSES = {"queued", "in_progress", "pending", "requested", "waiting"}


def _json_request(url: str, *, method: str = "GET", headers: Dict[str, str] | None = None,
                  payload: Dict[str, Any] | None = None, timeout: int = 30,
                  allow_404: bool = False) -> Tuple[int, Dict[str, Any]]:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if allow_404 and exc.code == 404:
            return 404, {}
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body[:500]}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"network error for {url}: {exc}") from exc


def telegram_api(method: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    _, result = _json_request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        method="POST",
        payload=payload or {},
        timeout=35,
    )
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {result}")
    return result.get("result")


def github_headers() -> Dict[str, str]:
    if not GH_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": API_VERSION,
    }


def github_api(path: str, *, method: str = "GET", payload: Dict[str, Any] | None = None,
               allow_404: bool = False) -> Tuple[int, Dict[str, Any]]:
    return _json_request(
        f"https://api.github.com/repos/{REPOSITORY}{path}",
        method=method,
        headers=github_headers(),
        payload=payload,
        allow_404=allow_404,
    )


def read_repo_json(path: str, ref: str, default: Dict[str, Any]) -> Dict[str, Any]:
    encoded_path = quote(path, safe="/")
    status, item = github_api(f"/contents/{encoded_path}?ref={quote(ref, safe='')}", allow_404=True)
    if status == 404:
        return dict(default)
    try:
        raw = base64.b64decode(str(item["content"]).replace("\n", ""))
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else dict(default)
    except Exception as exc:
        print(f"[bot-control] invalid JSON at {ref}:{path}: {exc}", file=sys.stderr, flush=True)
        return dict(default)


def write_repo_json(path: str, branch: str, payload: Dict[str, Any], message: str) -> None:
    encoded_path = quote(path, safe="/")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("ascii")

    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            status, current = github_api(
                f"/contents/{encoded_path}?ref={quote(branch, safe='')}",
                allow_404=True,
            )
            body: Dict[str, Any] = {
                "message": message,
                "content": encoded_content,
                "branch": branch,
            }
            if status != 404 and current.get("sha"):
                body["sha"] = current["sha"]

            github_api(f"/contents/{encoded_path}", method="PUT", payload=body)
            return
        except Exception as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(attempt * 2)
    raise RuntimeError(f"could not persist {branch}:{path}: {last_error}")


def load_bot_state() -> Dict[str, Any]:
    state = read_repo_json(
        BOT_STATE_PATH,
        BOT_STATE_BRANCH,
        {"last_update_id": 0, "authorized_user_ids": [], "commands_registered": False},
    )
    users = []
    for value in state.get("authorized_user_ids", []):
        try:
            users.append(int(value))
        except (TypeError, ValueError):
            pass
    try:
        last_update_id = int(state.get("last_update_id", 0) or 0)
    except (TypeError, ValueError):
        last_update_id = 0
    return {
        "last_update_id": max(0, last_update_id),
        "authorized_user_ids": sorted(set(users)),
        "commands_registered": bool(state.get("commands_registered", False)),
    }


def save_bot_state(state: Dict[str, Any]) -> None:
    write_repo_json(
        BOT_STATE_PATH,
        BOT_STATE_BRANCH,
        {
            "last_update_id": int(state.get("last_update_id", 0) or 0),
            "authorized_user_ids": sorted(set(int(x) for x in state.get("authorized_user_ids", []))),
            "commands_registered": bool(state.get("commands_registered", False)),
        },
        "chore: update Telegram bot control state [automated]",
    )


def current_enabled() -> bool:
    control = read_repo_json(CONTROL_PATH, CONTROL_BRANCH, {"enabled": False})
    return control.get("enabled") is True


def set_enabled(enabled: bool) -> bool:
    before = current_enabled()
    if before != enabled:
        write_repo_json(
            CONTROL_PATH,
            CONTROL_BRANCH,
            {"enabled": enabled},
            f"chore: turn Telegram publisher {'ON' if enabled else 'OFF'} via bot",
        )
    if enabled:
        ensure_publisher_run()
    return before != enabled


def publisher_run_count() -> int:
    _, data = github_api(f"/actions/workflows/{PUBLISHER_WORKFLOW}/runs?per_page=30")
    return sum(
        1 for run in data.get("workflow_runs", [])
        if str(run.get("status") or "") in ACTIVE_RUN_STATUSES
    )


def ensure_publisher_run() -> None:
    if publisher_run_count() > 0:
        return
    github_api(
        f"/actions/workflows/{PUBLISHER_WORKFLOW}/dispatches",
        method="POST",
        payload={"ref": "main"},
    )


def publisher_metrics() -> Tuple[int, int, int]:
    state = read_repo_json(
        PUBLISHER_STATE_PATH,
        PUBLISHER_STATE_BRANCH,
        {"queue": [], "sent": [], "sent_fingerprints": []},
    )
    queue = state.get("queue", []) if isinstance(state.get("queue", []), list) else []
    sent = state.get("sent", []) if isinstance(state.get("sent", []), list) else []
    return len(queue), len(sent), publisher_run_count()


def is_target_group_admin(user_id: int) -> bool:
    try:
        member = telegram_api("getChatMember", {"chat_id": TARGET_CHAT_ID, "user_id": user_id})
        return str(member.get("status") or "") in {"administrator", "creator"}
    except Exception as exc:
        print(f"[bot-control] admin verification failed for {user_id}: {exc}", file=sys.stderr, flush=True)
        return False


def message_thread_id(message: Dict[str, Any]) -> int | None:
    try:
        value = message.get("message_thread_id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def send_text(chat_id: int, text: str, *, thread_id: int | None = None,
              keyboard: Dict[str, Any] | None = None) -> None:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    telegram_api("sendMessage", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    payload: Dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
    try:
        telegram_api("answerCallbackQuery", payload)
    except Exception as exc:
        print(f"[bot-control] answerCallbackQuery failed: {exc}", file=sys.stderr, flush=True)


def menu_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 روشن", "callback_data": "publisher:on"},
                {"text": "🔴 خاموش", "callback_data": "publisher:off"},
            ],
            [{"text": "📊 وضعیت", "callback_data": "publisher:status"}],
        ]
    }


def status_text() -> str:
    enabled = current_enabled()
    pending, sent, active = publisher_metrics()
    icon = "🟢" if enabled else "🔴"
    label = "روشن" if enabled else "خاموش"
    return (
        f"{icon} انتشار کانفیگ: {label}\n\n"
        f"📤 ارسال‌شده: {sent}\n"
        f"⏳ در صف: {pending}\n"
        f"⚙️ Run فعال/منتظر: {active}"
    )


def register_commands() -> None:
    commands = [
        {"command": "publisher", "description": "کنترل انتشار کانفیگ‌ها"},
        {"command": "publisher_on", "description": "روشن کردن انتشار"},
        {"command": "publisher_off", "description": "خاموش کردن انتشار"},
        {"command": "publisher_status", "description": "وضعیت انتشار"},
        {"command": "publisher_claim", "description": "ثبت مدیر کنترل‌کننده (داخل گروه)"},
    ]
    telegram_api("setMyCommands", {"commands": commands})


def normalize_command(text: str) -> str:
    token = (text or "").strip().split(maxsplit=1)[0].lower()
    if token.startswith("/") and "@" in token:
        token = token.split("@", 1)[0]
    return token


def authorization_help() -> str:
    return (
        "⛔ دسترسی کنترل برای این اکانت ثبت نشده.\n\n"
        "برای اولین‌بار داخل گروه Broute دستور /publisher_claim را بفرست. "
        "بات ادمین‌بودنت را بررسی می‌کند؛ بعد از آن می‌توانی حتی در Private Chat از /publisher استفاده کنی."
    )


def process_action(action: str, *, user_id: int, chat_id: int, thread_id: int | None,
                   state: Dict[str, Any], callback_id: str | None = None) -> None:
    authorized = set(int(x) for x in state.get("authorized_user_ids", []))

    if action == "claim":
        if chat_id != TARGET_CHAT_ID:
            send_text(chat_id, "برای Claim اولیه این دستور را داخل گروه Broute بفرست: /publisher_claim")
            return
        if not is_target_group_admin(user_id):
            send_text(chat_id, "⛔ فقط ادمین گروه می‌تواند کنترل Publisher را Claim کند.", thread_id=thread_id)
            return
        if authorized and user_id not in authorized:
            send_text(
                chat_id,
                "⛔ کنترل قبلاً به یک مدیر دیگر اختصاص داده شده و Claim جدید خودکار پذیرفته نمی‌شود.",
                thread_id=thread_id,
            )
            return

        if user_id not in authorized:
            authorized.add(user_id)
            state["authorized_user_ids"] = sorted(authorized)
            save_bot_state(state)
        send_text(
            chat_id,
            "✅ کنترل Publisher برای اکانت تو فعال شد. از این به بعد /publisher را در گروه یا Private Chat بات بفرست.",
            thread_id=thread_id,
            keyboard=menu_keyboard(),
        )
        return

    if user_id not in authorized:
        if callback_id:
            answer_callback(callback_id, "دسترسی نداری")
        send_text(chat_id, authorization_help(), thread_id=thread_id)
        return

    try:
        if action == "on":
            changed = set_enabled(True)
            if callback_id:
                answer_callback(callback_id, "Publisher روشن شد")
            send_text(
                chat_id,
                ("✅ Publisher روشن شد.\n\n" if changed else "ℹ️ Publisher از قبل روشن بود.\n\n") + status_text(),
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
        elif action == "off":
            changed = set_enabled(False)
            if callback_id:
                answer_callback(callback_id, "Publisher خاموش شد")
            send_text(
                chat_id,
                ("⛔ Publisher خاموش شد. صف و تاریخچه حفظ شدند.\n\n" if changed else "ℹ️ Publisher از قبل خاموش بود.\n\n") + status_text(),
                thread_id=thread_id,
                keyboard=menu_keyboard(),
            )
        elif action in {"status", "menu"}:
            if callback_id:
                answer_callback(callback_id, "وضعیت به‌روز شد")
            send_text(chat_id, status_text(), thread_id=thread_id, keyboard=menu_keyboard())
    except Exception as exc:
        if callback_id:
            answer_callback(callback_id, "خطا در اعمال فرمان")
        print(f"[bot-control] action {action} failed: {exc}", file=sys.stderr, flush=True)
        send_text(chat_id, f"❌ اجرای فرمان ناموفق بود. GitHub/Telegram API خطا داد؛ وضعیت قبلی حفظ شده است.", thread_id=thread_id)


def update_to_action(update: Dict[str, Any]) -> Tuple[str | None, int | None, int | None, int | None, str | None]:
    message = update.get("message")
    callback = update.get("callback_query")

    if isinstance(callback, dict):
        sender = callback.get("from") or {}
        msg = callback.get("message") or {}
        data = str(callback.get("data") or "")
        mapping = {
            "publisher:on": "on",
            "publisher:off": "off",
            "publisher:status": "status",
        }
        action = mapping.get(data)
        if action is None:
            return None, None, None, None, None
        return (
            action,
            int(sender.get("id")),
            int((msg.get("chat") or {}).get("id")),
            message_thread_id(msg),
            str(callback.get("id") or ""),
        )

    if not isinstance(message, dict):
        return None, None, None, None, None

    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    text = str(message.get("text") or "")
    command = normalize_command(text)
    mapping = {
        "/publisher": "menu",
        "/publisher_on": "on",
        "/publisher_off": "off",
        "/publisher_status": "status",
        "/publisher_claim": "claim",
    }
    action = mapping.get(command)
    if action is None:
        return None, None, None, None, None
    return action, int(sender.get("id")), int(chat.get("id")), message_thread_id(message), None


def main() -> int:
    if not BOT_TOKEN or not GH_TOKEN:
        print("[bot-control] TELEGRAM_BOT_TOKEN and GITHUB_TOKEN are required.", file=sys.stderr)
        return 1

    state = load_bot_state()

    if not state.get("commands_registered"):
        try:
            register_commands()
            state["commands_registered"] = True
            save_bot_state(state)
            print("[bot-control] Telegram commands registered.", flush=True)
        except Exception as exc:
            print(f"[bot-control] command registration failed (continuing): {exc}", file=sys.stderr, flush=True)

    try:
        updates = telegram_api(
            "getUpdates",
            {
                "offset": int(state.get("last_update_id", 0)) + 1,
                "limit": 100,
                "timeout": 0,
                "allowed_updates": ["message", "callback_query"],
            },
        )
    except Exception as exc:
        print(f"[bot-control] getUpdates failed: {exc}", file=sys.stderr, flush=True)
        return 1

    if not isinstance(updates, list):
        print("[bot-control] Telegram returned a non-list update payload.", file=sys.stderr, flush=True)
        return 1

    handled = 0
    for update in updates:
        if not isinstance(update, dict):
            continue
        try:
            update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            continue

        action, user_id, chat_id, thread_id, callback_id = update_to_action(update)
        if action is not None and user_id is not None and chat_id is not None:
            process_action(
                action,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                state=state,
                callback_id=callback_id,
            )
            handled += 1

        state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)

    if updates:
        save_bot_state(state)

    print(
        f"[bot-control] checked {len(updates)} update(s); handled {handled}; "
        f"authorized controllers: {len(state.get('authorized_user_ids', []))}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
