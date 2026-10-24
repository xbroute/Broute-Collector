import os
import tempfile
import unittest
from pathlib import Path

import test_server_state as state_tests
if state_tests.PostgresState:
    from server_operations import export_archive, restore_archive
    from server_bindings import Bindings


@unittest.skipUnless(state_tests.PostgresState and os.environ.get("TEST_DATABASE_URL"), "requires a real isolated PostgreSQL service")
class ArchiveIntegrationTests(unittest.TestCase):
    setUp = state_tests.PostgresIntegrationTests.setUp
    tearDown = state_tests.PostgresIntegrationTests.tearDown

    def test_archive_restore_retains_history_forces_off_and_holds_unproven_replies(self):
        self.db.put("control", {"enabled": True})
        self.db.put("offset", {"last_update_id": 321})
        self.db.put("publisher", {"version": 1, "destinations": {"-1001": {"sent": ["a"]}}})
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
            with self.assertRaises(state_tests.StateError):
                restore_archive(self.db, archive)

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
