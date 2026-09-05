"""Read the Telegram publisher ON/OFF control flag.

The control file lives on main at .github/telegram-publisher-control.json.
Any malformed or unreachable control state fails closed (OFF) so an explicit
operator stop is never ignored because of a transient read/parsing problem.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Sequence

CONTROL_PATH = os.environ.get(
    "TELEGRAM_PUBLISHER_CONTROL_PATH",
    ".github/telegram-publisher-control.json",
)


def _parse_enabled(text: str) -> bool:
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
        raise ValueError("control JSON must contain boolean field 'enabled'")
    return bool(data["enabled"])


def _run_git(args: Sequence[str], cwd: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def local_enabled(repo_dir: str = ".") -> bool:
    path = os.path.join(repo_dir, CONTROL_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _parse_enabled(f.read())
    except Exception as exc:
        print(
            f"[telegram-control] local control check failed; failing closed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def remote_enabled(repo_dir: str = ".") -> bool:
    try:
        fetched = _run_git(["fetch", "origin", "main", "--quiet"], repo_dir)
        if fetched.returncode != 0:
            raise RuntimeError(fetched.stderr.strip() or "git fetch failed")

        shown = _run_git(["show", f"origin/main:{CONTROL_PATH}"], repo_dir, timeout=10)
        if shown.returncode != 0:
            raise RuntimeError(shown.stderr.strip() or "git show failed")

        return _parse_enabled(shown.stdout)
    except Exception as exc:
        print(
            f"[telegram-control] remote control check failed; failing closed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def _write_github_output(enabled: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output:
        return
    with open(output, "a", encoding="utf-8") as f:
        f.write(f"enabled={'true' if enabled else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("local", "remote"), default="local")
    parser.add_argument("--repo-dir", default=".")
    args = parser.parse_args()

    enabled = (
        remote_enabled(args.repo_dir)
        if args.source == "remote"
        else local_enabled(args.repo_dir)
    )
    _write_github_output(enabled)
    print(f"[telegram-control] publisher enabled={enabled}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
