"""Resident bot, journaled publisher and atomic collector generation workers."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import telegram_bot_control as control
import telegram_multi_publisher as publisher
from server_bindings import Bindings
from server_state import LeaseLost, StateError


def bind_bot_identity(state):
    me = control.telegram_api("getMe")
    if not isinstance(me, dict) or not isinstance(me.get("id"), int):
        raise StateError("Telegram bot identity could not be verified")
    with state.transaction():
        state._conn.execute("SELECT pg_advisory_xact_lock(%s)", (state.lock_id("bot-identity"),))
        identity = state.get("bot_identity")
        if identity is not None and identity != {"id": me["id"]}:
            raise StateError("this database belongs to a different Telegram bot")
        state.put("bot_identity", {"id": me["id"]})


def run_bot(state, stop, once=False):
    if state.get("activation_required"):
        raise StateError("reconcile pending Telegram updates with the activate command first")
    bind_bot_identity(state)
    webhook = control.telegram_api("getWebhookInfo")
    if not isinstance(webhook, dict) or webhook.get("url"):
        raise StateError("an existing webhook must be removed before starting polling")
    with state.lease("bot"), Bindings(state, stop=stop) as bindings:
        state.recover_replies()
        control.register_commands()
        backoff = 1
        while not stop.is_set():
            state.pulse("bot")
            try:
                bindings.flush_replies()
                updates = control.telegram_api("getUpdates", {
                    "offset": control.load_bot_state()["last_update_id"] + 1, "timeout": 20,
                    "limit": 100, "allowed_updates": ["message", "callback_query", "my_chat_member"]})
                if not isinstance(updates, list):
                    raise StateError("invalid Telegram update batch")
                for update in updates:
                    if stop.is_set():
                        break
                    bindings.process_update(update)
                    bindings.flush_replies(limit=5)
                backoff = 1
            except control.RemoteHTTPError as exc:
                if exc.status_code in {401, 403, 404, 409}:
                    raise StateError("Telegram credentials or update consumer conflict") from None
                if exc.status_code == 429:
                    backoff = max(1, min(60, exc.retry_after or 30))
                state.pulse("bot", "degraded")
            except (LeaseLost, StateError):
                raise
            except Exception as exc:
                # Exception messages can contain private URLs or database DSNs.
                print(f"[server] bot operation deferred ({type(exc).__name__})", flush=True)
                state.pulse("bot", "degraded")
                backoff = min(30, backoff * 2)
            if once:
                break
            if backoff > 1:
                stop.wait(backoff)


def run_publisher(state, stop, once=False):
    if state.get("activation_required"):
        raise StateError("activate the runtime before publishing")
    bind_bot_identity(state)
    with state.lease("publisher"), Bindings(state, stop=stop) as bindings:
        publisher.RUN_BUDGET_SECONDS = 30
        publisher.RUN_STOP_RESERVE_SECONDS = 0
        publisher.CHECK_INTERVAL_SECONDS = 2
        # Reuse the mature journaled send loop, with interruptible waits.
        original_sleep = publisher.wait_for_next_slot
        def wait(seconds):
            stop.wait(seconds)
            if stop.is_set():
                raise publisher.PublishingDisabled("worker is stopping")
        publisher.wait_for_next_slot = wait
        try:
            while not stop.is_set():
                state.pulse("publisher", "idle")
                enabled = state.get("control", {"enabled": False}).get("enabled") is True
                if enabled and bindings.cooldown_remaining() <= 0:
                    state.pulse("publisher", "working")
                    try:
                        result = publisher.main()
                        state.pulse("publisher", "idle" if result == 0 else "degraded")
                    except (LeaseLost, StateError):
                        raise
                    except Exception as exc:
                        print(f"[server] publisher operation deferred ({type(exc).__name__})", flush=True)
                        state.pulse("publisher", "degraded")
                if once:
                    break
                stop.wait(2)
        finally:
            publisher.wait_for_next_slot = original_sleep


def collect_generation(state, data_dir: Path, source_repo: Path | None = None):
    import generator_managed as managed
    from telegram_managed_sources import decrypt_sources, enabled_sources
    from common import save_json

    repo = source_repo or Path(__file__).resolve().parents[1]
    source_file = repo / "data/sources.json"
    source_config = json.loads(source_file.read_text())
    manual_files = []
    for entry in source_config.get("manual_sources", []):
        if entry.get("enabled") is not True:
            continue
        relative = Path(entry["path"])
        resolved = (repo / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or not resolved.is_relative_to(repo.resolve()) or not resolved.is_file():
            raise StateError("enabled manual source must be an existing relative file inside the repository")
        manual_files.append((relative, resolved))
    data_dir.mkdir(parents=True, exist_ok=True)
    generation = Path(tempfile.mkdtemp(prefix="generation-", dir=data_dir)).resolve()
    (generation / "data").mkdir()
    shutil.copyfile(source_file, generation / "data/sources.json")
    for relative, resolved in manual_files:
        destination = generation / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, destination)
    previous = state.get("collector_snapshot", {}).get("servers", [])
    save_json(str(generation / "data/servers.json"), previous)
    # A source-change request arriving during this run remains pending.
    request_id = state.get("collector_request", {}).get("id", "")
    source_snapshot = enabled_sources(decrypt_sources(state.get("sources", {})))
    original_sources = managed.managed_sources
    original_cwd = Path.cwd()
    committed = False
    try:
        managed.managed_sources = lambda: [dict(row) for row in source_snapshot]
        os.chdir(generation)
        managed.main()
        from generate_online_subscription import write_online_subscription
        write_online_subscription()
        servers = json.loads(Path("data/servers.json").read_text())
        status = json.loads(Path("data/status.json").read_text())
        state.guard()
        # The entire public generation switches together, never half a feed.
        temp_link = data_dir / ".current-next"
        temp_link.unlink(missing_ok=True)
        temp_link.symlink_to(generation.name, target_is_directory=True)
        with state.transaction():
            state.put("collector_snapshot", {"at": time.time(), "servers": servers, "status": status})
            state.put("collector_completed_request", {"id": request_id})
            os.replace(temp_link, data_dir / "current")
        committed = True
    finally:
        os.chdir(original_cwd)
        managed.managed_sources = original_sources
        if not committed:
            # A failed DB commit may follow the symlink switch. Retain that
            # complete generation for readers; the next run reconciles state.
            current = data_dir / "current"
            if not current.is_symlink() or current.resolve() != generation:
                shutil.rmtree(generation)
    current = (data_dir / "current").resolve()
    generations = sorted(data_dir.glob("generation-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in generations[3:]:
        if old.resolve() != current:
            shutil.rmtree(old)


def run_collector(state, stop, data_dir, interval=300, once=False):
    with state.lease("collector"):
        next_run = 0
        last_request = None
        while not stop.is_set():
            state.pulse("collector", "idle")
            requested = state.get("collector_request", {}).get("id", "")
            changed = requested != last_request
            if time.monotonic() >= next_run or changed:
                last_request = requested
                state.pulse("collector", "working")
                try:
                    collect_generation(state, data_dir)
                    state.pulse("collector", "idle")
                    next_run = time.monotonic() + interval
                except (LeaseLost, StateError):
                    raise
                except Exception as exc:
                    print(f"[server] collector deferred ({type(exc).__name__})", flush=True)
                    state.pulse("collector", "degraded")
                    next_run = time.monotonic() + 30
            if once:
                break
            stop.wait(5)
