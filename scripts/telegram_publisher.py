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
from pathlib import Path
from typing import Dict, List, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import country_flag, load_json, rename_raw_config, save_json

SERVERS_PATH = "data/servers.json"
STATE_PATH = "data/telegram_state.json"
BRAND_NAME = "@xbroute"
SEND_DELAY_SECONDS = 0.12

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
    country_text = f"{flag} {country}".strip()

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

    return "\n".join([
        "🟢 کانفیگ رایگان",
        "",
        f"🌍 کشور: {country_text}",
        f"🔹 پروتکل: {protocol_text}",
        security_label(server),
        f"🔌 شبکه: {transport_text}",
        f"⚡ پینگ: {latency_text}",
        "",
        branded_config,
        "",
        f"🔗 {BRAND_NAME}",
    ])


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


def send_message(token: str, chat_id: str, topic_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "message_thread_id": topic_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Telegram connection error: {exc}") from exc

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


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
    pending = [server for server in servers if eligible(server) and str(server["id"]) not in sent_ids]

    if not pending:
        print("[telegram] no new online configs to publish.")
        return 0

    published = 0
    for server in pending:
        server_id = str(server["id"])
        try:
            send_message(token, chat_id, topic_id, build_message(server))
        except Exception as exc:
            # Persist every successful send immediately. This prevents duplicates if a
            # later message fails and the GitHub Actions job is retried.
            save_sent_ids(sent_ids)
            print(f"[telegram] failed for {server_id}: {exc}", file=sys.stderr)
            return 1

        sent_ids.add(server_id)
        save_sent_ids(sent_ids)
        published += 1
        print(f"[telegram] published {server_id}")
        time.sleep(SEND_DELAY_SECONDS)

    print(f"[telegram] done. {published} new configs published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
