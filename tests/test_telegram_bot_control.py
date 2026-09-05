import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_bot_control as control


class TelegramBotControlTests(unittest.TestCase):
    def test_command_with_bot_username_is_normalized(self):
        update = {
            "message": {
                "from": {"id": 123},
                "chat": {"id": 456},
                "text": "/publisher_off@BrouteFreeConfigBot",
            }
        }
        action, user_id, chat_id, thread_id, callback_id = control.update_to_action(update)
        self.assertEqual(action, "off")
        self.assertEqual(user_id, 123)
        self.assertEqual(chat_id, 456)
        self.assertIsNone(thread_id)
        self.assertIsNone(callback_id)

    def test_callback_is_parsed(self):
        update = {
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 123},
                "data": "publisher:off",
                "message": {
                    "chat": {"id": 456},
                    "message_thread_id": 1944,
                },
            }
        }
        self.assertEqual(
            control.update_to_action(update),
            ("off", 123, 456, 1944, "cb-1"),
        )

    def test_admin_ids_come_from_current_chat_administrators(self):
        admins = [
            {"user": {"id": 100, "is_bot": False}},
            {"user": {"id": 200, "is_bot": True}},
            {"user": {"id": 300, "is_bot": False}},
        ]
        with patch.object(control, "telegram_api", return_value=admins):
            self.assertEqual(control.current_admin_ids(), {100, 300})

    def test_transient_command_failure_does_not_advance_offset(self):
        state = {"last_update_id": 10}
        update = {
            "update_id": 11,
            "message": {
                "from": {"id": 123},
                "chat": {"id": 456},
                "text": "/publisher_off",
            },
        }

        def fake_telegram(method, payload=None):
            if method == "getUpdates":
                return [update]
            raise AssertionError(f"unexpected Telegram call: {method}")

        with (
            patch.object(control, "BOT_TOKEN", "test-token"),
            patch.object(control, "GH_TOKEN", "test-gh"),
            patch.object(control, "load_bot_state", return_value=state),
            patch.object(control, "register_commands"),
            patch.object(control, "telegram_api", side_effect=fake_telegram),
            patch.object(
                control,
                "process_action",
                side_effect=control.RetryableCommandError("temporary"),
            ),
            patch.object(control, "save_bot_state") as save_state,
        ):
            self.assertEqual(control.main(), 1)
            self.assertEqual(state["last_update_id"], 10)
            save_state.assert_not_called()

    def test_successful_command_advances_and_checkpoints_offset(self):
        state = {"last_update_id": 20}
        update = {
            "update_id": 21,
            "message": {
                "from": {"id": 123},
                "chat": {"id": 456},
                "text": "/publisher_status",
            },
        }

        def fake_telegram(method, payload=None):
            if method == "getUpdates":
                return [update]
            raise AssertionError(f"unexpected Telegram call: {method}")

        snapshots = []

        with (
            patch.object(control, "BOT_TOKEN", "test-token"),
            patch.object(control, "GH_TOKEN", "test-gh"),
            patch.object(control, "load_bot_state", return_value=state),
            patch.object(control, "register_commands"),
            patch.object(control, "telegram_api", side_effect=fake_telegram),
            patch.object(control, "process_action"),
            patch.object(
                control,
                "save_bot_state",
                side_effect=lambda value: snapshots.append(dict(value)),
            ),
        ):
            self.assertEqual(control.main(), 0)
            self.assertEqual(state["last_update_id"], 21)
            self.assertEqual(snapshots[-1]["last_update_id"], 21)


if __name__ == "__main__":
    unittest.main()
