import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    from server_state import PostgresState, StateError, LeaseLost
except ImportError:
    PostgresState = None
from cryptography.fernet import Fernet


@unittest.skipIf(PostgresState is None, "install requirements-server.txt")
class StateEncryptionTests(unittest.TestCase):
    def test_ciphertext_is_authenticated_and_bound_to_document_and_instance(self):
        key = Fernet.generate_key().decode()
        state = PostgresState("", key)
        encrypted = state.encode({"private": "never-log-this"}, "roles")
        self.assertNotIn(b"never-log-this", encrypted)
        self.assertEqual(state.decode(encrypted, "roles"), {"private": "never-log-this"})
        for other, context in [(state, "offset"), (PostgresState("", key, "other"), "roles"),
                               (PostgresState("", Fernet.generate_key().decode()), "roles")]:
            with self.assertRaises(StateError):
                other.decode(encrypted, context)
        with self.assertRaises(StateError):
            state.decode(encrypted[:-5] + b"wrong", "roles")

    def test_invalid_configuration_and_nonfinite_json_are_rejected(self):
        for key, namespace in [("bad-key", "bot"), (Fernet.generate_key().decode(), "../other")]:
            with self.assertRaises(StateError):
                PostgresState("", key, namespace)
        with self.assertRaises(ValueError):
            PostgresState("", Fernet.generate_key().decode()).encode(float("nan"))


@unittest.skipUnless(PostgresState and os.environ.get("TEST_DATABASE_URL"), "requires a real isolated PostgreSQL service")
class PostgresIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["TEST_DATABASE_URL"]
        self.key = Fernet.generate_key().decode()
        self.namespace = "test_" + uuid.uuid4().hex
        self.db = PostgresState(self.dsn, self.key, self.namespace)
        self.db.initialize()

    def tearDown(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM broute_replies WHERE namespace=%s", (self.namespace,))
            conn.execute("DELETE FROM broute_documents WHERE namespace=%s", (self.namespace,))

    def test_command_settings_offset_and_reply_are_atomic(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.put("control", {"enabled": True})
                self.db.put("offset", 123)
                self.db.enqueue_reply({"text": "success"})
                raise RuntimeError("crash before commit")
        self.assertIsNone(self.db.get("control"))
        self.assertIsNone(self.db.get("offset"))
        self.assertIsNone(self.db.next_reply())
        with self.db.transaction():
            self.db.put("control", {"enabled": False})
            self.db.put("offset", 123)
            self.db.enqueue_reply({"text": "stopped"})
        other = PostgresState(self.dsn, self.key, self.namespace)
        self.assertEqual(other.get("offset"), 123)
        self.assertEqual(other.get("control"), {"enabled": False})
        self.assertEqual(other.next_reply()[1], {"text": "stopped"})

    def test_exclusive_role_and_lease_loss_fence_writes(self):
        other = PostgresState(self.dsn, self.key, self.namespace)
        with self.db.lease("publisher"):
            with self.assertRaises(StateError):
                with other.lease("publisher"):
                    self.fail("second publisher acquired lease")
            # Disconnect the lease independently of the application transaction.
            self.db._leases["publisher"].close()
            with self.assertRaises(LeaseLost):
                self.db.put("should_not_write", True)
        with other.lease("publisher"):
            other.put("recovered", True)
        self.assertIsNone(other.get("should_not_write"))

    def test_incomplete_reply_is_held_after_restart(self):
        self.db.enqueue_reply({"text": "private"})
        reply_id, _ = self.db.next_reply()
        self.db.recover_replies()
        self.assertIsNone(self.db.next_reply())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM broute_replies WHERE namespace=%s AND id=%s", (self.namespace, reply_id)).fetchone()[0], "uncertain")

    def test_wrong_key_and_ciphertext_row_swap_fail_closed(self):
        with self.assertRaises(StateError):
            PostgresState(self.dsn, Fernet.generate_key().decode(), self.namespace).initialize()
        self.db.put("roles", {"owner": 1})
        with self.db.connect() as conn:
            conn.execute("INSERT INTO broute_documents(namespace,name,payload) SELECT namespace,'control',payload FROM broute_documents WHERE namespace=%s AND name='roles'", (self.namespace,))
        with self.assertRaises(StateError):
            self.db.get("control")
