import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_bot_control as control
import telegram_bot_destination_extension as destination_extension
import telegram_multidestination_publisher as multi


class TelegramDestinationControlTests(unittest.TestCase):
    def tearDown(self):
        destination_extension._PENDING.clear()
        destination_extension._EVENTS.clear()

    def test_my_chat_member_update_becomes_destination_event(self):
        restore = destination_extension.install(control)
        try:
            update = {
                "update_id": 99,
                "my_chat_member": {
                    "from": {"id": 123},
                    "chat": {
                        "id": -100555,
                        "type": "channel",
                        "title": "Target",
                    },
                    "old_chat_member": {"status": "member"},
                    "new_chat_member": {
                        "status": "administrator",
                        "can_post_messages": True,
                    },
                },
            }
            action = control.update_to_action(update)
            self.assertEqual(action[:4], ("dest_member_event", 123, -100555, None))
            self.assertIn((123, -100555), destination_extension._EVENTS)
        finally:
            restore()

    def test_start_is_intercepted_by_destination_owner_layer(self):
        restore = destination_extension.install(control)
        try:
            update = {
                "message": {
                    "from": {"id": 123},
                    "chat": {"id": 123, "type": "private"},
                    "text": "/start",
                }
            }
            action = control.update_to_action(update)
            self.assertEqual(action[0], "menu")
        finally:
            restore()

    def test_permanent_destination_errors_are_isolated_candidates(self):
        self.assertTrue(multi._permanent_destination_error(RuntimeError("Forbidden: bot was kicked")))
        self.assertTrue(multi._permanent_destination_error(RuntimeError("Bad Request: chat not found")))
        self.assertFalse(multi._permanent_destination_error(RuntimeError("temporary network reset")))


if __name__ == "__main__":
    unittest.main()
