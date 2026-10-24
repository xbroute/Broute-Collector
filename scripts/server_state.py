"""Encrypted PostgreSQL state for the opt-in, single-host server runtime.

Existing domain schemas remain intact. Configuration, consumed update offsets,
and private reply outbox entries can commit together; publisher checkpoints
commit independently before/after Telegram. No Git or network fallback exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager

import psycopg
from cryptography.fernet import Fernet

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
SCHEMA_VERSION = 1


class StateError(RuntimeError):
    pass


class LeaseLost(StateError):
    pass


class PostgresState:
    def __init__(self, dsn: str, key: str, namespace: str = "broute"):
        if not namespace or len(namespace) > 64 or not all(c.isalnum() or c in "_-" for c in namespace):
            raise StateError("invalid runtime namespace")
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise StateError("BROUTE_STATE_KEY must be a Fernet key") from None
        self.dsn, self.namespace = dsn, namespace
        self._conn = None
        self._leases = {}

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("DATABASE_URL", ""), os.environ.get("BROUTE_STATE_KEY", ""),
                   os.environ.get("BROUTE_INSTANCE", "broute"))

    def connect(self):
        return psycopg.connect(self.dsn, connect_timeout=5, autocommit=True,
                               options="-c statement_timeout=10000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=90000")

    def initialize(self):
        with self.connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(76328401)")
            conn.execute("CREATE TABLE IF NOT EXISTS broute_schema (version integer PRIMARY KEY)")
            versions = conn.execute("SELECT version FROM broute_schema").fetchall()
            if versions and versions != [(SCHEMA_VERSION,)]:
                raise StateError("unsupported database schema version")
            conn.execute("CREATE TABLE IF NOT EXISTS broute_documents (namespace text NOT NULL, name text NOT NULL, payload bytea NOT NULL, revision bigint NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(namespace,name))")
            conn.execute("CREATE TABLE IF NOT EXISTS broute_replies (namespace text NOT NULL, id uuid NOT NULL, payload bytea NOT NULL, status text NOT NULL CHECK(status IN ('pending','sending','sent','uncertain','failed')), created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(namespace,id))")
            conn.execute("CREATE INDEX IF NOT EXISTS broute_replies_pending ON broute_replies(namespace,created_at) WHERE status='pending'")
            if not versions:
                conn.execute("INSERT INTO broute_schema VALUES (%s)", (SCHEMA_VERSION,))
        # A known encrypted sentinel catches a wrong key before polling/sending.
        with self.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (self.lock_id("initialize"),))
            sentinel = self.get("encryption_check")
            if sentinel is None:
                if self._conn.execute("SELECT 1 FROM broute_documents WHERE namespace=%s LIMIT 1", (self.namespace,)).fetchone():
                    raise StateError("database encryption sentinel missing")
                self.put("encryption_check", {"version": 1})
            elif sentinel != {"version": 1}:
                raise StateError("invalid database encryption sentinel")

    def encode(self, value, context="backup"):
        raw = json.dumps({"namespace": self.namespace, "context": context, "value": value}, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise StateError("runtime document exceeds size limit")
        return self.cipher.encrypt(raw)

    def decode(self, raw, context="backup"):
        try:
            envelope = json.loads(self.cipher.decrypt(bytes(raw)))
            if envelope["namespace"] != self.namespace or envelope["context"] != context:
                raise ValueError
            return envelope["value"]
        except Exception:
            raise StateError("runtime state cannot be authenticated or decoded") from None

    def guard(self):
        for conn in self._leases.values():
            try:
                conn.execute("SELECT 1")
            except Exception:
                raise LeaseLost("runtime lease connection lost; restart required") from None

    @contextmanager
    def transaction(self):
        self.guard()
        if self._conn is not None:
            yield self
            return
        with self.connect() as conn, conn.transaction():
            self._conn = conn
            try:
                yield self
                self.guard()
            finally:
                self._conn = None

    @contextmanager
    def lease(self, role):
        with self.connect() as conn:
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_id(role),)).fetchone()[0]:
                raise StateError(f"another {role} process is already running")
            self._leases[role] = conn
            try:
                yield
            finally:
                self._leases.pop(role, None)
                # Closing releases the session lock, including after failure.

    def lock_id(self, role):
        return int.from_bytes(hashlib.sha256(f"broute:{self.namespace}:{role}".encode()).digest()[:8], "big", signed=True)

    def get(self, name, default=None):
        with self.transaction():
            row = self._conn.execute("SELECT payload FROM broute_documents WHERE namespace=%s AND name=%s", (self.namespace, name)).fetchone()
            return self.decode(row[0], "doc:" + name) if row else default

    def put(self, name, value):
        payload = self.encode(value, "doc:" + name)
        with self.transaction():
            self._conn.execute("INSERT INTO broute_documents(namespace,name,payload) VALUES (%s,%s,%s) ON CONFLICT(namespace,name) DO UPDATE SET payload=EXCLUDED.payload, revision=broute_documents.revision+1, updated_at=now()", (self.namespace, name, payload))

    def enqueue_reply(self, request):
        with self.transaction():
            count = self._conn.execute("SELECT count(*) FROM broute_replies WHERE namespace=%s AND status='pending'", (self.namespace,)).fetchone()[0]
            if count >= 1000:
                raise StateError("private reply outbox is full")
            self._conn.execute("INSERT INTO broute_replies(namespace,id,payload,status) VALUES (%s,%s,%s,'pending')", (self.namespace, uuid.uuid4(), self.encode(request, "reply")))

    def recover_replies(self):
        # Like config sends, a response lost across a crash is not retried.
        with self.transaction():
            self._conn.execute("UPDATE broute_replies SET status='uncertain' WHERE namespace=%s AND status='sending'", (self.namespace,))

    def next_reply(self):
        with self.transaction():
            row = self._conn.execute("SELECT id,payload FROM broute_replies WHERE namespace=%s AND status='pending' ORDER BY created_at,id LIMIT 1 FOR UPDATE", (self.namespace,)).fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE broute_replies SET status='sending' WHERE namespace=%s AND id=%s", (self.namespace, row[0]))
            return row[0], self.decode(row[1], "reply")

    def finish_reply(self, reply_id, status):
        with self.transaction():
            self._conn.execute("UPDATE broute_replies SET status=%s WHERE namespace=%s AND id=%s", (status, self.namespace, reply_id))
            self._conn.execute("DELETE FROM broute_replies WHERE namespace=%s AND status IN ('sent','failed') AND created_at < now() - interval '7 days'", (self.namespace,))

    def pulse(self, role, status="ready"):
        import time
        self.put(f"heartbeat:{role}", {"at": time.time(), "status": status})
