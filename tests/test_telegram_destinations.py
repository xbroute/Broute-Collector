import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_destinations as destinations


class TelegramDestinationConfigTests(unittest.TestCase):
    def test_encrypted_roundtrip_hides_plain_chat_metadata(self):
        store = {
            "manager_user_ids": [123456],
            "destinations": [
                {
                    "chat_id": -1001234567890,
                    "title": "Private Channel",
                    "username": "",
                    "chat_type": "channel",
                    "bot_status": "administrator",
                    "enabled": True,
                    "min_delay_seconds": 30,
                    "max_delay_seconds": 90,
                    "template": destinations.DEFAULT_TEMPLATE,
                }
            ],
        }
        encrypted = destinations.encrypt_store(store, "test-secret")
        self.assertNotIn("Private Channel", encrypted["ciphertext"])
        self.assertNotIn("-1001234567890", encrypted["ciphertext"])
        decoded = destinations.decrypt_store(encrypted, "test-secret")
        self.assertEqual(decoded["manager_user_ids"], [123456])
        self.assertEqual(decoded["destinations"][0]["chat_id"], -1001234567890)

    def test_single_interval_uses_approximately_twenty_percent_jitter(self):
        self.assertEqual(destinations.parse_interval_spec("60"), (48, 72))
        self.assertEqual(destinations.parse_interval_spec("2m"), (96, 144))

    def test_explicit_interval_range_is_preserved(self):
        self.assertEqual(destinations.parse_interval_spec("30-90"), (30, 90))
        self.assertEqual(destinations.parse_interval_spec("2m-4m"), (120, 240))

    def test_too_fast_interval_is_rejected(self):
        with self.assertRaises(destinations.DestinationValidationError):
            destinations.parse_interval_spec("5-10")

    def test_template_requires_exact_config_placeholder(self):
        self.assertEqual(
            destinations.normalize_template("x\n{config}\n{country}"),
            "x\n{config}\n{country}",
        )
        with self.assertRaises(destinations.DestinationValidationError):
            destinations.normalize_template("{country}")
        with self.assertRaises(destinations.DestinationValidationError):
            destinations.normalize_template("{config}\n{config}")

    def test_unknown_template_placeholder_is_rejected(self):
        with self.assertRaises(destinations.DestinationValidationError):
            destinations.normalize_template("{config}\n{evil}")

    def test_selector_supports_index_key_and_username(self):
        items = [
            destinations.normalize_destination(
                {
                    "chat_id": -1001,
                    "title": "One",
                    "username": "onechannel",
                    "chat_type": "channel",
                    "bot_status": "administrator",
                }
            )
        ]
        self.assertEqual(destinations.find_destination(items, "1")[0], 0)
        self.assertEqual(
            destinations.find_destination(items, items[0]["key"][:5])[0], 0
        )
        self.assertEqual(destinations.find_destination(items, "@onechannel")[0], 0)


if __name__ == "__main__":
    unittest.main()
