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
    tearDown = state_tests.PostgresIntegrationTests.tearDown

    def setUp(self):
        state_tests.PostgresIntegrationTests.setUp(self)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "isolated-test-token"}).start()
        patch.object(control, "BOT_TOKEN", "isolated-test-token").start()
        def request(url, **kwargs):
            if url.endswith("/getMe"):
                return 200, {"ok": True, "result": {"id": 999}}
            raise AssertionError("unexpected external request in integration test")
        patch.object(control, "_json_request", side_effect=request).start()

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

    def test_shutdown_does_not_claim_or_send_a_pending_reply(self):
        import threading
        self.db.enqueue_reply({"method": "sendMessage", "payload": {}, "headers": None, "body": None})
        stop = threading.Event()
        stop.set()
        with Bindings(self.db, stop=stop) as bindings, patch.object(bindings, "original_request") as request:
            bindings.flush_replies()
            request.assert_not_called()
        self.assertIsNotNone(self.db.next_reply())

    def test_fifty_sequential_admin_commands_keep_offsets_audit_and_replies_consistent(self):
        self.db.put("control", {"enabled": False})
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "123"}), self.db.lease("bot"), Bindings(self.db) as bindings:
            for update_id in range(1, 51):
                bindings.process_update(self.update("/publisher_on" if update_id % 2 else "/publisher_off", update_id))
        self.assertEqual(self.db.get("offset")["last_update_id"], 50)
        self.assertFalse(self.db.get("control")["enabled"])
        self.assertEqual(self.db.reply_metrics().get("pending"), 50)

    def test_send_intent_commits_before_http_and_crash_is_quarantined_on_restart(self):
        import telegram_destinations
        import test_telegram_delivery_journal as journal
        from contextlib import ExitStack
        self.db.put("control", {"enabled": True})
        self.db.put("collector_snapshot", {"at": time.time(), "servers": [journal.server()]})
        self.db.put("destinations", telegram_destinations.encrypt_store({"manager_user_ids": [], "destinations": [journal.destination()]}))
        original_put = self.db.put
        def fail_confirmed_write(name, value):
            if name == "publisher" and value["destinations"].get("-1001", {}).get("sent"):
                raise RuntimeError("power loss after HTTP accepted")
            return original_put(name, value)
        def accepted(*_):
            other = state_tests.PostgresState(self.dsn, self.key, self.namespace)
            pending = other.get("publisher")["destinations"]["-1001"]["pending_delivery"]
            self.assertEqual(pending["server_id"], "a")
            return {"ok": True, "result": {"message_id": 1}}
        with self.db.lease("publisher"), Bindings(self.db), ExitStack() as stack:
            stack.enter_context(patch.object(publisher, "RUN_BUDGET_SECONDS", 10))
            stack.enter_context(patch.object(publisher, "RUN_STOP_RESERVE_SECONDS", 0))
            stack.enter_context(patch.object(publisher.base, "live_validate", side_effect=lambda row: row))
            request = stack.enter_context(patch.object(publisher.base, "_telegram_request_once", side_effect=accepted))
            with patch.object(self.db, "put", side_effect=fail_confirmed_write):
                self.assertEqual(publisher.main(), 1)
            request.assert_called_once()
            self.assertIsNotNone(self.db.get("publisher")["destinations"]["-1001"]["pending_delivery"])
            request.reset_mock()
            self.assertEqual(publisher.main(), 0)
            request.assert_not_called()
        self.assertIn("a", self.db.get("publisher")["destinations"]["-1001"]["uncertain_deliveries"])
