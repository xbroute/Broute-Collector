import base64
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_copy_format as copy_format
import telegram_publisher as publisher


def sample_server(**overrides):
    server = {
        "id": "srv-1",
        "status": "online",
        "valid": True,
        "should_remove": False,
        "raw": "vless://uuid@example.com:443?security=reality&type=ws#old-name",
        "protocol": "vless",
        "address": "example.com",
        "port": 443,
        "transport": "ws",
        "security": "reality",
        "tls": True,
        "latency": 42,
        "country": "US",
        "country_name": "United States",
    }
    server.update(overrides)
    return server


class TelegramPublisherMessageTests(unittest.TestCase):
    def test_reality_wins_over_tls_flag(self):
        self.assertEqual(
            publisher.security_label(sample_server()),
            "🛡 امنیت: Reality",
        )

    def test_non_vmess_brand_is_literal_xbroute(self):
        raw = "vless://uuid@example.com:443?type=ws#anything"
        self.assertEqual(
            publisher.brand_raw_config(raw, "vless"),
            "vless://uuid@example.com:443?type=ws#@xbroute",
        )

    def test_vmess_ps_is_rebranded(self):
        data = {
            "v": "2",
            "ps": "old",
            "add": "example.com",
            "port": "443",
            "id": "00000000-0000-0000-0000-000000000000",
            "aid": "0",
            "net": "ws",
            "type": "none",
            "host": "example.com",
            "path": "/",
            "tls": "tls",
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        branded = publisher.brand_raw_config(f"vmess://{encoded}", "vmess")
        decoded_payload = branded[len("vmess://"):]
        decoded_payload += "=" * (-len(decoded_payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(decoded_payload).decode())
        self.assertEqual(decoded["ps"], "@xbroute")

    def test_message_contains_expected_metadata_and_no_source(self):
        message = publisher.build_message(sample_server(source_name="should-not-leak"))
        self.assertIn("🟢 کانفیگ رایگان", message)
        self.assertIn("🇺🇸 کشور: United States", message)
        self.assertIn("🔹 پروتکل: VLESS", message)
        self.assertIn("🛡 امنیت: Reality", message)
        self.assertIn("🔌 شبکه: WebSocket", message)
        self.assertIn("⚡ تأخیر تست: 42ms", message)
        self.assertIn("#@xbroute", message)
        self.assertNotIn("source_name", message)
        self.assertNotIn("should-not-leak", message)

    def test_copyable_message_escapes_html_but_preserves_exact_clipboard_text(self):
        config = "vless://u@h:443?x=1&y=<value>#@xbroute"
        plain = f"🟢 کانفیگ رایگان\n\n{config}\n\n🔗 @xbroute"
        message = copy_format.make_copyable_message(plain, config)

        self.assertIsInstance(message, copy_format.CopyableTelegramMessage)
        self.assertEqual(message.copy_text, config)
        self.assertIn("<pre>", message)
        self.assertIn("&amp;", message)
        self.assertIn("&lt;value&gt;", message)
        self.assertNotIn("<value>", message)

    def test_short_config_gets_native_copy_text_button(self):
        config = "vless://short#@xbroute"
        message = copy_format.make_copyable_message(
            f"head\n\n{config}\n\ntail", config
        )
        payload = copy_format.decorate_send_payload({"text": message, "chat_id": 1})

        self.assertEqual(payload["parse_mode"], "HTML")
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "📋 کپی کانفیگ")
        self.assertEqual(button["copy_text"]["text"], config)

    def test_256_character_config_gets_native_button(self):
        config = "x" * 256
        markup = copy_format.native_copy_markup(config)
        self.assertIsNotNone(markup)
        self.assertEqual(
            markup["inline_keyboard"][0][0]["copy_text"]["text"], config
        )

    def test_long_config_is_never_truncated(self):
        config = "vless://" + ("x" * 300)
        message = copy_format.make_copyable_message(
            f"head\n\n{config}\n\ntail", config
        )
        payload = copy_format.decorate_send_payload({"text": message})

        self.assertEqual(message.copy_text, config)
        self.assertIn(config, payload["text"])
        self.assertNotIn("reply_markup", payload)
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_plain_non_publisher_payload_is_untouched(self):
        payload = {"text": "plain", "chat_id": 1}
        self.assertIs(copy_format.decorate_send_payload(payload), payload)


if __name__ == "__main__":
    unittest.main()
