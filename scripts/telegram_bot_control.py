"""Single-consumer Telegram control for the config publisher.

Only this script calls getUpdates. Every control action is authorized against
the current administrator list of the target Telegram group. No claim state is
required: a current group admin can control the publisher from the group or a
private chat with the bot.

Durable state contains only the last consumed Telegram update id. Actionable
updates that fail because of transient Telegram/GitHub errors are NOT consumed,
so the next poll retries them instead of silently losing an OFF command.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any, Dict, Tuple
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


class RetryableCommandError(RuntimeError):
    """Do not advance last_update_id; retry this Telegram command next poll."""


def _json_request(
    url: str,
    *,
    method: str = "GET",
    headers: Dict[str, str] | None = None,
    payload: Dict[str, Any] | None = None,
    timeout: int = 30,
    allow_404: bool = False,
) -> Tuple[int, Any]:
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
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if allow_404 and exc.code == 404:
            return 404, {}
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body[:500]}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"network error for {url}: {exc}") from exc


def telegram_api(method: str, payload: Dict[str, Any] | None = None) -> Any:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    _, response = _json_request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        method="POST",
        payload=payload or {},
        timeout=35,
    )
    if not isinstance(response, dict) or not response.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {response}")
    return response.get("result")


def github_headers() -> Dict[str, str]:
    if not GH_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": API_VERSION,
    }


def github_api(
    path: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    allow_404: bool = False,
) -> Tuple[int, Any]:
    return _json_request(
        f"https://api.github.com/repos/{REPOSITORY}{path}",
        method=method,
        headers=github_headers(),
        payload=payload,
        allow_404=allow_404,
    )


def read_repo_json(path: str, ref: str, default: Dict[str, Any]) -> Dict[str, Any]:
    encoded_path = quote(path, safe="/")
    status, item = github_api(
        f"/contents/{encoded_path}?ref={quote(ref, safe='')}",
        allow_404=True,
    )
    if status == 404:
        return dict(default)

    try:
        raw = base64.b64decode(str(item["content"]).replace("\n", ""))
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else dict(default)
    except Exception as exc:
        raise RuntimeError(f"invalid JSON at {ref}:{path}: {exc}") from exc


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
            if status != 404 and isinstance(current, dict) and current.get("sha"):
                body["sha"] = current["sha"]

            github_api(f"/contents/{encoded_path}", method="PUT", payload=body)
            return
        except Exception as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(attempt * 2)

    raise RuntimeError(f"could not persist {branch}:{path}: {last_error}")


def load_bot_state() -> Dict[str, Any]:
    state = read_repo_json(BOT_STATE_PATH, BOT_STATE_BRANCH, {"last_update_id": 0})
    try:
        last_update_id = int(state.get("last_update_id", 0) or 0)
    except (TypeError, ValueError):
        last_update_id = 0
    return {"last_update_id": max(0, last_update_id)}


def save_bot_state(state: Dict[str, Any]) -> None:
    write_repo_json(
        BOT_STATE_PATH,
        BOT_STATE_BRANCH,
        {"last_update_id": int(state.get("last_update_id", 0) or 0)},
        "chore: update Telegram bot control offset [automated]",
    )


def current_enabled() -> bool:
    data = read_repo_json(CONTROL_PATH, CONTROL_BRANCH, {"enabled": False})
    return data.get("enabled") is True


def publisher_run_count() -> int:
    _, data = github_api(f"/actions/workflows/{PUBLISHER_WORKFLOW}/runs?per_page=30")
    if not isinstance(data, dict):
        return 0
    return sum(
        1
        for run in data.get("workflow_runs", [])
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


def publisher_metrics() -> Tuple[int, int, int]:
    state = read_repo_json(
        PUBLISHER_STATE_PATH,
        PUBLISHER_STATE_BRANCH,
        {"queue": [], "sent": []},
    )
    queue = state.get("queue", []) if isinstance(state.get("queue"), list) else []
    sent = state.get("sent", []) if isinstance(state.get("sent"), list) else []
    return len(queue), len(sent), publisher_run_count()


def current_admin_ids() -> set[int]:
    """Return current human administrator IDs of the target group."""
    try:
        members = telegram_api("getChatAdministrators", {"chat_id": TARGET_CHAT_ID})
    except Exception as exc:
        raise RetryableCommandError(f"could not verify group administrators: {exc}") from exc

    if not isinstance(members, list):
        raise RetryableCommandError("Telegram returned invalid administrator list")

    result: set[int] = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        user = member.get("user")
        if not isinstance(user, dict):
            continue
        try:
            user_id = int(user.get("id"))
        except (TypeError, ValueError):
            continue
        if not bool(user.get("is_bot", False)):
            result.add(user_id)
    return result


def is_authorized_admin(user_id: int) -> bool:
    return user_id in current_admin_ids()


def message_thread_id(message: Dict[str, Any]) -> int | None:
    try:
        value = message.get("message_thread_id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def send_text(
    chat_id: int,
    text: str,
    *,
    thread_id: int | None = None,
    keyboard: Dict[str, Any] | None = None,
) -> None:
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


def safe_send_text(
    chat_id: int,
    text: str,
    *,
    thread_id: int | None = None,
    keyboard: Dict[str, Any] | None = None,
) -> None:
    try:
        send_text(chat_id, text, thread_id=thread_id, keyboard=keyboard)
    except Exception as exc:
        print(f"[bot-control] reply failed: {exc}", file=sys.stderr, flush=True)


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        payload: Dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        telegram_api("answerCallbackQuery", payload)
    except Exception as exc:
        print(f"[bot-control] callback answer failed: {exc}", file=sys.stderr, flush=True)


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
    return (
        f"{'🟢' if enabled else '🔴'} انتشار کانفیگ: "
        f"{'روشن' if enabled else 'خاموش'}\n\n"
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
    ]
    telegram_api("setMyCommands", {"commands": commands})


def normalize_command(text: Any) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    token = value.split(maxsplit=1)[0].lower()
    if token.startswith("/") and "@" in token:
        token = token.split("@", 1)[0]
    return token


def update_to_action(
    update: Dict[str, Any],
) -> Tuple[str | None, int | None, int | None, int | None, str | None]:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from")
        message = callback.get("message")
        if not isinstance(sender, dict) or not isinstance(message, dict):
            return None, None, None, None, None

        mapping = {
            "publisher:on": "on",
            "publisher:off": "off",
            "publisher:status": "status",
        }
        action = mapping.get(str(callback.get("data") or ""))
        if action is None:
            return None, None, None, None, None

        try:
            user_id = int(sender.get("id"))
            chat_id = int((message.get("chat") or {}).get("id"))
        except (TypeError, ValueError):
            return None, None, None, None, None

        return (
            action,
            user_id,
            chat_id,
            message_thread_id(message),
            str(callback.get("id") or ""),
        )

    message = update.get("message")
    if not isinstance(message, dict):
        return None, None, None, None, None

    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None, None, None, None, None

    mapping = {
        "/start": "menu",
        "/publisher": "menu",
        "/publisher_on": "on",
        "/publisher_off": "off",
        "/publisher_status": "status",
        "/publisher_claim": "menu",
    }
    action = mapping.get(normalize_command(message.get("text")))
    if action is None:
        return None, None, None, None, None

    try:
        user_id = int(sender.get("id"))
        chat_id = int(chat.get("id"))
    except (TypeError, ValueError):
        return None, None, None, None, None

    return action, user_id, chat_id, message_thread_id(message), None


def process_action(
    action: str,
    *,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    callback_id: str | None = None,
) -> None:
    if not is_authorized_admin(user_id):
        if callback_id:
            answer_callback(callback_id, "فقط ادمین‌های گروه دسترسی دارند")
        safe_send_text(
            chat_id,
            "⛔ فقط ادمین‌های فعلی گروه Broute اجازه کنترل Publisher را دارند.",
            thread_id=thread_id,
        )
        return

    if action == "menu":
        safe_send_text(
            chat_id,
            "کنترل انتشار کانفیگ‌های رایگان:",
            thread_id=thread_id,
            keyboard=menu_keyboard(),
        )
        return

    if action == "status":
        if callback_id:
            answer_callback(callback_id, "وضعیت به‌روز شد")
        try:
            text = status_text()
        except Exception as exc:
            raise RetryableCommandError(f"could not read publisher status: {exc}") from exc
        safe_send_text(chat_id, text, thread_id=thread_id, keyboard=menu_keyboard())
        return

    if action not in {"on", "off"}:
        return

    enabled = action == "on"
    try:
        changed = set_enabled(enabled)
        text = status_text()
    except Exception as exc:
        raise RetryableCommandError(f"could not apply {action}: {exc}") from exc

    if callback_id:
        answer_callback(
            callback_id,
            "Publisher روشن شد" if enabled else "Publisher خاموش شد",
        )

    if enabled:
        prefix = "✅ Publisher روشن شد." if changed else "ℹ️ Publisher از قبل روشن بود."
    else:
        prefix = (
            "⛔ Publisher خاموش شد. صف و تاریخچه حفظ شدند."
            if changed
            else "ℹ️ Publisher از قبل خاموش بود."
        )

    safe_send_text(
        chat_id,
        f"{prefix}\n\n{text}",
        thread_id=thread_id,
        keyboard=menu_keyboard(),
    )


def main() -> int:
    if not BOT_TOKEN or not GH_TOKEN:
        print(
            "[bot-control] TELEGRAM_BOT_TOKEN and GITHUB_TOKEN are required.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    try:
        state = load_bot_state()
    except Exception as exc:
        print(f"[bot-control] state load failed: {exc}", file=sys.stderr, flush=True)
        return 1

    try:
        register_commands()
    except Exception as exc:
        print(
            f"[bot-control] command registration failed (continuing): {exc}",
            file=sys.stderr,
            flush=True,
        )

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
        print(
            "[bot-control] Telegram returned a non-list update payload.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    handled = 0
    ignored = 0

    for update in updates:
        if not isinstance(update, dict):
            ignored += 1
            continue

        try:
            update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            ignored += 1
            continue

        action, user_id, chat_id, thread_id, callback_id = update_to_action(update)

        if action is None or user_id is None or chat_id is None:
            state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)
            try:
                save_bot_state(state)
            except Exception as exc:
                print(
                    f"[bot-control] failed to checkpoint ignored update {update_id}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            ignored += 1
            continue

        print(
            f"[bot-control] update={update_id} action={action} "
            f"user={user_id} chat={chat_id}",
            flush=True,
        )

        try:
            process_action(
                action,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                callback_id=callback_id,
            )
        except RetryableCommandError as exc:
            print(
                f"[bot-control] transient command failure; update {update_id} "
                f"will be retried: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1
        except Exception as exc:
            print(
                f"[bot-control] unexpected command failure; update {update_id} "
                f"will be retried: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1

        state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)
        try:
            save_bot_state(state)
        except Exception as exc:
            print(
                f"[bot-control] command applied but offset checkpoint failed for "
                f"{update_id}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1

        handled += 1

    print(
        f"[bot-control] checked {len(updates)} update(s); "
        f"handled {handled}; ignored {ignored}; "
        f"last_update_id={state.get('last_update_id', 0)}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
