import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_destinations as destinations
import telegram_bot_destination_extension as extension


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

    def _fake_control(self, admin_ids):
        fake = SimpleNamespace()
        fake.update_to_action = lambda update: (None, None, None, None, None)
        fake.process_action = lambda *args, **kwargs: None
        fake.register_commands = lambda: None
        fake.menu_keyboard = lambda: {"inline_keyboard": []}
        fake.is_authorized_admin = lambda user_id: False
        fake.current_admin_ids = lambda: set()
        fake.message_thread_id = lambda message: None
        fake.safe_send_text = lambda *args, **kwargs: None
        fake.answer_callback = lambda *args, **kwargs: None
        fake.current_enabled = lambda: True
        fake.ensure_publisher_run = lambda: None
        fake.normalize_command = lambda text: str(text or "").split(maxsplit=1)[0].lower()
        fake.RetryableCommandError = RuntimeError

        def telegram_api(method, payload=None):
            if method == "getChatAdministrators":
                return [
                    {"user": {"id": value, "is_bot": False}}
                    for value in admin_ids
                ]
            if method == "getMyCommands":
                return []
            if method == "setMyCommands":
                return True
            raise AssertionError(method)

        fake.telegram_api = telegram_api
        return fake

    def test_first_verified_promoter_bootstraps_manager_and_destination(self):
        store = destinations.default_store()
        fake = self._fake_control({111})
        restore = extension.install(fake)
        update = {
            "my_chat_member": {
                "from": {"id": 111},
                "chat": {
                    "id": -100555,
                    "type": "channel",
                    "title": "My Channel",
                },
                "new_chat_member": {"status": "administrator"},
            }
        }

        def save(_, value, message):
            # _membership mutates the same in-memory store before persistence.
            # Return a normalized read-back snapshot without clearing that object.
            return destinations.normalize_store(value)

        try:
            with (
                patch.object(extension, "_load_store", side_effect=lambda _: store),
                patch.object(extension, "_save_store_verified", side_effect=save),
            ):
                action, user_id, chat_id, thread_id, callback_id = fake.update_to_action(update)
                self.assertEqual(action, "target_membership")
                fake.process_action(
                    action,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    callback_id=callback_id,
                )
        finally:
            restore()

        self.assertEqual(store["manager_user_ids"], [111])
        self.assertEqual(len(store["destinations"]), 1)
        self.assertEqual(store["destinations"][0]["chat_id"], -100555)
        self.assertFalse(store["destinations"][0]["enabled"])

    def test_untrusted_promoter_cannot_register_destination_after_owner_exists(self):
        store = {"manager_user_ids": [111], "destinations": []}
        fake = self._fake_control({222})
        restore = extension.install(fake)
        update = {
            "my_chat_member": {
                "from": {"id": 222},
                "chat": {
                    "id": -100777,
                    "type": "supergroup",
                    "title": "Attacker Group",
                },
                "new_chat_member": {"status": "administrator"},
            }
        }

        def save(_, value, message):
            store.clear()
            store.update(destinations.normalize_store(value))
            return store

        try:
            with (
                patch.object(extension, "_load_store", side_effect=lambda _: store),
                patch.object(extension, "_save_store_verified", side_effect=save),
            ):
                action, user_id, chat_id, thread_id, callback_id = fake.update_to_action(update)
                fake.process_action(
                    action,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    callback_id=callback_id,
                )
        finally:
            restore()

        self.assertEqual(store["manager_user_ids"], [111])
        self.assertEqual(store["destinations"], [])


if __name__ == "__main__":
    unittest.main()
