import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_bot_control as control
import telegram_bot_control_verified as verified


class TelegramBotControlVerifiedTests(unittest.TestCase):
    def test_off_write_is_read_back_before_success(self):
        with (
            patch.object(control, "current_enabled", side_effect=[True, False]),
            patch.object(control, "write_repo_json") as write_json,
            patch.object(control, "ensure_publisher_run") as ensure_run,
        ):
            self.assertTrue(verified.verified_set_enabled(False))
            write_json.assert_called_once_with(
                control.CONTROL_PATH,
                control.CONTROL_BRANCH,
                {"enabled": False},
                "chore: turn Telegram publisher OFF via bot",
            )
            ensure_run.assert_not_called()

    def test_readback_mismatch_is_retryable_and_does_not_dispatch(self):
        with (
            patch.object(control, "current_enabled", side_effect=[False, False]),
            patch.object(control, "write_repo_json") as write_json,
            patch.object(control, "ensure_publisher_run") as ensure_run,
        ):
            with self.assertRaises(control.RetryableCommandError):
                verified.verified_set_enabled(True)
            write_json.assert_called_once()
            ensure_run.assert_not_called()

    def test_readback_failure_is_retryable(self):
        with (
            patch.object(
                control,
                "current_enabled",
                side_effect=[True, RuntimeError("temporary GitHub read failure")],
            ),
            patch.object(control, "write_repo_json") as write_json,
            patch.object(control, "ensure_publisher_run") as ensure_run,
        ):
            with self.assertRaises(control.RetryableCommandError):
                verified.verified_set_enabled(False)
            write_json.assert_called_once()
            ensure_run.assert_not_called()

    def test_on_dispatches_only_after_successful_readback(self):
        order = []

        def current_enabled():
            order.append("read")
            return False if order.count("read") == 1 else True

        def write_repo_json(*args, **kwargs):
            order.append("write")

        def ensure_publisher_run():
            order.append("dispatch")

        with (
            patch.object(control, "current_enabled", side_effect=current_enabled),
            patch.object(control, "write_repo_json", side_effect=write_repo_json),
            patch.object(control, "ensure_publisher_run", side_effect=ensure_publisher_run),
        ):
            self.assertTrue(verified.verified_set_enabled(True))

        self.assertEqual(order, ["read", "write", "read", "dispatch"])

    def test_already_enabled_does_not_rewrite_but_still_ensures_run(self):
        with (
            patch.object(control, "current_enabled", return_value=True),
            patch.object(control, "write_repo_json") as write_json,
            patch.object(control, "ensure_publisher_run") as ensure_run,
        ):
            self.assertFalse(verified.verified_set_enabled(True))
            write_json.assert_not_called()
            ensure_run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
