import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_copy_format as copy_format
import telegram_promo_config as promo
import telegram_publisher_controlled as controlled
from telegram_bot_promo_extension import install


class RetryableCommandError(RuntimeError):
    pass


class FakeControl(SimpleNamespace):
    def __init__(self):
        super().__init__()
        self.CONTROL_BRANCH = "main"
        self.RetryableCommandError = RetryableCommandError
        self.repo = {promo.PROMO_PATH: {"url": promo.DEFAULT_PROMO_URL}}
        self.sent = []
        self.callback_answers = []
        self.commands = []
        self.authorized = True
        self.persist_writes = True

        self.update_to_action = lambda update: (None, None, None, None, None)
        self.process_action = lambda action, **kwargs: None
        self.menu_keyboard = lambda: {
            "inline_keyboard": [[{"text": "📚 منابع", "callback_data": "source:list"}]]
        }
        self.register_commands = self._register_base

    def _register_base(self):
        self.commands = [
            {"command": "publisher", "description": "base"},
            {"command": "source_add", "description": "source"},
        ]

    def telegram_api(self, method, payload=None):
        payload = payload or {}
        if method == "getMyCommands":
            return list(self.commands)
        if method == "setMyCommands":
            self.commands = list(payload.get("commands", []))
            return True
        raise AssertionError(f"unexpected Telegram API method {method}")

    @staticmethod
    def normalize_command(text):
        token = str(text or "").strip().split(maxsplit=1)[0].lower() if str(text or "").strip() else ""
        if token.startswith("/") and "@" in token:
            token = token.split("@", 1)[0]
        return token

    @staticmethod
    def message_thread_id(message):
        return message.get("message_thread_id")

    def is_authorized_admin(self, user_id):
        return self.authorized

    def safe_send_text(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    def answer_callback(self, callback_id, text=""):
        self.callback_answers.append((callback_id, text))

    def read_repo_json(self, path, ref, default):
        return dict(self.repo.get(path, default))

    def write_repo_json(self, path, branch, payload, message):
        if self.persist_writes:
            self.repo[path] = dict(payload)


def private_message(text):
    return {
        "message": {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": text,
        }
    }


def group_message(text):
    return {
        "message": {
            "from": {"id": 42},
            "chat": {"id": -1001, "type": "supergroup"},
            "text": text,
        }
    }


class PromoConfigTests(unittest.TestCase):
    def test_default_url_and_button_text(self):
        self.assertEqual(promo.DEFAULT_PROMO_URL, "https://t.me/xbroutebot")
        self.assertEqual(promo.BUTTON_TEXT, "خرید اشتراک پرسرعت و بدون قطعی")

    def test_tme_without_scheme_is_normalized(self):
        self.assertEqual(
            promo.normalize_promo_url("t.me/xbroutebot?start=free"),
            "https://t.me/xbroutebot?start=free",
        )

    def test_http_and_https_are_accepted(self):
        self.assertEqual(
            promo.normalize_promo_url("https://example.com/a?x=1#b"),
            "https://example.com/a?x=1#b",
        )
        self.assertEqual(
            promo.normalize_promo_url("http://example.com/path"),
            "http://example.com/path",
        )

    def test_unsafe_schemes_and_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            promo.normalize_promo_url("javascript:alert(1)")
        with self.assertRaises(ValueError):
            promo.normalize_promo_url("https://user:pass@example.com/path")
        with self.assertRaises(ValueError):
            promo.normalize_promo_url("https://example.com/a b")


class PromoButtonTests(unittest.TestCase):
    def test_short_config_keeps_copy_subscription_and_purchase_rows(self):
        config = "vless://short#@xbroute"
        message = copy_format.make_copyable_message(
            f"head\n\n{config}\n\ntail", config
        )
        payload = copy_format.decorate_send_payload({"text": message, "chat_id": 1})
        payload = controlled.add_subscription_button(payload)
        payload = controlled.add_purchase_button(payload, "https://t.me/xbroutebot")

        rows = payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(rows), 3)
        self.assertIn("copy_text", rows[0][0])
        self.assertEqual(rows[1][0]["text"], "🔄 لینک سابسکریپشن")
        self.assertEqual(rows[2][0]["text"], "خرید اشتراک پرسرعت و بدون قطعی")
        self.assertEqual(rows[2][0]["url"], "https://t.me/xbroutebot")

    def test_long_config_has_subscription_and_purchase_without_truncation(self):
        config = "vless://" + "x" * 400
        message = copy_format.make_copyable_message(
            f"head\n\n{config}\n\ntail", config
        )
        payload = copy_format.decorate_send_payload({"text": message})
        payload = controlled.add_subscription_button(payload)
        payload = controlled.add_purchase_button(payload, "https://example.com/buy")

        rows = payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(rows), 2)
        self.assertNotIn("copy_text", rows[0][0])
        self.assertEqual(rows[-1][0]["url"], "https://example.com/buy")
        self.assertIn(config, payload["text"])

    def test_purchase_button_is_idempotent(self):
        payload = {}
        first = controlled.add_purchase_button(payload, "https://example.com/buy")
        second = controlled.add_purchase_button(first, "https://example.com/buy")
        rows = second["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(rows), 1)


class PromoBotExtensionTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.restore = install(self.control)

    def tearDown(self):
        self.restore()

    def test_menu_and_command_registration_include_purchase_link(self):
        keyboard = self.control.menu_keyboard()
        callbacks = [
            button.get("callback_data")
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("source:list", callbacks)
        self.assertIn("promo:show", callbacks)

        self.control.register_commands()
        names = [item["command"] for item in self.control.commands]
        self.assertIn("publisher", names)
        self.assertIn("source_add", names)
        self.assertIn("buy_link", names)

    def test_private_buy_link_change_is_persisted_and_read_back(self):
        action = self.control.update_to_action(private_message("/buy_link t.me/newbot?start=x"))
        self.assertEqual(action[0], "promo_link")
        self.control.process_action(
            action[0], user_id=action[1], chat_id=action[2], thread_id=action[3]
        )
        self.assertEqual(
            self.control.repo[promo.PROMO_PATH]["url"],
            "https://t.me/newbot?start=x",
        )
        self.assertIn("✅ لینک دکمه خرید تغییر کرد", self.control.sent[-1][1])

    def test_group_change_is_refused(self):
        before = dict(self.control.repo[promo.PROMO_PATH])
        action = self.control.update_to_action(group_message("/buy_link https://example.com/buy"))
        self.control.process_action(
            action[0], user_id=action[1], chat_id=action[2], thread_id=action[3]
        )
        self.assertEqual(self.control.repo[promo.PROMO_PATH], before)
        self.assertIn("فقط در پیام خصوصی", self.control.sent[-1][1])

    def test_buy_link_without_argument_shows_current_value(self):
        action = self.control.update_to_action(private_message("/buy_link"))
        self.control.process_action(
            action[0], user_id=action[1], chat_id=action[2], thread_id=action[3]
        )
        self.assertIn(promo.DEFAULT_PROMO_URL, self.control.sent[-1][1])

    def test_readback_mismatch_is_retryable(self):
        self.control.persist_writes = False
        action = self.control.update_to_action(private_message("/buy_link https://example.com/new"))
        with self.assertRaises(RetryableCommandError):
            self.control.process_action(
                action[0], user_id=action[1], chat_id=action[2], thread_id=action[3]
            )

    def test_unauthorized_admin_cannot_change_url(self):
        self.control.authorized = False
        action = self.control.update_to_action(private_message("/buy_link https://example.com/new"))
        self.control.process_action(
            action[0], user_id=action[1], chat_id=action[2], thread_id=action[3]
        )
        self.assertEqual(
            self.control.repo[promo.PROMO_PATH]["url"], promo.DEFAULT_PROMO_URL
        )
        self.assertIn("فقط ادمین", self.control.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
