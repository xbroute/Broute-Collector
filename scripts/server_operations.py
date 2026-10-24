"""Offline migration, authenticated full backup and runtime health operations."""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from server_bindings import DOCUMENTS, validate_document
from server_state import StateError

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def offline(state):
    stack = ExitStack()
    try:
        for role in ("bot", "publisher", "collector", "backup"):
            stack.enter_context(state.lease(role))
        return stack
    except Exception:
        stack.close()
        raise


def require_empty(state):
    rows = state._conn.execute("SELECT name FROM broute_documents WHERE namespace=%s AND name NOT IN ('encryption_check','activation_required')", (state.namespace,)).fetchall()
    replies = state._conn.execute("SELECT 1 FROM broute_replies WHERE namespace=%s LIMIT 1", (state.namespace,)).fetchone()
    if rows or replies:
        raise StateError("import/restore requires an empty runtime namespace; export the current database first")


def read_object(path, optional=False):
    if optional and not path.exists():
        return {}
    if path.stat().st_size > 32 * 1024 * 1024:
        raise StateError("migration input exceeds size limit")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise StateError("migration input must contain a JSON object")
    return value


def import_git_state(state, repo: Path, bot_state: Path, publisher_file: Path):
    # Parse and decrypt EVERYTHING before the first write. Missing operational
    # history is never treated as an empty fresh installation.
    values = {}
    for (branch, path), name in DOCUMENTS.items():
        source = publisher_file if name == "publisher" else (bot_state if branch == "telegram-bot-state" else repo) / path
        values[name] = validate_document(name, read_object(source, optional=name in {"destinations", "sources", "promo"}))
    if values["publisher"].get("sent") and not values["publisher"].get("destinations"):
        raise StateError("legacy single-destination history requires an explicit conversion; refusing to discard it")
    with offline(state), state.transaction():
        require_empty(state)
        for name, value in values.items():
            state.put(name, value)
        state.put("control", {"enabled": False})
        state.put("activation_required", {"reason": "Git migration", "at": time.time()})


def export_archive(state, destination: Path):
    with state.transaction():
        state._conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        documents = {name: state.decode(payload, "doc:" + name) for name, payload in
                     state._conn.execute("SELECT name,payload FROM broute_documents WHERE namespace=%s", (state.namespace,))}
        replies = [{"id": str(key), "value": state.decode(payload, "reply"), "status": status} for key, payload, status in
                   state._conn.execute("SELECT id,payload,status FROM broute_replies WHERE namespace=%s", (state.namespace,))]
    archive = state.encode({"version": 1, "at": time.time(), "documents": documents, "replies": replies}, "archive")
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise StateError("backup exceeds size limit; use an encrypted pg_dump instead")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=destination.parent, prefix=".backup-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(archive)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def restore_archive(state, source: Path):
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise StateError("backup exceeds size limit")
    archive = state.decode(source.read_bytes(), "archive")
    if not isinstance(archive, dict) or archive.get("version") != 1 or not isinstance(archive.get("documents"), dict) or not isinstance(archive.get("replies"), list):
        raise StateError("invalid runtime backup")
    for name, value in archive["documents"].items():
        if name in DOCUMENTS.values():
            validate_document(name, value)
    for reply in archive["replies"]:
        uuid.UUID(reply["id"])
        if reply["status"] not in {"pending", "sending", "sent", "uncertain", "failed"} or not isinstance(reply["value"], dict):
            raise StateError("invalid reply backup")
    with offline(state), state.transaction():
        require_empty(state)
        for name, value in archive["documents"].items():
            state.put(name, value)
        state.put("control", {"enabled": False})
        # Old prompts/approvals and destination ON flags are not fresh consent.
        if "destinations" in archive["documents"]:
            from telegram_destinations import decrypt_store, encrypt_store
            from telegram_admin_policy import administration
            destinations = decrypt_store(archive["documents"]["destinations"])
            destinations["administration"] = administration(destinations)
            destinations["administration"]["sessions"] = {}
            destinations["administration"]["confirmations"] = {}
            for destination in destinations["destinations"]:
                destination["enabled"] = False
            state.put("destinations", encrypt_store(destinations))
        state.put("activation_required", {"reason": "backup restore", "at": time.time()})
        for reply in archive["replies"]:
            # An older backup cannot prove whether a pending reply was sent
            # later. Restoring never replays these external side effects.
            status = reply["status"] if reply["status"] in {"sent", "failed"} else "uncertain"
            state._conn.execute("INSERT INTO broute_replies(namespace,id,payload,status) VALUES (%s,%s,%s,%s)",
                                (state.namespace, uuid.UUID(reply["id"]), state.encode(reply["value"], "reply"), status))


def activate(state, pending_updates):
    import telegram_bot_control as control
    with offline(state), state.transaction():
        if pending_updates == "discard":
            # Explicit operator choice. A negative offset forgets older updates;
            # no update payload or private command content is logged.
            batch = control.telegram_api("getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
            if not isinstance(batch, list):
                raise StateError("invalid Telegram update batch")
            offset = state.get("offset", {"last_update_id": 0})["last_update_id"]
            for update in batch:
                if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                    raise StateError("invalid Telegram update identity")
                offset = max(offset, update["update_id"])
            state.put("offset", {"last_update_id": offset})
        state.put("control", {"enabled": False})
        state.put("activation_required", None)


def health(state, role):
    thresholds = {"bot": 120, "publisher": 90, "collector": 900, "backup": 90000}
    pulse = state.get(f"heartbeat:{role}", {})
    age = time.time() - float(pulse.get("at", 0))
    ready = 0 <= age < thresholds[role] and pulse.get("status") in {"ready", "idle", "working"}
    return {"role": role, "healthy": ready, "age_seconds": round(age), "status": pulse.get("status", "missing")}


def run_backups(state, stop, directory: Path, once=False):
    with state.lease("backup"):
        next_backup = 0
        while not stop.is_set():
            if time.monotonic() >= next_backup:
                name = "runtime-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".broute"
                export_archive(state, directory / name)
                state.pulse("backup", "ready")
                for old in sorted(directory.glob("runtime-*.broute"), reverse=True)[7:]:
                    old.unlink()
                next_backup = time.monotonic() + 86400
            if once:
                break
            stop.wait(60)
