import os
import tempfile
import json
import unittest
from unittest.mock import patch
from pathlib import Path

import test_server_state as state_tests
if state_tests.PostgresState:
    from server_operations import export_archive, restore_archive
    from server_bindings import Bindings


@unittest.skipUnless(state_tests.PostgresState and os.environ.get("TEST_DATABASE_URL"), "requires a real isolated PostgreSQL service")
class ArchiveIntegrationTests(unittest.TestCase):
    tearDown = state_tests.PostgresIntegrationTests.tearDown

    def setUp(self):
        state_tests.PostgresIntegrationTests.setUp(self)
        context = patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "isolated-migration-token"})
        context.start()
        self.addCleanup(context.stop)

    def test_archive_restore_retains_history_forces_off_and_holds_unproven_replies(self):
        self.db.put("control", {"enabled": True})
        self.db.put("offset", {"last_update_id": 321})
        self.db.put("publisher", {"version": 1, "destinations": {"-1001": {"sent": ["a"]}}})
        from telegram_destinations import encrypt_store, decrypt_store, normalize_destination
        self.db.put("destinations", encrypt_store({"manager_user_ids": [123], "destinations": [normalize_destination({"chat_id": -1001, "enabled": True})]}))
        self.db.enqueue_reply({"method": "sendMessage", "payload": {"text": "private-value"}, "headers": None, "body": None})
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "runtime.broute"
            export_archive(self.db, archive)
            self.assertNotIn(b"private-value", archive.read_bytes())
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with self.db.connect() as conn:
                conn.execute("DELETE FROM broute_documents WHERE namespace=%s AND name <> 'encryption_check'", (self.namespace,))
                conn.execute("DELETE FROM broute_replies WHERE namespace=%s", (self.namespace,))
            restore_archive(self.db, archive)
            self.assertFalse(self.db.get("control")["enabled"])
            self.assertEqual(self.db.get("offset")["last_update_id"], 321)
            self.assertEqual(self.db.get("publisher")["destinations"]["-1001"]["sent"], ["a"])
            self.assertIsNone(self.db.next_reply())
            self.assertEqual(self.db.get("activation_required")["reason"], "backup restore")
            restored = decrypt_store(self.db.get("destinations"))
            self.assertFalse(restored["destinations"][0]["enabled"])
            self.assertEqual(restored["administration"]["confirmations"], {})
            with self.assertRaises(state_tests.StateError):
                restore_archive(self.db, archive)

    def test_activation_discard_is_explicit_and_does_not_turn_publishing_on(self):
        from server_operations import activate
        from unittest.mock import patch
        import telegram_bot_control as control
        with patch.object(control, "telegram_api", return_value=[{"update_id": 999}]) as request:
            activate(self.db, "discard")
        self.assertEqual(request.call_args.args[1]["offset"], -1)
        self.assertEqual(self.db.get("offset"), {"last_update_id": 999})
        self.assertIsNone(self.db.get("activation_required"))
        self.assertFalse(self.db.get("control")["enabled"])

    def test_corrupt_backup_and_restore_while_a_worker_runs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "runtime.broute"
            export_archive(self.db, archive)
            with self.db.lease("publisher"):
                other = state_tests.PostgresState(self.dsn, self.key, self.namespace)
                with self.assertRaises(state_tests.StateError):
                    restore_archive(other, archive)
            archive.write_bytes(archive.read_bytes()[:-10] + b"corruption")
            with self.assertRaises(state_tests.StateError):
                restore_archive(self.db, archive)

    def test_git_migration_preserves_history_and_bad_input_writes_nothing(self):
        from server_operations import import_git_state
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, bot = root / "repo", root / "bot"
            (repo / ".github").mkdir(parents=True)
            bot.mkdir()
            (repo / ".github/telegram-publisher-control.json").write_text(json.dumps({"enabled": True}))
            (bot / "telegram_bot_control_state.json").write_text(json.dumps({"last_update_id": 555}))
            history = root / "history.json"
            history.write_text(json.dumps({"version": 1, "destinations": []}))
            with self.assertRaises(RuntimeError):
                import_git_state(self.db, repo, bot, history)
            self.assertIsNone(self.db.get("offset"))
            history.write_text(json.dumps({"version": 1, "destinations": {"-1001": {"sent": ["preserved"]}}}))
            import_git_state(self.db, repo, bot, history)
            self.assertEqual(self.db.get("offset")["last_update_id"], 555)
            self.assertEqual(self.db.get("publisher")["destinations"]["-1001"]["sent"], ["preserved"])
            self.assertFalse(self.db.get("control")["enabled"])
            self.assertTrue(self.db.get("activation_required"))
