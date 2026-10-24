"""Reuse the existing private panel/publisher through explicit storage bindings.

Installed only by server_runtime, in one OS process per role. The original
Actions entrypoints and their Git storage are unaffected.
"""
from __future__ import annotations

import base64
import copy
import os
import time
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import urlsplit

import telegram_bot_control as control
import telegram_bot_admin_extension as admin
import telegram_bot_destination_extension as destinations
import telegram_bot_promo_extension as promo
import telegram_bot_source_extension as sources
import telegram_multi_publisher as publisher
from telegram_bot_control_verified import verified_set_enabled
from telegram_destinations import DESTINATION_STATE_BRANCH, DESTINATION_STATE_PATH, decrypt_store, normalize_store
from telegram_managed_sources import SOURCE_STATE_BRANCH, SOURCE_STATE_PATH, decrypt_sources
from telegram_promo_config import PROMO_PATH, promo_from_data
from server_state import StateError

DOCUMENTS = {
    (control.CONTROL_BRANCH, control.CONTROL_PATH): "control",
    (control.BOT_STATE_BRANCH, control.BOT_STATE_PATH): "offset",
    (control.PUBLISHER_STATE_BRANCH, control.PUBLISHER_STATE_PATH): "publisher",
    (DESTINATION_STATE_BRANCH, DESTINATION_STATE_PATH): "destinations",
    (SOURCE_STATE_BRANCH, SOURCE_STATE_PATH): "sources",
    ("main", PROMO_PATH): "promo",
}


def validate_document(name, value):
    if not isinstance(value, dict):
        raise StateError("runtime document must be a JSON object")
    if name == "control" and not isinstance(value.get("enabled"), bool):
        raise StateError("invalid publisher control")
    if name == "offset":
        offset = value.get("last_update_id")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise StateError("invalid bot update offset")
    if name == "publisher":
        publisher.normalize_state(value)
    if name == "destinations":
        normalize_store(decrypt_store(value))
    if name == "sources":
        decrypt_sources(value)
    if name == "promo":
        promo_from_data(value)
    return value


class SnapshotUnavailable(RuntimeError):
    pass


class Bindings:
    def __init__(self, state, max_snapshot_age=900, stop=None):
        self.state, self.max_snapshot_age = state, max_snapshot_age
        self.stop = stop
        self.original_request = control._json_request
        self.stack = ExitStack()
        self._last_pulse = 0

    def read(self, path, ref, default):
        if ref == "main" and path == "data/status.json":
            return copy.deepcopy(self.state.get("collector_snapshot", {}).get("status", default))
        name = DOCUMENTS[(ref, path)]
        value = self.state.get(name)
        return validate_document(name, value) if value is not None else copy.deepcopy(default)

    def write(self, path, branch, payload, message):
        name = DOCUMENTS[(branch, path)]
        self.state.put(name, validate_document(name, payload))

    def snapshot(self, *, fresh=False):
        snapshot = self.state.get("collector_snapshot", {})
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("servers", []), list):
            raise StateError("invalid collector snapshot")
        if fresh and time.time() - float(snapshot.get("at", 0)) > self.max_snapshot_age:
            raise SnapshotUnavailable("collector snapshot is missing or stale; publishing paused")
        servers = copy.deepcopy(snapshot.get("servers", []))
        if fresh and os.environ.get("BROUTE_VALIDATION_MODE", "tcp") == "proxy":
            servers = [row for row in servers if row.get("validation_method") == "proxy_https" and row.get("probe_status") == "passed"
                       and 0 <= time.time() - row.get("probe_at", 0) <= 900]
        return servers

    def active_publisher(self):
        pulse = self.state.get("heartbeat:publisher", {})
        return int(pulse.get("status") == "working" and time.time() - pulse.get("at", 0) < 90)

    def request_collector(self, *_):
        import uuid
        self.state.put("collector_request", {"id": uuid.uuid4().hex, "at": time.time()})

    def queue_request(self, url, **kwargs):
        parsed = urlsplit(url)
        method = parsed.path.rsplit("/", 1)[-1]
        if parsed.hostname == "api.telegram.org" and method in {"sendMessage", "sendDocument", "answerCallbackQuery"}:
            request = {"method": method, "headers": kwargs.get("headers"), "payload": kwargs.get("payload")}
            raw = kwargs.get("raw_body")
            request["body"] = base64.b64encode(raw).decode() if raw is not None else None
            self.state.enqueue_reply(request)
            return 200, {"ok": True, "result": {}}
        return self.original_request(url, **kwargs)

    def flush_replies(self, limit=20):
        for _ in range(limit):
            if self.stop is not None and self.stop.is_set():
                return
            if self.cooldown_remaining() > 0:
                return
            reply = self.state.next_reply()
            if reply is None:
                return
            reply_id, request = reply
            self.state.guard()
            if self.stop is not None and self.stop.is_set():
                self.state.finish_reply(reply_id, "pending")
                return
            try:
                _, response = self.original_request(f"https://api.telegram.org/bot{control.BOT_TOKEN}/{request['method']}",
                    method="POST", headers=request["headers"], payload=request["payload"],
                    raw_body=base64.b64decode(request["body"]) if request["body"] is not None else None)
                status = "sent" if isinstance(response, dict) and response.get("ok") is True else "uncertain"
            except control.RemoteHTTPError as exc:
                if exc.status_code == 429:
                    self.state.put("telegram_cooldown", {"until": time.time() + max(30, exc.retry_after)})
                    status = "pending"  # Explicit rejection; it was not delivered.
                else:
                    status = "failed" if exc.status_code in {400, 401, 403, 404} else "uncertain"
            except Exception:
                status = "uncertain"
            self.state.finish_reply(reply_id, status)

    def cooldown_remaining(self):
        until = self.state.get("telegram_cooldown", {}).get("until", 0)
        # Publisher 429 responses also suspend private panel replies.
        operational = self.state.get("publisher", {}).get("bot_next_send_after", 0)
        return max(0, max(until, operational) - time.time())

    def __enter__(self):
        def bind(module, name, value):
            self.stack.enter_context(patch.object(module, name, value))
        bind(control, "read_repo_json", self.read)
        bind(control, "write_repo_json", self.write)
        bind(control, "publisher_run_count", self.active_publisher)
        bind(control, "ensure_publisher_run", lambda: None)  # Resident loop observes DB controls every second.
        bind(control, "_json_request", self.queue_request)
        bind(sources, "_ensure_collector", self.request_collector)
        bind(admin.AdminPanel, "servers", lambda panel: self.snapshot())
        original_health = admin.AdminPanel.health
        def health(panel, store, chat):
            from server_operations import health as worker_health
            original_health(panel, store, chat)
            lines = ["اجرای دائمی و پایگاه داده تراکنشی PostgreSQL"]
            for role in ("bot", "publisher", "collector", "backup"):
                status = worker_health(self.state, role)
                lines.append(f"{'✅' if status['healthy'] else '⚠️'} {role}: {status['status']}")
            replies = self.state.reply_metrics()
            lines.append(f"پاسخ خصوصی در انتظار: {replies.get('pending', 0)} | نامطمئن: {replies.get('uncertain', 0)} | ردشده: {replies.get('failed', 0)}")
            panel.say(chat, "\n".join(lines))
        bind(admin.AdminPanel, "health", health)
        bind(publisher, "load_state", lambda: publisher.normalize_state(self.state.get("publisher", {})))
        bind(publisher, "checkpoint_state", lambda value, reason: self.state.put("publisher", publisher._aggregate_compatibility(value)))
        bind(publisher, "load_destinations_remote", lambda: normalize_store(decrypt_store(self.state.get("destinations", {}))))
        bind(publisher, "load_latest_servers", lambda: self.snapshot(fresh=True))
        bind(publisher, "_current_promo", lambda: promo_from_data(self.state.get("promo", {})))

        def enabled():
            self.state.guard()
            if self.stop is not None and self.stop.is_set():
                raise publisher.PublishingDisabled("worker is stopping")
            if time.monotonic() - self._last_pulse > 20:
                self.state.pulse("publisher", "working")
                self._last_pulse = time.monotonic()
            if self.cooldown_remaining() > 0:
                raise publisher.PublishingDisabled("Telegram cooldown is active")
            if not self.read(control.CONTROL_PATH, control.CONTROL_BRANCH, {"enabled": False})["enabled"]:
                raise publisher.PublishingDisabled("publisher is OFF")
        bind(publisher, "ensure_global_enabled", enabled)
        # Install the same production role/confirmation policy, outermost last.
        for install in (sources.install, promo.install, destinations.install, admin.install):
            self.stack.callback(install(control))
        bind(control, "set_enabled", verified_set_enabled)
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def process_update(self, update):
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int) or isinstance(update.get("update_id"), bool):
            return
        update_id = update["update_id"]
        with self.state.transaction():
            offset = control.load_bot_state()["last_update_id"]
            if update_id <= offset:
                return
            try:
                action, actor, chat, thread, callback = control.update_to_action(update)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                action, actor, chat, thread, callback = (None,) * 5
            if action is not None and actor is not None and chat is not None:
                control.process_action(action, user_id=actor, chat_id=chat, thread_id=thread, callback_id=callback)
            control.save_bot_state({"last_update_id": update_id})
