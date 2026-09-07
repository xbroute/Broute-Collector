import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import generate_online_subscription as online_sub
import telegram_publisher_controlled as controlled


class OnlineSubscriptionTests(unittest.TestCase):
    def test_feed_contains_only_current_online_valid_configs(self):
        servers = [
            {"status": "online", "valid": True, "should_remove": False, "raw": "vless://one"},
            {"status": "offline", "valid": True, "should_remove": False, "raw": "vless://two"},
            {"status": "online", "valid": False, "should_remove": False, "raw": "vless://three"},
            {"status": "online", "valid": True, "should_remove": True, "raw": "vless://four"},
            {"status": "online", "valid": True, "should_remove": False, "raw": "vless://one"},
            {"status": "online", "valid": True, "should_remove": False, "raw": "trojan://five"},
        ]
        self.assertEqual(
            online_sub.online_lines(servers),
            ["vless://one", "trojan://five"],
        )

    def test_footer_is_explicit_and_contains_stable_url(self):
        footer = controlled.subscription_footer()
        self.assertIn("سابسکریپشن", footer)
        self.assertIn("همیشه‌به‌روز", footer)
        self.assertIn("همه کانفیگ‌های آنلاین", footer)
        self.assertIn("Update/Refresh", footer)
        self.assertIn(controlled.ONLINE_SUBSCRIPTION_URL, footer)

    def test_subscription_button_exists_without_native_copy_button(self):
        payload = controlled.add_subscription_button({"chat_id": 1, "text": "x"})
        rows = payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[-1][0]["text"], "🔄 لینک سابسکریپشن")
        self.assertEqual(rows[-1][0]["url"], controlled.ONLINE_SUBSCRIPTION_URL)

    def test_subscription_button_preserves_existing_copy_button(self):
        payload = {
            "reply_markup": {
                "inline_keyboard": [[{"text": "copy", "copy_text": {"text": "abc"}}]]
            }
        }
        decorated = controlled.add_subscription_button(payload)
        rows = decorated["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[0][0]["copy_text"]["text"], "abc")
        self.assertEqual(rows[1][0]["url"], controlled.ONLINE_SUBSCRIPTION_URL)
        self.assertTrue(controlled._has_native_copy_button(decorated))


if __name__ == "__main__":
    unittest.main()
