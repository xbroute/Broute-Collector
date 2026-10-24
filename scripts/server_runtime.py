"""Opt-in self-hosted runtime. This module never deploys or changes GitHub."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from server_state import PostgresState
from server_operations import activate, export_archive, health, import_git_state, restore_archive, run_backups


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    activation = commands.add_parser("activate")
    activation.add_argument("--pending-updates", choices=("keep", "discard"), required=True,
                            help="keep replays saved pending commands; discard explicitly forgets the Telegram backlog")
    run = commands.add_parser("run")
    run.add_argument("role", choices=("bot", "publisher", "collector", "backup"))
    run.add_argument("--once", action="store_true")
    check = commands.add_parser("health")
    check.add_argument("role", choices=("bot", "publisher", "collector", "backup"))
    migrate = commands.add_parser("import-git")
    migrate.add_argument("--repo", type=Path, required=True)
    migrate.add_argument("--bot-state", type=Path, required=True)
    migrate.add_argument("--publisher-state", type=Path, required=True)
    for action in ("export", "restore"):
        item = commands.add_parser(action)
        item.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        state = PostgresState.from_env()
        state.initialize()
        if args.command == "init":
            print("Database initialized; publisher OFF; explicit activation required before polling.")
        elif args.command == "activate":
            activate(state, args.pending_updates)
            print("Runtime activated; publisher remains OFF.")
        elif args.command == "import-git":
            import_git_state(state, args.repo.resolve(), args.bot_state.resolve(), args.publisher_state.resolve())
            print("State imported transactionally; publisher is OFF.")
        elif args.command == "export":
            export_archive(state, args.file)
            print("Encrypted runtime backup written.")
        elif args.command == "restore":
            restore_archive(state, args.file)
            print("Runtime backup restored; publisher is OFF; unsent replies held for review.")
        elif args.command == "health":
            status = health(state, args.role)
            print(json.dumps(status))
            return 0 if status["healthy"] else 1
        elif args.command == "run":
            import telegram_bot_control as control
            from server_workers import run_bot, run_collector, run_publisher
            stop = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            if args.role in {"bot", "publisher"} and not control.BOT_TOKEN:
                raise RuntimeError("Telegram token missing")
            if args.role == "bot":
                run_bot(state, stop, args.once)
            elif args.role == "publisher":
                run_publisher(state, stop, args.once)
            elif args.role == "collector":
                interval = int(os.environ.get("BROUTE_COLLECTION_INTERVAL", "300"))
                if not 60 <= interval <= 86400:
                    raise ValueError("invalid collection interval")
                run_collector(state, stop, Path(os.environ.get("BROUTE_DATA_DIR", "runtime-data")).resolve(), interval, args.once)
            else:
                run_backups(state, stop, Path(os.environ.get("BROUTE_BACKUP_DIR", "runtime-backups")).resolve(), args.once)
        return 0
    except Exception as exc:
        # Never print a DSN, bot token, private URL, or raw Telegram request.
        print(f"[server] operation failed ({type(exc).__name__}); verify configuration, database and worker health.", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
