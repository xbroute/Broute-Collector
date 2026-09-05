"""Publish newly discovered online configs to a Telegram forum topic.

Required environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    TELEGRAM_TOPIC_ID

Only configs that are online, valid and not marked for removal are published.
Each config is sent once and each Telegram message contains exactly one config.
Previously published messages are intentionally left untouched if a config later
becomes unavailable.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import country_flag, load_json, rename_raw_config, save_json

SERVERS_PATH = "data/servers.json"
STATE_PATH = "data/telegram_state.json"
BRAND_NAME = "@xbroute"

# Telegram's official bot FAQ recommends staying below 20 messages/minute in a
# group. 3.2 seconds keeps us just under that ceiling, and the per-run cap keeps
# a 5-minute GitHub Actions schedule from piling up overlapping jobs.
SEND_DELAY_SECONDS = 3.2
MAX_MESSAGES_PER_RUN = 80
MAX_TELEGRAM_TEXT_LENGTH = 4096
MAX_RATE_LIMIT_RETRIES = 3

PROTOCOL_LABELS = {
    "vless": "VLESS",
    "vmess": "VMess",
    "trojan": "Trojan",
    "shadowsocks": "Shadowsocks",
    "hysteria2": "Hysteria2",
    "tuic": "TUIC",
    "wireguard": "WireGuard",
    "socks": "SOCKS",
    "http": "HTTP",
}

TRANSPORT_LABELS = {
    "ws": "WebSocket",
    "websocket": "WebSocket",
    "grpc": "gRPC",
    "tcp": "TCP",
    "http": "HTTP",
    "httpupgrade": "HTTP Upgrade",
    "xhttp": "XHTTP",
    "quic": "QUIC",
    "kcp": "mKCP",
    "h2": "HTTP/2",
}


def eligible(server: Dict) -> bool:
    return (
        server.get("status") == "online"
        and server.get("valid") is True
        and not server.get("should_remove", False)
        and bool(server.get("raw"))
        and bool(server.get("id"))
    )


def security_label(server: Dict) -> str:
    security = str(server.get("security") or "none").lower()
    if security == "reality":
        return "🛡 امنیت: Reality"
    if security == "tls" or server.get("tls") is True:
        return "🔐 امنیت: TLS"
    return "⚠️ امنیت: بدون TLS"


def build_message(server: Dict) -> str:
    flag = country_flag(str(server.get("country") or ""))
    country = str(server.get("country_name") or "Unknown")
    country_prefix = flag or "🌍"

    protocol = str(server.get("protocol") or "Unknown").lower()
    protocol_text = PROTOCOL_LABELS.get(protocol, protocol.upper())

    transport = str(server.get("transport") or "tcp").lower()
    transport_text = TRANSPORT_LABELS.get(transport, transport or "Unknown")

    latency = server.get("latency")
    latency_text = f"{latency}ms" if latency is not None else "نامشخص"

    branded_config = rename_raw_config(
        str(server.get("raw") or ""),
        protocol,
        BRAND_NAME,
    )

    message = "\n".join([
        "🟢 کانفیگ رایگان",
        "",
        f"{country_prefix} کشور: {country}",
        f"🔹 پروتکل: {protocol_text}",
        security_label(server),
        f"🔌 شبکه: {transport_text}",
        f"⚡ پینگ: {latency_text}",
        "",
        branded_config,
        "",
        f"🔗 {BRAND_NAME}",
    ])

    if len(message) > MAX_TELEGRAM_TEXT_LENGTH:
        raise ValueError(
            f"message exceeds Telegram's {MAX_TELEGRAM_TEXT_LENGTH}-character limit"
        )

    return message


def load_sent_ids() -> Set[str]:
    state = load_json(STATE_PATH, {"sent": []})
    if isinstance(state, dict):
        sent = state.get("sent", [])
    elif isinstance(state, list):
        sent = state
    else:
        sent = []
    return {str(item) for item in sent if item}


def save_sent_ids(sent_ids: Set[str]) -> None:
    save_json(STATE_PATH, {"sent": sorted(sent_ids)})


def _telegram_request(token: str, payload: Dict) -> Dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        retry_after = (
            data.get("parameters", {}).get("retry_after")
            if isinstance(data, dict)
            else None
        )
        if exc.code == 429 and retry_after is not None:
            raise RateLimited(int(retry_after)) from exc

        raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Telegram connection error: {exc}") from exc


class RateLimited(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__(f"Telegram rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


def send_message(token: str, chat_id: str, topic_id: int, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "message_thread_id": topic_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            result = _telegram_request(token, payload)
        except RateLimited as exc:
            if attempt >= MAX_RATE_LIMIT_RETRIES:
                raise
            sleep_for = max(exc.retry_after, 1) + 1
            print(f"[telegram] rate limited; sleeping {sleep_for}s before retry")
            time.sleep(sleep_for)
            continue

        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    topic_raw = os.environ.get("TELEGRAM_TOPIC_ID", "").strip()

    if not token or not chat_id or not topic_raw:
        print(
            "[telegram] skipped: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and "
            "TELEGRAM_TOPIC_ID must all be configured."
        )
        return 0

    try:
        topic_id = int(topic_raw)
    except ValueError:
        print("[telegram] TELEGRAM_TOPIC_ID must be an integer.", file=sys.stderr)
        return 1

    servers: List[Dict] = load_json(SERVERS_PATH, [])
    if not isinstance(servers, list):
        print("[telegram] servers.json is not a list.", file=sys.stderr)
        return 1

    sent_ids = load_sent_ids()
    pending = [
        server
        for server in servers
        if eligible(server) and str(server["id"]) not in sent_ids
    ]
    pending = pending[:MAX_MESSAGES_PER_RUN]

    if not pending:
        print("[telegram] no new online configs to publish.")
        return 0

    published = 0
    for index, server in enumerate(pending):
        server_id = str(server["id"])
        try:
            message = build_message(server)
            send_message(token, chat_id, topic_id, message)
        except Exception as exc:
            # Successful sends are persisted immediately in the local state file.
            # The workflow keeps going after this step so the state can still be
            # committed, preventing duplicate sends on the next scheduled run.
            save_sent_ids(sent_ids)
            print(f"[telegram] failed for {server_id}: {exc}", file=sys.stderr)
            return 1

        sent_ids.add(server_id)
        save_sent_ids(sent_ids)
        published += 1
        print(f"[telegram] published {server_id}")

        if index < len(pending) - 1:
            time.sleep(SEND_DELAY_SECONDS)

    print(f"[telegram] done. {published} new configs published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
