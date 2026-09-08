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
DEFAULT_BUTTON_TEXT = "خرید اشتراک پرسرعت و بدون قطعی"
# Backwards-compatible alias for older imports/tests.
BUTTON_TEXT = DEFAULT_BUTTON_TEXT
DEFAULT_PROMO_URL = "https://t.me/xbroutebot"
MAX_URL_CHARS = 2048
MAX_BUTTON_TEXT_CHARS = 96


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


def normalize_button_text(value: str) -> str:
    """Validate a compact, single-line Telegram purchase-button label."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("purchase button text must not be empty")
    if any(ord(ch) < 32 for ch in text) or "\n" in text or "\r" in text:
        raise ValueError("purchase button text must be a single line")
    if len(text) > MAX_BUTTON_TEXT_CHARS:
        raise ValueError(
            f"purchase button text exceeds {MAX_BUTTON_TEXT_CHARS} characters"
        )
    return text


def promo_from_data(data: object) -> dict[str, str]:
    """Read URL + label with backwards compatibility for old URL-only JSON."""
    if not isinstance(data, dict):
        raise ValueError("promo config must be a JSON object")
    return {
        "url": normalize_promo_url(str(data.get("url") or DEFAULT_PROMO_URL)),
        "text": normalize_button_text(str(data.get("text") or DEFAULT_BUTTON_TEXT)),
    }


def promo_from_text(text: str) -> dict[str, str]:
    return promo_from_data(json.loads(text))


def promo_url_from_data(data: object) -> str:
    return promo_from_data(data)["url"]


def promo_url_from_text(text: str) -> str:
    return promo_from_text(text)["url"]


def promo_button_text_from_data(data: object) -> str:
    return promo_from_data(data)["text"]


def promo_button_text_from_text(text: str) -> str:
    return promo_from_text(text)["text"]


def local_promo(repo_dir: str = ".") -> dict[str, str]:
    path = os.path.join(repo_dir, PROMO_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return promo_from_text(f.read())


def local_promo_url(repo_dir: str = ".") -> str:
    return local_promo(repo_dir)["url"]


def local_button_text(repo_dir: str = ".") -> str:
    return local_promo(repo_dir)["text"]


def _git(args: list[str], cwd: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def remote_promo(repo_dir: str = ".") -> dict[str, str]:
    fetched = _git(["fetch", "origin", "main", "--quiet"], repo_dir)
    if fetched.returncode != 0:
        raise RuntimeError(fetched.stderr.strip() or "git fetch failed")

    shown = _git(["show", f"origin/main:{PROMO_PATH}"], repo_dir, timeout=10)
    if shown.returncode != 0:
        raise RuntimeError(shown.stderr.strip() or "git show failed")

    return promo_from_text(shown.stdout)


def remote_promo_url(repo_dir: str = ".") -> str:
    return remote_promo(repo_dir)["url"]


def remote_button_text(repo_dir: str = ".") -> str:
    return remote_promo(repo_dir)["text"]
