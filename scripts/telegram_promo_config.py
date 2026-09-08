"""Durable configuration for the purchase CTA under Telegram config posts."""
from __future__ import annotations

import json
import os
import subprocess
from urllib.parse import urlsplit, urlunsplit

PROMO_PATH = os.environ.get(
    "TELEGRAM_PUBLISHER_PROMO_PATH",
    ".github/telegram-publisher-promo.json",
)
BUTTON_TEXT = "خرید اشتراک پرسرعت و بدون قطعی"
DEFAULT_PROMO_URL = "https://t.me/xbroutebot"
MAX_URL_CHARS = 2048


def normalize_promo_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("purchase URL must not be empty")
    if any(ord(ch) < 32 or ch.isspace() for ch in raw):
        raise ValueError("purchase URL must not contain whitespace/control characters")
    if len(raw) > MAX_URL_CHARS:
        raise ValueError(f"purchase URL exceeds {MAX_URL_CHARS} characters")

    lowered = raw.lower()
    if lowered.startswith("t.me/") or lowered.startswith("www.t.me/"):
        raw = "https://" + raw

    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid purchase URL: {exc}") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("purchase URL must use http:// or https://")
    if not parsed.hostname:
        raise ValueError("purchase URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("purchase URL must not contain username/password credentials")

    # Preserve path/query/fragment exactly while normalizing only the scheme.
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def promo_url_from_data(data: object) -> str:
    if not isinstance(data, dict):
        raise ValueError("promo config must be a JSON object")
    return normalize_promo_url(str(data.get("url") or ""))


def promo_url_from_text(text: str) -> str:
    return promo_url_from_data(json.loads(text))


def local_promo_url(repo_dir: str = ".") -> str:
    path = os.path.join(repo_dir, PROMO_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return promo_url_from_text(f.read())


def _git(args: list[str], cwd: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def remote_promo_url(repo_dir: str = ".") -> str:
    fetched = _git(["fetch", "origin", "main", "--quiet"], repo_dir)
    if fetched.returncode != 0:
        raise RuntimeError(fetched.stderr.strip() or "git fetch failed")

    shown = _git(["show", f"origin/main:{PROMO_PATH}"], repo_dir, timeout=10)
    if shown.returncode != 0:
        raise RuntimeError(shown.stderr.strip() or "git show failed")

    return promo_url_from_text(shown.stdout)
