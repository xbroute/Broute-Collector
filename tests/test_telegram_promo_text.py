import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

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
        self.repo = {
            promo.PROMO_PATH: {
                "url": "https://t.me/xbroutebot",
                "text": "خرید اولیه",
            }
        }
        self.sent = []
        self.commands = []
        self.authorized = True
        self.persist_writes = True
        self.update_to_action = lambda update: (None, None, None, None, None)
        self.process_action = lambda action, **kwargs: None
        self.menu_keyboard = lambda: {"inline_keyboard": []}
        self.register_commands = lambda: None

    def telegram_api(self, method, payload=None):
        payload = payload or {}
        if method == "getMyCommands":
            return list(self.commands)
        if method == "setMyCommands":
            self.commands = list(payload.get("commands", []))
            return True
        raise AssertionError(method)

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
        pass

    def read_repo_json(self, path, ref, default):
        return dict(self.repo.get(path, default))

    def write_repo_json(self, path, branch, payload, message):
        if self.persist_writes:
            self.repo[path] = dict(payload)


def message(text, chat_type="private"):
    return {
        "message": {
            "from": {"id": 42},
            "chat": {"id": 42 if chat_type == "private" else -1001, "type": chat_type},
            "text": text,
        }
    }


class PromoTextConfigTests(unittest.TestCase):
    def test_old_url_only_config_gets_default_text(self):
        parsed = promo.promo_from_data({"url": "https://example.com/buy"})
        self.assertEqual(parsed["url"], "https://example.com/buy")
        self.assertEqual(parsed["text"], promo.DEFAULT_BUTTON_TEXT)

    def test_button_text_validation_accepts_persian_and_emoji(self):
        self.assertEqual(
            promo.normalize_button_text("  🚀 خرید اشتراک ویژه  "),
            "🚀 خرید اشتراک ویژه",
        )

    def test_button_text_rejects_empty_multiline_and_excessive_text(self):
        for value in ("", "   ", "خرید\nاشتراک", "x" * (promo.MAX_BUTTON_TEXT_CHARS + 1)):
            with self.subTest(value=value[:10]):
                with self.assertRaises(ValueError):
                    promo.normalize_button_text(value)

    def test_custom_purchase_text_is_used_in_markup(self):
        payload = controlled.add_purchase_button(
            {},
            "https://example.com/buy",
            "🚀 همین الان خرید کن",
        )
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "🚀 همین الان خرید کن")
        self.assertEqual(button["url"], "https://example.com/buy")


class PromoTextBotTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.restore = install(self.control)

    def tearDown(self):
        self.restore()

    def _run(self, text, chat_type="private"):
        action = self.control.update_to_action(message(text, chat_type))
        self.assertIsNotNone(action[0])
        self.control.process_action(
            action[0],
            user_id=action[1],
            chat_id=action[2],
            thread_id=action[3],
            callback_id=action[4],
        )

    def test_commands_register_button_text_management(self):
        self.control.register_commands()
        names = {item["command"] for item in self.control.commands}
        self.assertTrue({"buy_button", "buy_link", "buy_text"}.issubset(names))

    def test_buy_text_persists_and_preserves_url(self):
        self._run("/buy_text 🚀 خرید ویژه Broute")
        stored = self.control.repo[promo.PROMO_PATH]
        self.assertEqual(stored["url"], "https://t.me/xbroutebot")
        self.assertEqual(stored["text"], "🚀 خرید ویژه Broute")
        self.assertIn("نام دکمه خرید تغییر کرد", self.control.sent[-1][1])

    def test_buy_link_preserves_custom_text(self):
        self._run("/buy_text متن سفارشی")
        self._run("/buy_link https://example.com/new")
        stored = self.control.repo[promo.PROMO_PATH]
        self.assertEqual(stored["url"], "https://example.com/new")
        self.assertEqual(stored["text"], "متن سفارشی")

    def test_buy_text_requires_private_chat(self):
        before = dict(self.control.repo[promo.PROMO_PATH])
        self._run("/buy_text نباید ذخیره شود", "supergroup")
        self.assertEqual(self.control.repo[promo.PROMO_PATH], before)
        self.assertIn("فقط در پیام خصوصی", self.control.sent[-1][1])

    def test_buy_text_readback_mismatch_is_retryable(self):
        self.control.persist_writes = False
        action = self.control.update_to_action(message("/buy_text متن جدید"))
        with self.assertRaises(RetryableCommandError):
            self.control.process_action(
                action[0],
                user_id=action[1],
                chat_id=action[2],
                thread_id=action[3],
                callback_id=action[4],
            )

    def test_show_settings_includes_both_text_and_url(self):
        self._run("/buy_button")
        body = self.control.sent[-1][1]
        self.assertIn("خرید اولیه", body)
        self.assertIn("https://t.me/xbroutebot", body)
        self.assertIn("/buy_text", body)
        self.assertIn("/buy_link", body)


if __name__ == "__main__":
    unittest.main()
