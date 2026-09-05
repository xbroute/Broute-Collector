"""Telegram presentation helpers for copyable VPN configs.

Bot API CopyTextButton is the strongest one-tap clipboard primitive, but Telegram
limits its payload to 256 characters. We therefore always render the complete
config as a preformatted code block and add the native copy button only when the
whole, exact config fits the documented limit. Never truncate or split a config.
"""
from __future__ import annotations

import html
from typing import Any, Dict

COPY_TEXT_MAX_CHARS = 256


class CopyableTelegramMessage(str):
    """A string carrying the exact raw config used for Telegram reply markup."""

    copy_text: str

    def __new__(cls, value: str, copy_text: str):
        obj = str.__new__(cls, value)
        obj.copy_text = copy_text
        return obj


def make_copyable_message(plain_message: str, config: str) -> CopyableTelegramMessage:
    """Convert the config section of a plain publisher message to HTML <pre>.

    The publisher's visible-text length validation happens before this function.
    HTML escaping changes the wire representation but not the rendered text.
    """
    if not config:
        raise ValueError("copyable Telegram config must not be empty")

    marker = f"\n\n{config}\n\n"
    if plain_message.count(marker) != 1:
        raise ValueError("could not locate the exact config section in Telegram message")

    before, after = plain_message.split(marker, 1)
    rendered = (
        f"{html.escape(before, quote=False)}\n\n"
        f"<pre>{html.escape(config, quote=False)}</pre>\n\n"
        f"{html.escape(after, quote=False)}"
    )
    return CopyableTelegramMessage(rendered, config)


def native_copy_markup(config: str) -> Dict[str, Any] | None:
    """Return Telegram's native clipboard button when the exact config fits."""
    if not (1 <= len(config) <= COPY_TEXT_MAX_CHARS):
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📋 کپی کانفیگ",
                    "copy_text": {"text": config},
                }
            ]
        ]
    }


def decorate_send_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Attach HTML parsing and optional native copy button to publisher payload."""
    text = payload.get("text")
    if not isinstance(text, CopyableTelegramMessage):
        return payload

    decorated = dict(payload)
    decorated["text"] = str(text)
    decorated["parse_mode"] = "HTML"

    markup = native_copy_markup(text.copy_text)
    if markup is not None:
        decorated["reply_markup"] = markup

    return decorated
