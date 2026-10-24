import os
import time
import unittest
from unittest.mock import patch

import test_server_state as state_tests

if state_tests.PostgresState:
    from server_bindings import Bindings, SnapshotUnavailable
    import telegram_bot_control as control
    import telegram_multi_publisher as publisher


@unittest.skipUnless(state_tests.PostgresState and os.environ.get("TEST_DATABASE_URL"), "requires a real isolated PostgreSQL service")
class RuntimeIntegrationTests(unittest.TestCase):
    setUp = state_tests.PostgresIntegrationTests.setUp
    tearDown = state_tests.PostgresIntegrationTests.tearDown

    def update(self, text="/publisher_off", update_id=1, actor=123):
        return {"update_id": update_id, "message": {"text": text, "from": {"id": actor}, "chat": {"id": actor, "type": "private"}}}

    def test_off_and_offset_commit_before_reply_and_duplicate_update_is_ignored(self):
        self.db.put("control", {"enabled": True})
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "123"}), self.db.lease("bot"), Bindings(self.db) as bindings:
            with patch.object(control, "github_api", side_effect=AssertionError("GitHub must not be called")):
                bindings.process_update(self.update())
                bindings.process_update(self.update())
            self.assertFalse(self.db.get("control")["enabled"])
            self.assertEqual(self.db.get("offset"), {"last_update_id": 1})
            with patch.object(bindings, "original_request", side_effect=TimeoutError("reply lost")) as request:
                bindings.flush_replies()
                bindings.flush_replies()
                request.assert_called_once()
            self.assertFalse(self.db.get("control")["enabled"])

    def test_failed_offset_rolls_back_mutation_and_success_reply(self):
        self.db.put("control", {"enabled": False})
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "123"}), Bindings(self.db) as bindings:
            with patch.object(control, "save_bot_state", side_effect=RuntimeError("offset write failed")):
                with self.assertRaises(RuntimeError):
                    bindings.process_update(self.update("/publisher_on"))
            self.assertFalse(self.db.get("control")["enabled"])
            self.assertIsNone(self.db.get("offset"))
            self.assertIsNone(self.db.next_reply())

    def test_collector_refresh_request_commits_with_offset_and_never_dispatches_github(self):
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "123"}), Bindings(self.db) as bindings:
            with patch.object(control, "github_api", side_effect=AssertionError("external dispatch")):
                bindings.process_update(self.update("/collector_refresh"))
            self.assertTrue(self.db.get("collector_request")["id"])
            self.assertEqual(self.db.get("offset")["last_update_id"], 1)

    def test_private_reply_429_preserves_pending_response_and_blocks_publisher(self):
        self.db.put("control", {"enabled": True})
        self.db.enqueue_reply({"method": "sendMessage", "payload": {}, "headers": None, "body": None})
        with Bindings(self.db) as bindings:
            with patch.object(bindings, "original_request", side_effect=control.RemoteHTTPError(429, "api.telegram.org", 60)):
                bindings.flush_replies()
            with self.assertRaises(publisher.PublishingDisabled):
                publisher.ensure_global_enabled()
        self.assertGreater(self.db.get("telegram_cooldown")["until"], time.time())
        self.assertIsNotNone(self.db.next_reply())

    def test_stale_snapshot_is_not_eligible_for_publishing(self):
        self.db.put("collector_snapshot", {"at": time.time() - 1000, "servers": [{"id": "a"}]})
        with Bindings(self.db) as bindings:
            self.assertEqual(bindings.snapshot(), [{"id": "a"}])
            with self.assertRaises(SnapshotUnavailable):
                publisher.load_latest_servers()

    def test_unauthorized_operator_cannot_enable_publisher(self):
        self.db.put("control", {"enabled": False})
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "123"}), Bindings(self.db) as bindings:
            bindings.process_update(self.update("/publisher_on", actor=456))
        self.assertFalse(self.db.get("control")["enabled"])
        self.assertEqual(self.db.get("offset")["last_update_id"], 1)

    def test_health_and_following_off_do_not_call_github_or_block_offset(self):
        self.db.put("control", {"enabled": True})
        self.db.put("collector_snapshot", {"at": time.time(), "servers": [], "status": {"online_configs": 0}})
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "123"}), Bindings(self.db) as bindings:
            with patch.object(control, "github_api", side_effect=AssertionError("GitHub must not be called")):
                bindings.process_update(self.update("/health", 1))
                bindings.process_update(self.update("/publisher_off", 2))
        self.assertEqual(self.db.get("offset")["last_update_id"], 2)
        self.assertFalse(self.db.get("control")["enabled"])
