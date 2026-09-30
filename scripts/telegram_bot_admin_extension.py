"""Private, role-aware administration for the existing single Telegram consumer.

Configuration, prompts and approvals live in the encrypted destination store.
Operational publishing history remains exclusively owned by the publisher.
"""
from __future__ import annotations

import base64
import copy
import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

import telegram_admin_policy as policy
import telegram_bot_destination_extension as targets
import telegram_bot_promo_extension as promo
import telegram_bot_source_extension as sources_ext
from telegram_destinations import (
    DEFAULT_TEMPLATE, MAX_DESTINATIONS, DestinationValidationError,
    find_destination, format_interval, normalize_store, normalize_template, now_iso, parse_interval_spec,
)
from telegram_managed_sources import (
    MAX_MANAGED_SOURCES, SourceValidationError, new_source_entry,
    normalize_subscription_url, source_host, validate_subscription,
)
from telegram_promo_config import promo_from_data

ROLE_LABELS = {"owner": "مالک", "admin": "مدیر", "operator": "اپراتور", "viewer": "مشاهده‌گر"}
CONFIG_FIELDS = {"enabled", "min_delay_seconds", "max_delay_seconds", "template", "message_thread_id", "filters", "schedule", "daily_limit", "pause_until"}
READ_ACTIONS = {"home", "help", "report", "health", "audit", "targets", "target_show", "target_preview",
                "queue", "target_template_show", "sources", "source_show", "admins", "admin_show", "blocked", "buy_button"}
OPERATE_ACTIONS = {"publisher_on", "publisher_off", "publisher_pause", "publisher_resume", "target_on", "target_off",
                   "target_pause", "target_resume", "queue_retry", "queue_rebuild", "queue_resolve", "collector_refresh", "targets_off"}
TEAM_ACTIONS = {"admin_add", "admin_remove"}
BACKUP_ACTIONS = {"backup", "restore"}
CONFIGURE_ACTIONS = {"target_interval", "target_template", "target_template_reset", "target_topic", "target_remove",
                     "target_filter", "target_schedule", "target_quota", "target_check", "target_rollback", "target_register", "targets_on",
                     "source_add", "source_remove", "source_on", "source_off", "source_name", "source_check",
                     "config_block", "config_unblock", "buy_link", "buy_text"}
COMMANDS = READ_ACTIONS | OPERATE_ACTIONS | TEAM_ACTIONS | BACKUP_ACTIONS | CONFIGURE_ACTIONS | {"whoami", "cancel", "confirm"}
ALIASES = {"start": "home", "admin": "home", "menu": "home", "publisher": "home", "publisher_claim": "home",
           "publisher_status": "report", "source_list": "sources", "source": "sources"}
OLD_CALLBACKS = {"publisher:on": "publisher_on", "publisher:off": "publisher_off", "publisher:status": "report",
                 "targets:list": "targets", "source:list": "sources", "promo:show": "buy_button"}


def permission_for(action: str, argument: str = "") -> str:
    if action in READ_ACTIONS or action in {"cancel", "whoami"}:
        return "view"
    if action in OPERATE_ACTIONS:
        return "operate"
    if action in TEAM_ACTIONS:
        return "team"
    if action in BACKUP_ACTIONS:
        return "backup"
    if action in {"buy_link", "buy_text", "target_template"} and not argument.strip():
        return "view"
    if action in CONFIGURE_ACTIONS:
        return "configure"
    raise policy.PolicyError("دستور مدیریت شناخته نشد؛ /help را ببینید")


def _stamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if value else "—"
    except (ValueError, TypeError, OverflowError, OSError):
        return "—"


def _button(text: str, action: str, argument: str = "") -> dict:
    data = f"adm:{action}" + (f":{argument}" if argument else "")
    if len(data.encode()) > 64:
        raise policy.PolicyError("دکمه مدیریت بیش از حد طولانی است")
    return {"text": text, "callback_data": data}


def _resolve_source(items: list[dict], selector: str) -> tuple[int, dict]:
    if selector.isdigit() and 1 <= int(selector) <= len(items):
        return int(selector) - 1, items[int(selector) - 1]
    matches = [(i, item) for i, item in enumerate(items) if selector and str(item.get("id", "")).startswith(selector)]
    if len(matches) != 1:
        raise policy.PolicyError("منبع پیدا نشد یا شناسه مبهم است؛ /sources را ببینید")
    return matches[0]


def validate_backup(value: dict) -> dict:
    if value.get("version") != 1 or not isinstance(value.get("destinations"), dict):
        raise policy.PolicyError("ساختار پشتیبان معتبر نیست")
    raw_store = value["destinations"]
    raw_targets = raw_store.get("destinations")
    if not isinstance(raw_targets, list) or len(raw_targets) > MAX_DESTINATIONS:
        raise policy.PolicyError("تعداد مقصدهای پشتیبان معتبر نیست")
    seen = set()
    for item in raw_targets:
        if not isinstance(item, dict) or not isinstance(item.get("enabled"), bool):
            raise policy.PolicyError("مقصد پشتیبان معتبر نیست")
        chat = policy.integer(item.get("chat_id"), -(2**53 - 1))
        if not chat or chat in seen:
            raise policy.PolicyError("شناسه مقصد پشتیبان تکراری یا معتبر نیست")
        seen.add(chat)
        low = policy.integer(item.get("min_delay_seconds", 30), 15, 21600)
        high = policy.integer(item.get("max_delay_seconds", 90), low, 21600)
        if not isinstance(item.get("template", DEFAULT_TEMPLATE), str):
            raise policy.PolicyError("قالب پشتیبان باید متن باشد")
        normalize_template(item.get("template", DEFAULT_TEMPLATE))
        policy.normalize_policy(item)
        if item.get("message_thread_id") is not None:
            policy.integer(item["message_thread_id"], 1)
    store = normalize_store(raw_store)
    admin = policy.administration(store)
    if "owner" not in admin["roles"].values():
        raise policy.PolicyError("پشتیبان باید حداقل یک مالک داشته باشد")
    # Runtime requests/audit are not restored. Current audit is kept at apply.
    admin.update({"confirmations": {}, "sessions": {}, "versions": {}, "audit": [], "pause_until": 0})
    store["administration"] = admin
    for item in store["destinations"]:
        item["enabled"] = False
        item["delivery_resolutions"] = {}
    raw_sources = value.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) > MAX_MANAGED_SOURCES:
        raise policy.PolicyError("تعداد منابع پشتیبان معتبر نیست")
    clean_sources, ids, urls = [], set(), set()
    for raw in raw_sources:
        if not isinstance(raw, dict) or not re.fullmatch(r"[a-f0-9]{12}", str(raw.get("id", ""))):
            raise policy.PolicyError("شناسه منبع پشتیبان معتبر نیست")
        url = normalize_subscription_url(str(raw.get("url", "")))
        if raw["id"] in ids or url in urls or not isinstance(raw.get("enabled"), bool):
            raise policy.PolicyError("منبع پشتیبان تکراری یا معتبر نیست")
        ids.add(raw["id"])
        urls.add(url)
        clean_sources.append({"id": raw["id"], "url": url, "enabled": raw["enabled"],
                              "name": str(raw.get("name", ""))[:80],
                              "added_at": str(raw.get("added_at", ""))[:64],
                              "last_validated_configs": policy.integer(raw.get("last_validated_configs", 0), 0, 1_000_000)})
    return {"version": 1, "destinations": store, "sources": clean_sources, "promo": promo_from_data(value.get("promo"))}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Telegram backup download redirect refused")


def download_backup(control: Any, document: dict) -> bytes:
    if policy.integer(document.get("file_size", policy.MAX_BACKUP_BYTES * 2 + 1), 0) > policy.MAX_BACKUP_BYTES * 2:
        raise policy.PolicyError("حجم فایل پشتیبان بیش از حد مجاز است")
    item = control.telegram_api("getFile", {"file_id": str(document.get("file_id", ""))})
    path = str(item.get("file_path", "")) if isinstance(item, dict) else ""
    if not re.fullmatch(r"[A-Za-z0-9_./-]{1,250}", path) or path.startswith("/") or ".." in path.split("/"):
        raise policy.PolicyError("مسیر فایل تلگرام معتبر نیست")
    url = f"https://api.telegram.org/file/bot{control.BOT_TOKEN}/{quote(path, safe='/')}"
    try:
        with build_opener(_NoRedirect()).open(Request(url), timeout=25) as response:
            data = response.read(policy.MAX_BACKUP_BYTES * 2 + 1)
    except Exception:
        raise control.RetryableCommandError("could not download Telegram backup document") from None
    if len(data) > policy.MAX_BACKUP_BYTES * 2:
        raise policy.PolicyError("فایل پشتیبان بیش از حد بزرگ است")
    return data


def send_backup(control: Any, chat_id: int, data: bytes) -> None:
    boundary = "broute" + secrets.token_hex(16)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"disable_notification\"\r\n\r\ntrue\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"broute-settings.broute\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    _, result = control._json_request(f"https://api.telegram.org/bot{control.BOT_TOKEN}/sendDocument", method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                                     raw_body=body, timeout=35)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise control.RetryableCommandError("Telegram encrypted backup upload failed")


class AdminPanel:
    def __init__(self, control: Any):
        self.control = control

    def load(self) -> dict:
        store = targets._load_store(self.control)
        store["administration"] = policy.administration(store)
        return store

    def save(self, store: dict, actor: int, action: str = "", resource: str = "") -> None:
        admin = store["administration"]
        for uid in policy.owner_ids_from_env():
            admin["roles"][str(uid)] = "owner"
        if action:
            policy.audit(admin, actor, action, resource, int(time.time()))
        store["manager_user_ids"] = [int(uid) for uid in admin["roles"]]
        targets._save_store_verified(self.control, store, "chore: update encrypted Telegram administration [automated]")

    def say(self, chat: int, text: str, rows: list | None = None) -> None:
        # Telegram counts UTF-16 code units. Keep every panel message bounded.
        chunks, chunk, units = [], "", 0
        for char in text:
            size = len(char.encode("utf-16-le")) // 2
            if units + size > 3500:
                chunks.append(chunk)
                chunk, units = "", 0
            chunk += char
            units += size
        chunks.append(chunk)
        for i, content in enumerate(chunks):
            keyboard = {"inline_keyboard": rows} if rows is not None and i == len(chunks) - 1 else None
            # Unlike safe_send_text, retry failed panel deliveries before moving
            # getUpdates' durable offset, especially encrypted backup requests.
            try:
                self.control.send_text(chat, content, keyboard=keyboard)
            except Exception:
                raise self.control.RetryableCommandError("could not deliver private administration reply") from None

    def bootstrap(self, store: dict, actor: int) -> bool:
        if policy.effective_roles(store):
            return False
        if actor not in targets._chat_admin_ids(self.control, self.control.TARGET_CHAT_ID):
            return False
        store["administration"]["roles"][str(actor)] = "owner"
        self.save(store, actor, "bootstrap", "control_chat")
        return True

    def home_rows(self, store: dict, actor: int) -> list:
        rows = [[_button("📡 مقصدها", "targets"), _button("📚 منابع", "sources")],
                [_button("📊 گزارش", "report"), _button("🩺 سلامت", "health")],
                [_button("👥 مدیران", "admins"), _button("🧾 سابقه", "audit")]]
        if policy.allowed(store, actor, "operate"):
            rows += [[_button("🟢 انتشار روشن", "publisher_on"), _button("⛔ انتشار خاموش", "publisher_off")],
                     [_button("⏸ توقف یک ساعت", "publisher_pause", "1h"), _button("▶️ ادامه", "publisher_resume")],
                     [_button("🔄 اجرای جمع‌آوری", "collector_refresh")]]
        if policy.allowed(store, actor, "configure"):
            rows += [[_button("🛒 تنظیم دکمه خرید", "buy_button"), _button("➕ منبع جدید", "prompt", "source_add")]]
        if policy.allowed(store, actor, "backup"):
            rows += [[_button("💾 پشتیبان", "backup"), _button("♻️ بازیابی", "restore")]]
        return rows + [[_button("🚫 مسدودی", "blocked"), _button("❔ راهنما", "help")]]

    def target(self, store: dict, selector: str) -> tuple[int, dict]:
        index, item = find_destination(store["destinations"], selector)
        if item is None:
            raise policy.PolicyError("مقصد پیدا نشد یا شناسه مبهم است؛ /targets را ببینید")
        return int(index), item

    def remember(self, store: dict, item: dict) -> None:
        versions = store["administration"]["versions"]
        key = str(item["key"])
        snapshot = {k: copy.deepcopy(item[k]) for k in CONFIG_FIELDS if k in item}
        history = versions.get(key, [])
        if not history or history[-1].get("settings") != snapshot:
            versions[key] = (history + [{"at": int(time.time()), "settings": snapshot}])[-8:]
        # Bound encrypted revisions independently of long message templates.
        while len(json.dumps(versions, ensure_ascii=False).encode()) > 100_000:
            oldest = min((k for k, entries in versions.items() if entries), key=lambda k: versions[k][0]["at"])
            versions[oldest].pop(0)

    def update_target(self, store: dict, index: int, item: dict, actor: int, action: str) -> None:
        old = store["destinations"][index]
        self.remember(store, old)
        item["updated_at"] = now_iso()
        store["destinations"][index] = item
        self.save(store, actor, action, str(item["key"]))
        if item.get("enabled") and self.control.current_enabled():
            self.control.ensure_publisher_run()

    def prompt(self, store: dict, actor: int, chat: int, argument: str) -> None:
        verb, _, selector = argument.partition(" ")
        required = permission_for(verb, "input")
        if not policy.allowed(store, actor, required):
            raise policy.PolicyError("نقش شما اجازه این تغییر را ندارد")
        if verb not in {"source_add", "target_interval", "target_template", "target_topic", "target_filter", "target_schedule",
                        "target_quota", "target_register", "source_name", "buy_link", "buy_text", "admin_add"}:
            raise policy.PolicyError("درخواست ورودی معتبر نیست")
        if verb.startswith("target_") and verb != "target_register":
            _, item = self.target(store, selector)
            selector = item["key"]
        elif verb == "source_name":
            _, item = _resolve_source(sources_ext._load_sources(self.control), selector)
            selector = item["id"]
        sessions = store["administration"]["sessions"]
        now = int(time.time())
        sessions = {k: v for k, v in sessions.items() if isinstance(v, dict) and int(v.get("expires", 0)) > now}
        sessions[str(actor)] = {"chat": chat, "verb": verb, "selector": selector, "expires": now + policy.SESSION_TTL_SECONDS}
        store["administration"]["sessions"] = sessions
        self.save(store, actor)
        self.say(chat, f"✏️ مقدار {verb} را در پیام بعدی بفرستید. مهلت ۲۰ دقیقه؛ لغو با /cancel.", [[_button("لغو", "cancel")]])

    def ask(self, store: dict, actor: int, chat: int, action: str, payload: dict, current: Any, text: str) -> None:
        token = policy.issue_confirmation(store["administration"], actor=actor, chat=chat, action=action,
                                          payload=payload, resource_digest=policy.digest(current), now=int(time.time()))
        self.save(store, actor)
        self.say(chat, text + "\n\nاین تأیید فقط برای همین حساب و تا ۱۵ دقیقه معتبر است.",
                 [[_button("✅ تأیید", "confirm", token), _button("لغو", "cancel")]])

    def publisher_state(self) -> dict:
        state = self.control.read_repo_json(self.control.PUBLISHER_STATE_PATH, self.control.PUBLISHER_STATE_BRANCH, {})
        if not isinstance(state, dict) or not isinstance(state.get("destinations", {}), dict):
            raise self.control.RetryableCommandError("invalid publisher status snapshot")
        return state

    def servers(self) -> list[dict]:
        _, item = self.control.github_api("/contents/data/servers.json?ref=main")
        try:
            if int(item.get("size", 0)) > 15_000_000:
                raise ValueError
            data = json.loads(base64.b64decode(item["content"]))
            if not isinstance(data, list):
                raise ValueError
            return [row for row in data if isinstance(row, dict)]
        except (KeyError, ValueError, TypeError):
            raise self.control.RetryableCommandError("could not read collector server snapshot") from None

    def check_target(self, item: dict) -> dict:
        me = self.control.telegram_api("getMe", {})
        chat = self.control.telegram_api("getChat", {"chat_id": item["chat_id"]})
        member = self.control.telegram_api("getChatMember", {"chat_id": item["chat_id"], "user_id": me["id"]})
        if not isinstance(chat, dict) or not isinstance(member, dict) or not member.get("status"):
            raise self.control.RetryableCommandError("invalid Telegram destination permissions")
        updated = dict(item)
        updated.update({"title": str(chat.get("title") or item["title"])[:200],
                        "username": str(chat.get("username", ""))[:100], "bot_status": str(member["status"])})
        if member["status"] != "administrator" or chat.get("type") == "channel" and member.get("can_post_messages") is not True:
            updated["enabled"] = False
            if member["status"] == "administrator":
                updated["bot_status"] = "cannot_post"
        if item.get("message_thread_id") and chat.get("is_forum") is not True:
            updated.update({"enabled": False, "bot_status": "topic_not_supported"})
        return updated

    def target_rows(self, store: dict, actor: int, item: dict) -> list:
        key = str(item["key"])
        rows = [[_button("📊 صف", "queue", key), _button("👁 پیش‌نمایش", "target_preview", key)],
                [_button("📝 نمایش قالب", "target_template_show", key)]]
        if policy.allowed(store, actor, "operate"):
            rows += [[_button("🔴 خاموش" if item["enabled"] else "🟢 روشن", "target_off" if item["enabled"] else "target_on", key)],
                     [_button("⏸ توقف یک ساعت", "target_pause", key + " 1h"), _button("▶️ ادامه", "target_resume", key)],
                     [_button("🔄 تلاش مجدد", "queue_retry", key), _button("♻️ بازسازی صف", "queue_rebuild", key)]]
        if policy.allowed(store, actor, "configure"):
            rows += [[_button("⏱ فاصله", "prompt", "target_interval " + key), _button("📝 قالب", "prompt", "target_template " + key)],
                     [_button("🔎 فیلتر", "prompt", "target_filter " + key), _button("🕘 ساعات ارسال", "prompt", "target_schedule " + key)],
                     [_button("🔢 سهمیه", "prompt", "target_quota " + key), _button("💬 تاپیک", "prompt", "target_topic " + key)],
                     [_button("🔐 بررسی دسترسی", "target_check", key), _button("↩️ تنظیمات قبلی", "target_rollback", key)],
                     [_button("🗑 حذف مقصد", "target_remove", key)]]
        return rows + [[_button("⬅️ مقصدها", "targets"), _button("🏠 پنل", "home")]]

    def show_target(self, store: dict, actor: int, chat: int, item: dict) -> None:
        filters = item["filters"]
        schedule = item.get("schedule")
        window = "دائمی"
        if schedule:
            offset = schedule["utc_offset_minutes"]
            window = (f"{schedule['start']//60:02}:{schedule['start']%60:02}–{schedule['end']//60:02}:{schedule['end']%60:02} "
                      f"UTC{'+' if offset >= 0 else '-'}{abs(offset)//60:02}:{abs(offset)%60:02}")
        text = (f"📡 {item['title']}\nشناسه: {item['key']} | chat: {item['chat_id']}\n"
                f"انتشار: {'روشن' if item['enabled'] else 'خاموش'} | بات: {item['bot_status']}\n"
                f"فاصله: {format_interval(item['min_delay_seconds'], item['max_delay_seconds'])}\n"
                f"تاپیک: {item.get('message_thread_id') or 'عمومی'}\nساعات ارسال: {window}\n"
                f"سهمیه روزانه: {item['daily_limit'] or 'نامحدود'}\nتوقف تا: {_stamp(item['pause_until'])}\n"
                f"پروتکل: {','.join(filters['protocols']) or 'همه'}\nکشور: {','.join(filters['countries']) or 'همه'}\n"
                f"شبکه: {','.join(filters['transports']) or 'همه'}\nTLS اجباری: {'بله' if filters['tls_only'] else 'خیر'}\n"
                f"سقف تأخیر: {filters['max_latency_ms'] or 'نامحدود'} ms")
        self.say(chat, text, self.target_rows(store, actor, item))

    def paginated(self, chat: int, title: str, items: list, page_arg: str, buttons: Callable, action: str) -> None:
        pages = max(1, (len(items) + 5) // 6)
        page = min(policy.integer(page_arg or "0", 0, 100), pages - 1)
        rows = [[buttons(item, i + 1)] for i, item in enumerate(items[page * 6:(page + 1) * 6], page * 6)]
        nav = []
        if page:
            nav.append(_button("⬅️ قبلی", action, str(page - 1)))
        if page + 1 < pages:
            nav.append(_button("بعدی ➡️", action, str(page + 1)))
        if nav:
            rows.append(nav)
        rows.append([_button("🏠 پنل", "home")])
        self.say(chat, f"{title}\nتعداد: {len(items)} | صفحه {page + 1}/{pages}", rows)

    def report(self, store: dict, chat: int) -> None:
        state = self.publisher_state()
        now = time.time()
        lines = [f"📊 انتشار کلی: {'روشن' if self.control.current_enabled() else 'خاموش'}",
                 f"توقف کلی تا: {_stamp(store['administration']['pause_until'])}",
                 f"اجرای فعال/منتظر: {self.control.publisher_run_count()}",
                 f"محدودیت Telegram تا: {_stamp(state.get('bot_next_send_after'))}", ""]
        for item in store["destinations"]:
            target = state.get("destinations", {}).get(str(item["chat_id"]), {})
            count = target.get("daily_sent", 0) if target.get("daily_date") == policy.local_day(item, now) else 0
            lines += [f"{'🟢' if item['enabled'] else '🔴'} {item['title']} ({item['key']})",
                      f"صف {len(target.get('queue', []))} | دور {target.get('cycle', 1)} | امروز {count}/{item['daily_limit'] or '∞'}",
                      f"ارسال ثبت‌شده {target.get('total_sent', '—')} | آخرین ارسال {_stamp(target.get('last_sent_at'))}"]
        held_count = sum(len(t.get("uncertain_deliveries", {})) for t in state.get("destinations", {}).values())
        lines += ["", f"⚠️ ارسال مبهم نیازمند بررسی: {held_count}؛ جزئیات با /queue <مقصد>"]
        self.say(chat, "\n".join(lines), [[_button("🔄 به‌روزرسانی", "report"), _button("🏠 پنل", "home")]])

    def health(self, store: dict, chat: int) -> None:
        status = self.control.read_repo_json("data/status.json", "main", {})
        age = None
        try:
            age = max(0, int(time.time() - datetime.fromisoformat(status["last_update"].replace("Z", "+00:00")).timestamp()))
        except (KeyError, ValueError, TypeError):
            pass
        managed = sources_ext._load_sources(self.control)
        self.control.telegram_api("getMe", {})
        collection = status.get("collection", {})
        validation = status.get("validation", {})
        lines = ["🩺 سلامت عملیاتی", "Telegram API: پاسخ معتبر", "تنظیمات رمزگذاری‌شده: قابل خواندن",
                 f"عمر خروجی جمع‌آوری: {str(age//60)+' دقیقه' if age is not None else 'نامشخص'}",
                 f"کانفیگ آنلاین در آخرین خروجی: {status.get('online_configs', '—')}",
                 f"خطای اعتبارسنجی آخرین اجرا: {validation.get('validator_errors', '—')}",
                 f"منابع ناموفق آخرین جمع‌آوری: {collection.get('failed', '—')}",
                 f"منابع مدیریتی فعال: {sum(item.get('enabled') is True for item in managed)}/{len(managed)}",
                 f"مقصدهای نیازمند دسترسی ادمین: {sum(item['bot_status'] != 'administrator' for item in store['destinations'])}"]
        if age is None or age > 1800:
            lines.append("⚠️ خروجی جمع‌آوری نیازمند بررسی است؛ /collector_refresh")
        self.say(chat, "\n".join(lines), [[_button("🔄 بررسی دوباره", "health"), _button("🏠 پنل", "home")]])

    def backup_payload(self, store: dict) -> dict:
        clean = copy.deepcopy(store)
        admin = clean["administration"]
        admin.update({"confirmations": {}, "sessions": {}, "versions": {}, "audit": []})
        admin["roles"] = policy.effective_roles(store)
        return {"version": 1, "created_at": int(time.time()), "destinations": normalize_store(clean),
                "sources": sources_ext._load_sources(self.control), "promo": promo._current_promo(self.control)}

    def backup_keys(self) -> list[str]:
        from telegram_destinations import _secret_candidates
        return _secret_candidates()

    def consume_confirmation(self, store: dict, actor: int, chat: int, token: str) -> None:
        admin = store["administration"]
        request = policy.confirmation(admin, token, actor, chat, int(time.time()))
        action, payload = request["action"], request["payload"]
        if not policy.allowed(store, actor, permission_for(action, "change")):
            raise policy.PolicyError("دسترسی شما به این عملیات تغییر کرده است")
        current = None
        if action in {"target_remove", "target_rollback", "queue_resolve"}:
            index, item = self.target(store, payload["key"])
            current = item
        elif action == "source_remove":
            sources = sources_ext._load_sources(self.control)
            matches = [(i, row) for i, row in enumerate(sources) if row.get("id") == payload["id"]]
            if not matches:
                # A source write can succeed before an audit/store write fails.
                # Finish the durable approval on retry without deleting another
                # source whose display index has since moved.
                admin["confirmations"].pop(token, None)
                self.save(store, actor, action, payload["id"])
                sources_ext._ensure_collector(self.control)
                self.say(chat, "✅ این منبع حذف شده است؛ عملیات و سابقه تکمیل شد.")
                return
            index, item = matches[0]
            current = item
        elif action in TEAM_ACTIONS:
            current = admin["roles"]
        elif action == "targets_on":
            current = store["destinations"]
        elif action == "restore":
            current = self.restore_digest(store)
            backup = payload["backup"]
            # Retrying our own partially completed multi-branch writes is safe;
            # an intervening unrelated source/CTA edit invalidates approval.
            current_sources = policy.digest(sources_ext._load_sources(self.control))
            current_promo = policy.digest(promo._current_promo(self.control))
            if (current_sources not in {payload["sources_digest"], policy.digest(backup["sources"])}
                    or current_promo not in {payload["promo_digest"], policy.digest(backup["promo"])}):
                raise policy.PolicyError("منابع یا تنظیمات خرید تغییر کرده است؛ بازیابی را دوباره درخواست کنید")
        if policy.digest(current) != request["digest"]:
            raise policy.PolicyError("تنظیمات از زمان درخواست تغییر کرده است؛ دستور را دوباره بفرستید")
        if action == "target_remove":
            store["destinations"].pop(index)
            admin["versions"].pop(payload["key"], None)
        elif action == "target_rollback":
            self.remember(store, item)
            settings = payload["settings"]
            restored = dict(item)
            restored.update({k: copy.deepcopy(v) for k, v in settings.items() if k in CONFIG_FIELDS})
            restored["enabled"] = False
            restored["updated_at"] = now_iso()
            store["destinations"][index] = restored
        elif action == "queue_resolve":
            target_state = self.publisher_state().get("destinations", {}).get(str(item["chat_id"]), {})
            held = target_state.get("uncertain_deliveries", {}).get(payload["server_id"])
            if held is None or policy.digest(held) != payload["attempt_digest"]:
                raise policy.PolicyError("وضعیت ارسال مبهم تغییر کرده است؛ صف را دوباره بررسی کنید")
            resolutions = dict(item.get("delivery_resolutions", {}))
            if len(resolutions) >= 100 and payload["server_id"] not in resolutions:
                resolutions.pop(next(iter(resolutions)))
            resolutions[payload["server_id"]] = {"decision": payload["decision"], "nonce": secrets.token_hex(8)}
            item["delivery_resolutions"] = resolutions
            item["retry_generation"] += 1
            store["destinations"][index] = item
        elif action == "source_remove":
            sources.pop(index)
            sources_ext._save_sources_verified(self.control, sources, "chore: remove managed Telegram source [automated]")
        elif action in TEAM_ACTIONS:
            uid = str(payload["user_id"])
            if int(uid) in policy.owner_ids_from_env():
                raise policy.PolicyError("مالک معرفی‌شده در Secret را از پنل نمی‌توان تغییر داد")
            roles = dict(admin["roles"])
            if action == "admin_remove":
                roles.pop(uid, None)
            else:
                roles[uid] = payload["role"]
            if "owner" not in policy.effective_roles({"administration": {"roles": roles}}, policy.owner_ids_from_env()).values():
                raise policy.PolicyError("آخرین مالک قابل حذف یا تنزل نیست")
            admin["roles"] = roles
        elif action == "targets_on":
            for i, item in enumerate(store["destinations"]):
                checked = self.check_target(item)
                if checked["bot_status"] != "administrator":
                    raise policy.PolicyError("یک مقصد دسترسی انتشار ندارد؛ ابتدا /target_check را اجرا کنید")
                checked["enabled"] = True
                store["destinations"][i] = checked
        elif action == "restore":
            self.apply_restore(store, actor, chat, token, payload["backup"])
            return
        else:
            raise policy.PolicyError("عملیات تأیید پشتیبانی نمی‌شود")
        admin["confirmations"].pop(token, None)
        resource = str(payload.get("key") or payload.get("id") or payload.get("user_id") or "all")
        self.save(store, actor, action, resource)
        if action == "source_remove":
            sources_ext._ensure_collector(self.control)
        if action in {"targets_on", "queue_resolve"} and self.control.current_enabled():
            self.control.ensure_publisher_run()
        self.say(chat, "✅ عملیات تأیید و ثبت شد.", [[_button("🏠 پنل", "home")]])

    def restore_digest(self, store: dict) -> dict:
        return {"roles": store["administration"]["roles"], "targets": store["destinations"],
                "blocked_ids": store["administration"]["blocked_ids"]}

    def apply_restore(self, store: dict, actor: int, chat: int, token: str, value: dict) -> None:
        if self.control.current_enabled() or self.control.publisher_run_count():
            raise policy.PolicyError("ابتدا انتشار کلی را خاموش کنید و تا پایان اجرای فعال صبر کنید")
        backup = validate_backup(value)
        new_store = backup["destinations"]
        roles = new_store["administration"]["roles"]
        # Prevent a restored backup from locking its current approving owner out.
        roles[str(actor)] = "owner"
        new_store["administration"]["audit"] = store["administration"]["audit"]
        # All validation precedes the first write. Separate GitHub branches are
        # not transactional; master OFF and retained approval make retries safe.
        sources_ext._save_sources_verified(self.control, backup["sources"], "chore: restore encrypted source settings [automated]")
        promo._persist_verified(self.control, **backup["promo"], message="chore: restore Telegram purchase CTA settings [automated]")
        self.save(new_store, actor, "restore", "settings")
        sources_ext._ensure_collector(self.control)
        self.say(chat, "✅ تنظیمات بازیابی شد. مقصدها خاموش‌اند؛ تاریخچهٔ ارسال حفظ شده است. دسترسی مقصدها را بررسی و سپس فعال کنید.")

    def execute(self, action: str, argument: str, *, store: dict, actor: int, chat: int, document: dict | None = None) -> None:
        admin = store["administration"]
        now = int(time.time())
        if action == "home":
            role = policy.effective_roles(store).get(str(actor))
            self.say(chat, f"🎛 پنل مدیریت Broute\nنقش شما: {ROLE_LABELS.get(role, 'بدون دسترسی')}\nشناسه شما: {actor}\n"
                     f"انتشار کلی: {'روشن' if self.control.current_enabled() else 'خاموش'}\n"
                     f"مقصد فعال: {sum(item['enabled'] for item in store['destinations'])}/{len(store['destinations'])}\n"
                     f"توقف کلی تا: {_stamp(admin['pause_until'])}\n"
                     "کنترل انتشار، مقصدها، منابع و گزارش‌های عملیاتی.", self.home_rows(store, actor))
        elif action == "help":
            self.say(chat, HELP_TEXT, self.home_rows(store, actor))
        elif action == "prompt":
            self.prompt(store, actor, chat, argument)
        elif action == "cancel":
            admin["sessions"].pop(str(actor), None)
            admin["confirmations"] = {k: v for k, v in admin["confirmations"].items() if v.get("actor") != actor}
            self.save(store, actor)
            self.say(chat, "درخواست‌های ورودی و تأیید شما لغو شد.", [[_button("🏠 پنل", "home")]])
        elif action == "confirm":
            self.consume_confirmation(store, actor, chat, argument)
        elif action == "targets":
            self.paginated(chat, "📡 مقصدها (مقصد تازه خاموش ثبت می‌شود)", store["destinations"], argument,
                           lambda item, i: _button(f"{i}. {'🟢' if item['enabled'] else '🔴'} {item['title'][:35]}", "target_show", item["key"]), "targets")
            if policy.allowed(store, actor, "configure"):
                self.say(chat, "برای ثبت گروه/کانالی که بات از قبل در آن ادمین است، chat ID یا @username را بفرستید.",
                         [[_button("➕ ثبت مقصد موجود", "prompt", "target_register")]])
        elif action == "sources":
            self.paginated(chat, "📚 منابع مدیریتی", sources_ext._load_sources(self.control), argument,
                           lambda item, i: _button(f"{i}. {'🟢' if item['enabled'] else '🔴'} {str(item.get('name') or source_host(item['url']))[:35]}", "source_show", item["id"]), "sources")
        elif action == "admins":
            roles = policy.effective_roles(store)
            self.paginated(chat, "👥 تیم مدیریت؛ افزودن با /admin_add یا دکمهٔ زیر", list(roles.items()), argument,
                           lambda item, i: _button(f"{item[0]} — {ROLE_LABELS[item[1]]}", "admin_show", item[0]), "admins")
            if policy.allowed(store, actor, "team"):
                self.say(chat, "تغییر نقش و حذف مدیر نیازمند تأیید است.", [[_button("➕ افزودن/تغییر نقش", "prompt", "admin_add")]])
        elif action == "admin_show":
            uid = str(policy.integer(argument, 1))
            role = policy.effective_roles(store).get(uid)
            if not role:
                raise policy.PolicyError("این حساب در تیم مدیریت نیست")
            rows = []
            if policy.allowed(store, actor, "team") and int(uid) not in policy.owner_ids_from_env():
                rows = [[_button(ROLE_LABELS[new_role], "admin_add", uid + " " + new_role) for new_role in ("admin", "operator")],
                        [_button("مالک", "admin_add", uid + " owner"), _button("مشاهده‌گر", "admin_add", uid + " viewer")],
                        [_button("🗑 حذف دسترسی", "admin_remove", uid)]]
            self.say(chat, f"👤 شناسه: {uid}\nنقش: {ROLE_LABELS[role]}\n"
                     f"مالک بازیابی از Secret: {'بله' if int(uid) in policy.owner_ids_from_env() else 'خیر'}",
                     rows + [[_button("⬅️ تیم", "admins"), _button("🏠 پنل", "home")]])
        elif action in TEAM_ACTIONS:
            parts = argument.split()
            if len(parts) != (2 if action == "admin_add" else 1):
                raise policy.PolicyError("مثال: /admin_add 123456 operator یا /admin_remove 123456")
            uid = policy.integer(parts[0], 1)
            role = parts[1].lower() if action == "admin_add" else ""
            if action == "admin_add" and role not in policy.ROLES:
                raise policy.PolicyError("نقش مجاز: owner / admin / operator / viewer")
            if action == "admin_add" and str(uid) not in admin["roles"] and len(admin["roles"]) >= policy.MAX_ADMINS:
                raise policy.PolicyError("سقف تعداد مدیران پر شده است")
            if action == "admin_remove" and str(uid) not in admin["roles"]:
                raise policy.PolicyError("این مدیر در فهرست نیست")
            self.ask(store, actor, chat, action, {"user_id": uid, "role": role}, admin["roles"],
                     f"تغییر دسترسی حساب {uid} به {ROLE_LABELS.get(role, 'حذف دسترسی')}؟")
        elif action == "report":
            self.report(store, chat)
        elif action == "health":
            self.health(store, chat)
        elif action == "audit":
            entries = list(reversed(admin["audit"]))
            page = policy.integer(argument or "0", 0, 20)
            lines = ["🧾 سابقه تغییرات (آخرین ۲۰۰ رویداد)"] + [f"{_stamp(e['at'])} | {e['actor']} | {e['action']} | {e['resource']}" for e in entries[page*10:page*10+10]]
            rows = []
            if page:
                rows.append(_button("قبلی", "audit", str(page-1)))
            if (page + 1) * 10 < len(entries):
                rows.append(_button("بعدی", "audit", str(page+1)))
            self.say(chat, "\n".join(lines), ([rows] if rows else []) + [[_button("🏠 پنل", "home")]])
        elif action in {"publisher_on", "publisher_off"}:
            self.control.set_enabled(action == "publisher_on")
            self.save(store, actor, action, "global")
            self.say(chat, "✅ انتشار کلی " + ("روشن شد." if action == "publisher_on" else "خاموش شد؛ صف و تاریخچه حفظ شد."), self.home_rows(store, actor))
        elif action in {"publisher_pause", "publisher_resume"}:
            admin["pause_until"] = now + policy.duration(argument) if action == "publisher_pause" else 0
            self.save(store, actor, action, "global")
            if action == "publisher_resume" and self.control.current_enabled():
                self.control.ensure_publisher_run()
            self.say(chat, "✅ توقف موقت ثبت شد." if action == "publisher_pause" else "✅ توقف موقت برداشته شد.")
        elif action == "collector_refresh":
            sources_ext._ensure_collector(self.control)
            self.save(store, actor, action, "collector")
            self.say(chat, "✅ اجرای جمع‌آوری درخواست شد؛ اجرای موجود هم‌زمان تکرار نمی‌شود.")
        elif action == "targets_off":
            for item in store["destinations"]:
                if item["enabled"]:
                    self.remember(store, item)
                item["enabled"] = False
            self.save(store, actor, action, "all")
            self.say(chat, "✅ همه مقصدها خاموش شدند؛ تاریخچه حفظ شد.")
        elif action == "targets_on":
            if not store["destinations"]:
                raise policy.PolicyError("مقصدی ثبت نشده است")
            self.ask(store, actor, chat, action, {}, store["destinations"], "همه مقصدهای ثبت‌شده فعال شوند؟ دسترسی انتشار هر مقصد دوباره بررسی می‌شود.")
        elif action == "blocked":
            page = policy.integer(argument or "0", 0, 50)
            blocked = admin["blocked_ids"]
            lines = [f"🚫 مسدودی کانفیگ‌ها: {len(blocked)}", *blocked[page*10:page*10+10], "حذف مسدودی: /config_unblock <id>"]
            nav = []
            if page:
                nav.append(_button("قبلی", "blocked", str(page-1)))
            if (page+1)*10 < len(blocked):
                nav.append(_button("بعدی", "blocked", str(page+1)))
            self.say(chat, "\n".join(lines), ([nav] if nav else []) + [[_button("🏠 پنل", "home")]])
        elif action in {"config_block", "config_unblock"}:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", argument):
                raise policy.PolicyError("شناسه کانفیگ معتبر نیست")
            blocked = list(admin["blocked_ids"])
            if action == "config_block" and argument not in blocked:
                if len(blocked) >= policy.MAX_BLOCKED:
                    raise policy.PolicyError("سقف فهرست مسدودی پر شده است")
                blocked.append(argument)
            if action == "config_unblock":
                blocked = [v for v in blocked if v != argument]
            admin["blocked_ids"] = blocked
            self.save(store, actor, action, argument)
            self.say(chat, "✅ فهرست مسدودی به‌روز شد؛ صف ناشر در بازخوانی بعدی همگام می‌شود.")
        elif action == "backup":
            data = policy.encrypt_backup(self.backup_payload(store), self.backup_keys()[0])
            send_backup(self.control, chat, data)
            self.save(store, actor, action, "settings")
            self.say(chat, "💾 پشتیبان رمزگذاری‌شده ارسال شد. برای بازیابی به همان کلید تنظیمات نیاز دارید؛ فایل شامل توکن بات یا تاریخچهٔ ارسال نیست.")
        elif action == "restore":
            if self.control.current_enabled() or self.control.publisher_run_count():
                raise policy.PolicyError("برای بازیابی، انتشار کلی باید خاموش و اجرای ناشر تمام شده باشد")
            if document is None:
                admin["sessions"][str(actor)] = {"chat": chat, "verb": "restore", "selector": "", "expires": now + policy.SESSION_TTL_SECONDS}
                self.save(store, actor)
                self.say(chat, "فایل broute-settings.broute را به‌صورت Document بفرستید. مهلت ۲۰ دقیقه؛ سپس تأیید نهایی می‌گیرید.")
            else:
                value = validate_backup(policy.decrypt_backup(download_backup(self.control, document), self.backup_keys()))
                admin["sessions"].pop(str(actor), None)
                restore_payload = {"backup": value,
                                   "sources_digest": policy.digest(sources_ext._load_sources(self.control)),
                                   "promo_digest": policy.digest(promo._current_promo(self.control))}
                self.ask(store, actor, chat, action, restore_payload, self.restore_digest(store),
                         f"بازیابی {len(value['destinations']['destinations'])} مقصد و {len(value['sources'])} منبع؟ همه مقصدها خاموش می‌شوند و تاریخچهٔ ارسال حفظ می‌شود.")
        elif action in {"buy_button", "buy_link", "buy_text"}:
            if not argument or action == "buy_button":
                item = promo._current_promo(self.control)
            else:
                _, item = (promo._set_url_verified(self.control, argument) if action == "buy_link" else promo._set_text_verified(self.control, argument))
                self.save(store, actor, action, "promo")
            rows = [[_button("🏠 پنل", "home")]]
            if policy.allowed(store, actor, "configure"):
                rows.insert(0, [_button("لینک خرید", "prompt", "buy_link"), _button("نام دکمه", "prompt", "buy_text")])
            self.say(chat, f"🛒 نام: {item['text']}\nلینک: {item['url']}", rows)
        elif action == "target_register":
            selector = argument.strip()
            if not re.fullmatch(r"-\d{1,16}|@[A-Za-z0-9_]{5,32}", selector):
                raise policy.PolicyError("مثال: /target_register -1001234567890 یا @channel_name")
            details = self.control.telegram_api("getChat", {"chat_id": selector})
            if not isinstance(details, dict) or details.get("type") not in {"group", "supergroup", "channel"}:
                raise policy.PolicyError("مقصد باید گروه یا کانال باشد")
            target_id = policy.integer(details.get("id"), -(2**53 - 1), -1)
            if actor not in targets._chat_admin_ids(self.control, target_id):
                raise policy.PolicyError("برای ثبت مقصد، باید ادمین همان گروه یا کانال باشید")
            index, existing = find_destination(store["destinations"], str(target_id))
            if existing is None and len(store["destinations"]) >= MAX_DESTINATIONS:
                raise policy.PolicyError("سقف تعداد مقصدها پر شده است")
            item = dict(existing) if existing else {"chat_id": target_id, "title": details.get("title"), "enabled": False,
                                                    "chat_type": details["type"], "discovered_at": now_iso()}
            checked = self.check_target(item)
            if checked["bot_status"] != "administrator":
                raise policy.PolicyError("بات باید ادمین و دارای حق ارسال در مقصد باشد")
            from telegram_destinations import normalize_destination
            checked = normalize_destination(checked)
            checked["updated_at"] = now_iso()
            if existing is None:
                store["destinations"].append(checked)
            else:
                store["destinations"][int(index)] = checked
            self.save(store, actor, action, checked["key"])
            self.show_target(store, actor, chat, checked)
        elif action.startswith("target_") or action in {"queue", "queue_retry", "queue_rebuild", "queue_resolve"}:
            self.target_action(action, argument, store, actor, chat)
        elif action.startswith("source_"):
            self.source_action(action, argument, store, actor, chat)
        else:
            raise policy.PolicyError("دستور معتبر نیست؛ /help را ببینید")

    def target_action(self, action: str, argument: str, store: dict, actor: int, chat: int) -> None:
        # Splitting once preserves the remainder of a multi-line template.
        parts = argument.strip().split(maxsplit=1)
        selector, value = (parts[0], parts[1] if len(parts)>1 else "") if parts else ("", "")
        index, original = self.target(store, selector)
        item = copy.deepcopy(original)
        key = item["key"]
        if action == "target_show":
            self.show_target(store, actor, chat, item)
            return
        if action == "target_template_show" or action == "target_template" and not value:
            self.say(chat, "📝 قالب فعلی:\n" + item["template"], self.target_rows(store, actor, item))
            return
        if action == "queue":
            state = self.publisher_state()
            target = state.get("destinations", {}).get(str(item["chat_id"]), {})
            queued = target.get("queue", [])
            now = time.time()
            due = max(policy.next_allowed_at(item, target, now, store["administration"]["pause_until"]), state.get("bot_next_send_after", 0))
            text = (f"⏳ صف {item['title']}\nتعداد: {len(queued)} | دور: {target.get('cycle', 1)}\n"
                    f"زودترین زمان مجاز: {_stamp(due)}\nتعلیق خطا تا: {_stamp(target.get('suspended_until'))}\n"
                    f"شکست متوالی: {target.get('consecutive_failures', 0)}\n"
                    f"وضعیت خطا: {'نیازمند بررسی' if target.get('last_error') else 'بدون خطای ثبت‌شده'}\n"
                    "شناسه‌های ابتدای صف:\n" + "\n".join(str(v)[:128] for v in queued[:10]))
            uncertain = target.get("uncertain_deliveries", {})
            if uncertain:
                text += (f"\n\n⚠️ ارسال مبهم، متوقف برای بررسی: {len(uncertain)}\n" + "\n".join(list(uncertain)[:10])
                         + f"\nتعیین تکلیف: /queue_resolve {key} <id> sent|retry\nretry ممکن است پیام تکراری بسازد؛ ابتدا مقصد را بررسی کنید.")
            self.say(chat, text, self.target_rows(store, actor, item))
            return
        if action == "queue_resolve":
            parts = value.split()
            if len(parts) != 2 or parts[1] not in {"sent", "retry"}:
                raise policy.PolicyError("مثال: /queue_resolve 1 <id> sent یا retry")
            state = self.publisher_state().get("destinations", {}).get(str(item["chat_id"]), {})
            held = state.get("uncertain_deliveries", {}).get(parts[0])
            if held is None:
                raise policy.PolicyError("این شناسه در ارسال‌های مبهم نیست؛ /queue را بررسی کنید")
            self.ask(store, actor, chat, action, {"key": key, "server_id": parts[0], "decision": parts[1], "attempt_digest": policy.digest(held)},
                     original, "تعیین تکلیف ارسال مبهم: " + ("به‌عنوان ارسال‌شده ثبت شود؟" if parts[1] == "sent" else "دوباره تلاش شود؟ اگر ارسال قبلی رسیده باشد، پیام تکراری خواهد شد."))
            return
        if action == "target_preview":
            import telegram_multi_publisher as publisher
            blocked = set(store["administration"]["blocked_ids"])
            candidates = [s for s in self.servers() if publisher.base.eligible(s) and policy.server_matches(s, item, blocked)]
            for server in candidates:
                try:
                    message = publisher.render_target_message(item, server)
                    break
                except ValueError:
                    continue
            else:
                raise policy.PolicyError("نمونه قابل انتشار مطابق فیلترها وجود ندارد")
            self.say(chat, "👁 پیش‌نمایش در همین گفت‌وگوی خصوصی؛ از آخرین خروجی جمع‌آوری و بدون تست زنده یا ثبت ارسال.")
            payload = {"chat_id": chat, "text": str(message), "parse_mode": "HTML", "disable_notification": True,
                       "link_preview_options": {"is_disabled": True}}
            self.control.telegram_api("sendMessage", payload)
            return
        if action == "target_remove":
            self.ask(store, actor, chat, action, {"key": key}, original, f"مقصد «{item['title']}» حذف شود؟ تاریخچهٔ ارسال پاک نمی‌شود.")
            return
        if action == "target_rollback":
            history = store["administration"]["versions"].get(key, [])
            if not history:
                raise policy.PolicyError("تنظیمات قبلی برای این مقصد ثبت نشده است")
            self.ask(store, actor, chat, action, {"key": key, "settings": history[-1]["settings"]}, original,
                     "تنظیمات قبلی بازیابی شود؟ مقصد پس از بازیابی خاموش خواهد بود.")
            return
        if action == "target_on":
            item = self.check_target(item)
            if item["bot_status"] != "administrator":
                self.update_target(store, index, item, actor, "target_check")
                raise policy.PolicyError("بات دسترسی انتشار ندارد؛ مقصد خاموش نگه داشته شد")
            item["enabled"] = True
        elif action == "target_off":
            item["enabled"] = False
        elif action == "target_check":
            item = self.check_target(item)
        elif action == "target_interval":
            item["min_delay_seconds"], item["max_delay_seconds"] = parse_interval_spec(value)
        elif action == "target_template":
            item["template"] = normalize_template(value)
        elif action == "target_template_reset":
            item["template"] = DEFAULT_TEMPLATE
        elif action == "target_topic":
            topic = policy.integer(value, 0, 2**31 - 1)
            if topic:
                details = self.control.telegram_api("getChat", {"chat_id": item["chat_id"]})
                if not isinstance(details, dict) or details.get("is_forum") is not True:
                    raise policy.PolicyError("تاپیک فقط در گروه Forum قابل تنظیم است")
            item["message_thread_id"] = topic or None
        elif action == "target_filter":
            item["filters"] = policy.parse_filters(value, item["filters"])
        elif action == "target_schedule":
            item["schedule"] = policy.parse_schedule(value)
        elif action == "target_quota":
            item["daily_limit"] = policy.integer(value, 0, 1000)
        elif action == "target_pause":
            item["pause_until"] = int(time.time()) + policy.duration(value)
        elif action == "target_resume":
            item["pause_until"] = 0
        elif action == "queue_retry":
            item["retry_generation"] += 1
        elif action == "queue_rebuild":
            item["queue_revision"] += 1
        else:
            raise policy.PolicyError("دستور مقصد پشتیبانی نمی‌شود")
        self.update_target(store, index, item, actor, action)
        self.show_target(store, actor, chat, normalize_store(store)["destinations"][index])

    def source_action(self, action: str, argument: str, store: dict, actor: int, chat: int) -> None:
        items = sources_ext._load_sources(self.control)
        if action == "source_add":
            if not argument:
                self.prompt(store, actor, chat, "source_add")
                return
            url = normalize_subscription_url(argument)
            if any(item["url"] == url for item in items):
                raise policy.PolicyError("این منبع از قبل ثبت شده است")
            if len(items) >= MAX_MANAGED_SOURCES:
                raise policy.PolicyError("سقف تعداد منابع مدیریتی پر شده است")
            self.say(chat, "🔎 بررسی منبع و شمارش کانفیگ‌های قابل شناسایی…")
            canonical, count = validate_subscription(url)
            item = new_source_entry(canonical, count)
            items.append(item)
        else:
            parts = argument.split(maxsplit=1)
            selector, value = (parts[0], parts[1] if len(parts)>1 else "") if parts else ("", "")
            index, item = _resolve_source(items, selector)
            item = dict(item)
            if action == "source_show":
                key = item["id"]
                rows = [[_button("🏠 پنل", "home"), _button("⬅️ منابع", "sources")]]
                if policy.allowed(store, actor, "configure"):
                    rows = [[_button("🔴 خاموش" if item["enabled"] else "🟢 روشن", "source_off" if item["enabled"] else "source_on", key)],
                            [_button("✏️ نام", "prompt", "source_name " + key), _button("🔎 تست منبع", "source_check", key)],
                            [_button("🗑 حذف", "source_remove", key)]] + rows
                self.say(chat, f"📚 {item.get('name') or source_host(item['url'])}\nشناسه: {key}\n"
                         f"وضعیت: {'روشن' if item['enabled'] else 'خاموش'}\nآخرین تعداد معتبر: {item.get('last_validated_configs', '—')}\n"
                         f"آخرین بررسی: {_stamp(item.get('checked_at'))}\n"
                         f"نتیجه: {item.get('health', 'هنوز بررسی نشده')}", rows)
                return
            if action == "source_remove":
                self.ask(store, actor, chat, action, {"id": item["id"]}, item, "منبع مدیریتی حذف شود؟ URL اشتراک در پیام تأیید نمایش داده نمی‌شود.")
                return
            if action in {"source_on", "source_off"}:
                item["enabled"] = action == "source_on"
            elif action == "source_name":
                if not value.strip() or len(value) > 80 or any(ord(char) < 32 for char in value):
                    raise policy.PolicyError("نام باید یک خط و بین ۱ تا ۸۰ کاراکتر باشد")
                item["name"] = value.strip()
            elif action == "source_check":
                try:
                    _, count = validate_subscription(item["url"])
                    item.update({"last_validated_configs": count, "health": "ok"})
                except SourceValidationError:
                    item["health"] = "failed"
                item["checked_at"] = int(time.time())
            else:
                raise policy.PolicyError("دستور منبع معتبر نیست")
            items[index] = item
        sources_ext._save_sources_verified(self.control, items, "chore: update encrypted managed source [automated]")
        self.save(store, actor, action, item["id"])
        if action in {"source_add", "source_on", "source_off"}:
            sources_ext._ensure_collector(self.control)
        self.say(chat, "✅ وضعیت منبع ثبت و بازخوانی شد؛ " + ("بررسی منبع ناموفق بود." if item.get("health") == "failed" else "تنظیمات به‌روز است."),
                 [[_button("نمایش منبع", "source_show", item["id"]), _button("🏠 پنل", "home")]])


HELP_TEXT = """❔ راهنمای مدیریت Broute
همه تنظیمات فقط در پیام خصوصی بات قابل استفاده‌اند.

سطح دسترسی: مالک = همه امکانات؛ مدیر = تنظیمات و عملیات؛ اپراتور = عملیات انتشار؛ مشاهده‌گر = گزارش و مشاهده.
/whoami شناسه و نقش شما
/admins تیم مدیریت
/admin_add 123456 operator
/admin_remove 123456

انتشار و صف:
/publisher_on و /publisher_off
/publisher_pause 1h و /publisher_resume
/targets_off و /targets_on
/queue 1 و /queue_retry 1 و /queue_rebuild 1
/queue_resolve 1 <id> sent|retry برای تعیین تکلیف ارسال مبهم (با تأیید)
/collector_refresh
/config_block <id> و /config_unblock <id>
/blocked فهرست کانفیگ‌های مسدود

مقصدها: /targets سپس انتخاب مقصد
/target_register -1001234567890 یا @channel_name برای مقصدی که بات از قبل ادمین است
/target_on 1 و /target_off 1
/target_interval 1 30-90
/target_template 1 سپس خط جدید و قالب دارای {config}
/target_template_reset 1
/target_topic 1 123 (صفر = بدون تاپیک)
/target_filter 1 protocol=vless,trojan country=DE,NL tls=on latency=250
پاک‌کردن فیلتر: protocol=* country=* transport=* tls=off latency=0
/target_schedule 1 09:00-23:00 UTC+03:30
/target_schedule 1 off
/target_quota 1 100 (صفر = نامحدود)
/target_pause 1 2h و /target_resume 1
/target_check 1 و /target_preview 1
/target_rollback 1 و /target_remove 1

منابع: /sources سپس انتخاب منبع
/source_add <https URL> یا ارسال مستقیم URL
/source_on 1 و /source_off 1
/source_name 1 نام منبع
/source_check 1 و /source_remove 1

خرید: /buy_button، /buy_link <URL>، /buy_text <متن>
گزارش: /report، /health، /audit
پشتیبان تنظیمات رمزگذاری‌شده: /backup و /restore (فقط مالک)
حذف، تغییر تیم و بازیابی نیازمند تأیید زمان‌دار هستند.
/cancel لغو ورودی و تأییدهای شما

زمان‌بندی با اختلاف ثابت UTC کار می‌کند. پیش‌نمایش کانفیگ تست زنده نیست. فاصله پاسخ بات به زمان‌بندی GitHub Actions وابسته است."""


def install(control: Any) -> Callable[[], None]:
    original_parse, original_process = control.update_to_action, control.process_action
    original_register = control.register_commands
    original_auth, original_ids = control.is_authorized_admin, control.current_admin_ids
    original_menu = control.menu_keyboard
    panel = AdminPanel(control)
    requests: dict[tuple, dict] = {}

    def parse(update):
        if not isinstance(update, dict):
            return (None,) * 5
        if isinstance(update.get("my_chat_member"), dict):
            return original_parse(update)
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        sender = callback.get("from") if isinstance(callback, dict) else (message or {}).get("from")
        if not isinstance(message, dict) or not isinstance(sender, dict) or not isinstance(message.get("chat"), dict):
            return (None,) * 5
        try:
            actor, chat = int(sender["id"]), int(message["chat"]["id"])
        except (KeyError, TypeError, ValueError):
            return (None,) * 5
        cb_id = str(callback.get("id", "")) if isinstance(callback, dict) else None
        action, argument = "message", str(message.get("text", "")).strip()
        if callback:
            data = str(callback.get("data", ""))
            if data.startswith("adm:"):
                parts = data.split(":", 2)
                action = parts[1]
                argument = parts[2] if len(parts) > 2 else ""
            elif data in OLD_CALLBACKS:
                action, argument = OLD_CALLBACKS[data], ""
            elif data.startswith("target:"):
                parts = data.split(":")
                mapping = {"show": "target_show", "on": "target_on", "off": "target_off", "template": "target_template_show",
                           "reset": "target_template_reset", "interval": "target_interval"}
                if len(parts) not in {3, 4} or parts[1] not in mapping:
                    return (None,) * 5
                action, argument = mapping[parts[1]], " ".join(parts[2:])
            else:
                return (None,) * 5
        elif argument.startswith("/"):
            parts = argument.split(maxsplit=1)
            verb = parts[0].split("@", 1)[0][1:].lower()
            action = ALIASES.get(verb, verb)
            argument = parts[1] if len(parts) > 1 else ""
        elif not argument and not isinstance(message.get("document"), dict):
            return (None,) * 5
        key = (actor, chat, control.message_thread_id(message), cb_id)
        requests[key] = {"action": action, "argument": argument,
                         "document": message.get("document"), "chat_type": message["chat"].get("type")}
        return "admin_dispatch", *key

    def process(action, *, user_id, chat_id, thread_id, callback_id=None):
        if action == "target_membership":
            events = targets._PENDING_MEMBERSHIP.get((user_id, chat_id), [])
            status = events[0].get("bot_status") if events else ""
            store = panel.load()
            if status == "administrator":
                panel.bootstrap(store, user_id)
                if not policy.allowed(store, user_id, "configure"):
                    targets._PENDING_MEMBERSHIP.pop((user_id, chat_id), None)
                    return
                # Synchronize explicit recovery owners for the legacy discovery
                # handler, after verifying trust in the production policy.
                panel.save(store, user_id)
            original_process(action, user_id=user_id, chat_id=chat_id, thread_id=thread_id, callback_id=callback_id)
            after = panel.load()
            if store["destinations"] != after["destinations"]:
                panel.save(after, user_id, "target_membership", "destination")
            return
        if action != "admin_dispatch":
            return
        request = requests.pop((user_id, chat_id, thread_id, callback_id), None)
        if request is None:
            return
        if chat_id <= 0 or request["chat_type"] != "private":
            if callback_id:
                control.answer_callback(callback_id, "مدیریت فقط در پیام خصوصی")
            return
        store = panel.load()
        verb, argument = request["action"], request["argument"]
        if verb == "whoami":
            role = policy.effective_roles(store).get(str(user_id))
            panel.say(chat_id, f"شناسه شما: {user_id}\nنقش: {ROLE_LABELS.get(role, 'بدون دسترسی')}")
            return
        try:
            panel.bootstrap(store, user_id)
            role = policy.effective_roles(store).get(str(user_id))
            if role is None:
                panel.say(chat_id, f"⛔ دسترسی مدیریت ندارید. شناسه حساب: {user_id}\n"
                          "مالک می‌تواند با /admin_add به شما دسترسی بدهد. برای راه‌اندازی اولیه، مالک باید در TELEGRAM_OWNER_USER_IDS معرفی شود یا ادمین گروه کنترل اصلی باشد.")
                return
            session_used = False
            if verb == "message":
                session = store["administration"]["sessions"].get(str(user_id))
                if isinstance(session, dict) and session.get("chat") == chat_id and int(session.get("expires", 0)) > time.time():
                    verb = session["verb"]
                    argument = (session.get("selector", "") + " " + argument).strip()
                    session_used = True
                    if verb == "restore" and not isinstance(request["document"], dict):
                        raise policy.PolicyError("فایل پشتیبان را به‌صورت Document بفرستید یا /cancel را بزنید")
                elif argument.startswith(("https://", "http://")):
                    verb = "source_add"
                else:
                    raise policy.PolicyError("ورودی فعال نیست یا منقضی شده؛ /help را ببینید")
            required = (permission_for(argument.split(maxsplit=1)[0], "input") if verb == "prompt" and argument else
                        "view" if verb == "confirm" else permission_for(verb, argument))
            if not policy.allowed(store, user_id, required):
                raise policy.PolicyError("نقش شما اجازه این عملیات را ندارد")
            if callback_id:
                control.answer_callback(callback_id, "در حال پردازش")
            panel.execute(verb, argument, store=store, actor=user_id, chat=chat_id, document=request["document"])
            if session_used and verb != "restore":
                latest = panel.load()
                latest["administration"]["sessions"].pop(str(user_id), None)
                panel.save(latest, user_id)
        except RuntimeError as exc:
            if getattr(exc, "hostname", None) != "api.telegram.org" or getattr(exc, "status_code", None) not in {400, 403, 404}:
                raise
            # Invalid chat IDs / permanently missing permissions are command
            # failures, not transient outages that should block the entire poll.
            if callback_id:
                control.answer_callback(callback_id, "مقصد یا دسترسی معتبر نیست")
            panel.say(chat_id, "❌ تلگرام مقصد یا دسترسی درخواست‌شده را نپذیرفت؛ شناسه و ادمینی بات را بررسی کنید.")
        except ValueError as exc:
            if callback_id:
                control.answer_callback(callback_id, "درخواست پذیرفته نشد")
            # Validation exceptions never echo raw source URLs or uploaded data.
            panel.say(chat_id, "❌ " + str(exc))

    def register():
        commands = [{"command": name, "description": description} for name, description in (
            ("admin", "پنل مدیریت"), ("targets", "مقصدهای انتشار"), ("sources", "منابع مدیریتی"),
            ("report", "گزارش انتشار و سهمیه‌ها"), ("health", "سلامت جمع‌آوری و بات"), ("audit", "سابقه تغییرات"),
            ("publisher_on", "روشن کردن انتشار"), ("publisher_off", "خاموش کردن انتشار"),
            ("admins", "تیم مدیریت"), ("backup", "پشتیبان رمزگذاری‌شده"), ("restore", "بازیابی تنظیمات"),
            ("whoami", "شناسه و نقش شما"), ("cancel", "لغو درخواست"), ("help", "راهنمای کامل"))]
        control.telegram_api("setMyCommands", {"commands": commands, "scope": {"type": "all_private_chats"}})

    control.update_to_action, control.process_action, control.register_commands = parse, process, register
    control.is_authorized_admin = lambda uid: bool(policy.effective_roles(panel.load()).get(str(uid)))
    control.current_admin_ids = lambda: {int(uid) for uid in policy.effective_roles(panel.load())}
    control.menu_keyboard = lambda: {"inline_keyboard": [[_button("🎛 پنل مدیریت", "home")]]}

    def restore():
        control.update_to_action, control.process_action = original_parse, original_process
        control.register_commands, control.menu_keyboard = original_register, original_menu
        control.is_authorized_admin, control.current_admin_ids = original_auth, original_ids
        requests.clear()
    return restore
