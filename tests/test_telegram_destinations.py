import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_destinations as destinations


class TelegramDestinationTests(unittest.TestCase):
    def test_encryption_roundtrip_hides_plain_chat_metadata(self):
        state = destinations.default_state()
        state["owners"] = [123456]
        item = destinations.upsert_destination(
            state,
            chat_id=-1001234567890,
            chat_type="channel",
            title="Private Channel",
            username="",
            ready=True,
        )
        item["enabled"] = True
        payload = destinations.encrypt_state(state, secret="unit-test-secret")
        self.assertNotIn("-1001234567890", payload["ciphertext"])
        restored = destinations.decrypt_state(payload, secret="unit-test-secret")
        self.assertEqual(restored["owners"], [123456])
        self.assertEqual(restored["destinations"][0]["chat_id"], -1001234567890)
        self.assertTrue(restored["destinations"][0]["enabled"])

    def test_destination_is_disabled_by_default(self):
        state = destinations.default_state()
        item = destinations.upsert_destination(
            state,
            chat_id=-1001,
            chat_type="supergroup",
            title="Test",
            ready=True,
        )
        self.assertFalse(item["enabled"])
        self.assertEqual(item["status"], "ready")

    def test_permission_loss_forces_destination_off(self):
        state = destinations.default_state()
        item = destinations.upsert_destination(
            state,
            chat_id=-1002,
            chat_type="channel",
            title="Channel",
            ready=True,
        )
        item["enabled"] = True
        item = destinations.upsert_destination(
            state,
            chat_id=-1002,
            chat_type="channel",
            title="Channel",
            ready=False,
        )
        self.assertFalse(item["enabled"])
        self.assertEqual(item["status"], "missing_permission")

    def test_delay_validation(self):
        self.assertEqual(destinations.validate_delay(30, 90), (30, 90))
        with self.assertRaises(destinations.DestinationValidationError):
            destinations.validate_delay(5, 90)
        with self.assertRaises(destinations.DestinationValidationError):
            destinations.validate_delay(100, 90)

    def test_active_destinations_only_returns_ready_enabled(self):
        state = destinations.default_state()
        one = destinations.upsert_destination(
            state, chat_id=-1, chat_type="channel", title="One", ready=True
        )
        two = destinations.upsert_destination(
            state, chat_id=-2, chat_type="channel", title="Two", ready=True
        )
        one["enabled"] = True
        two["enabled"] = False
        self.assertEqual([x["id"] for x in destinations.active_destinations(state)], [one["id"]])


if __name__ == "__main__":
    unittest.main()
