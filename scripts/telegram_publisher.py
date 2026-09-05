"""Publish online configs to a Telegram forum topic using a persistent queue.

Required environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    TELEGRAM_TOPIC_ID

Optional environment variable:
    TELEGRAM_STATE_PATH

The queue is persistent and keeps its order across collector refreshes. New online
configs are appended to the end of the queue. Queued configs that are no longer
online in the latest servers.json snapshot are dropped before publishing and may
be queued again later if they become online again. Already-sent configs are never
sent twice, and old Telegram messages are intentionally left untouched if a config
later becomes unavailable.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from typing import Dict, List, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import country_flag, load_json, rename_raw_config, save_json

SERVERS_PATH = "data/servers.json"
STATE_PATH = os.environ.get("TELEGRAM_STATE_PATH", "data/telegram_state.json")
BRAND_NAME = "@xbroute"

MIN_SEND_DELAY_SECONDS = 30
MAX_SEND_DELAY_SECONDS = 90
# Keep one publisher run alive long enough that the next 5-minute scheduled run is
# already pending. The reserve lets us hand state back safely before Actions timeout.
RUN_BUDGET_SECONDS = 9 * 60
RUN_STOP_RESERVE_SECONDS = 60
MAX_TELEGRAM_TEXT_LENGTH = 4096
GIT_REFRESH_TIMEOUT_SECONDS = 30

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


def brand_raw_config(raw: str, protocol: str) -> str:
    """Rebrand a config while keeping literal #@xbroute for URI protocols."""
    if protocol == "vmess":
        # VMess keeps its display name inside the Base64 JSON payload as `ps`.
        return rename_raw_config(raw, protocol, BRAND_NAME)

    base = raw.split("#", 1)[0]
    return f"{base}#{BRAND_NAME}"


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

    branded_config = brand_raw_config(str(server.get("raw") or ""), protocol)

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


def publishable(server: Dict) -> bool:
    """Return True only for configs that can actually fit in one Telegram message."""
    if not eligible(server):
        return False
    try:
        build_message(server)
    except ValueError:
        return False
    return True


def load_state() -> Tuple[Set[str], List[str], float]:
    raw_state = load_json(
        STATE_PATH,
        {"sent": [], "queue": [], "next_send_after": 0},
    )

    # Backward compatibility with the first implementation where state could be
    # either a plain list or a dict containing only `sent`.
    if isinstance(raw_state, list):
        sent_raw = raw_state
        queue_raw = []
        next_send_after = 0.0
    elif isinstance(raw_state, dict):
        sent_raw = raw_state.get("sent", [])
        queue_raw = raw_state.get("queue", [])
        try:
            next_send_after = float(raw_state.get("next_send_after", 0) or 0)
        except (TypeError, ValueError):
            next_send_after = 0.0
    else:
        sent_raw = []
        queue_raw = []
        next_send_after = 0.0

    sent_ids = {str(item) for item in sent_raw if item}

    queue: List[str] = []
    seen: Set[str] = set()
    for item in queue_raw:
        server_id = str(item)
        if not server_id or server_id in seen or server_id in sent_ids:
            continue
        seen.add(server_id)
        queue.append(server_id)

    return sent_ids, queue, next_send_after


def save_state(sent_ids: Set[str], queue: List[str], next_send_after: float) -> None:
    save_json(
        STATE_PATH,
        {
            "sent": sorted(sent_ids),
            "queue": queue,
            "next_send_after": int(next_send_after),
        },
    )


def sync_queue(
    servers: List[Dict],
    sent_ids: Set[str],
    queue: List[str],
) -> Tuple[Dict[str, Dict], List[str], int, int]:
    """Reconcile the persistent queue with the latest validated online snapshot.

    Existing queue order is preserved. Entries that are no longer online or cannot
    fit in one Telegram message are removed. Newly discovered online configs are
    appended at the end.
    """
    online_by_id: Dict[str, Dict] = {}
    ordered_online_ids: List[str] = []

    for server in servers:
        if not publishable(server):
            continue
        server_id = str(server["id"])
        if server_id not in online_by_id:
            online_by_id[server_id] = server
            ordered_online_ids.append(server_id)

    clean_queue: List[str] = []
    queued_ids: Set[str] = set()
    removed_stale = 0

    for server_id in queue:
        if server_id in sent_ids:
            continue
        if server_id not in online_by_id:
            removed_stale += 1
            continue
        if server_id in queued_ids:
            continue
        clean_queue.append(server_id)
        queued_ids.add(server_id)

    added_new = 0
    for server_id in ordered_online_ids:
        if server_id in sent_ids or server_id in queued_ids:
            continue
        clean_queue.append(server_id)
        queued_ids.add(server_id)
        added_new += 1

    return online_by_id, clean_queue, added_new, removed_stale


def load_latest_servers_from_main() -> List[Dict]:
    """Fetch main and read its newest data/servers.json without changing checkout.

    The publisher may run for several minutes while the collector updates main every
    five minutes. Reading FETCH_HEAD immediately after fetching main prevents a
    config that became offline mid-run from being published from an old snapshot.
    """
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main", "--quiet"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_REFRESH_TIMEOUT_SECONDS,
        )
        shown = subprocess.run(
            ["git", "show", "FETCH_HEAD:data/servers.json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_REFRESH_TIMEOUT_SECONDS,
        )
        servers = json.loads(shown.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not refresh latest main servers.json: {exc}") from exc

    if not isinstance(servers, list):
        raise RuntimeError("latest main data/servers.json is not a list")
    return servers


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
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }

    result = _telegram_request(token, payload)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def wait_until_next_slot(next_send_after: float) -> None:
    now = time.time()
    if next_send_after <= now:
        return

    sleep_for = next_send_after - now
    print(f"[telegram] pacing delay: sleeping {sleep_for:.1f}s")
    time.sleep(sleep_for)


def find_publishable_server(servers: List[Dict], server_id: str) -> Dict | None:
    for server in servers:
        if str(server.get("id") or "") == server_id and publishable(server):
            return server
    return None


def enough_run_budget(next_send_after: float, deadline_monotonic: float) -> bool:
    wait_needed = max(0.0, next_send_after - time.time())
    remaining = deadline_monotonic - time.monotonic()
    return remaining >= wait_needed + RUN_STOP_RESERVE_SECONDS


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    topic_raw = os.environ.get("TELEGRAM_TOPIC_ID", "").strip()

    servers: List[Dict] = load_json(SERVERS_PATH, [])
    if not isinstance(servers, list):
        print("[telegram] servers.json is not a list.", file=sys.stderr)
        return 1

    sent_ids, queue, next_send_after = load_state()
    _, queue, added_new, removed_stale = sync_queue(
        servers,
        sent_ids,
        queue,
    )

    # Persist queue reconciliation even when the bot token has not been configured
    # yet. When the token is added later, publishing starts from a clean queue of
    # configs that are still online at that time.
    save_state(sent_ids, queue, next_send_after)
    print(
        f"[telegram] queue synced: {len(queue)} pending, "
        f"{added_new} added, {removed_stale} stale removed, "
        f"{len(sent_ids)} already sent."
    )

    if not token or not chat_id or not topic_raw:
        print(
            "[telegram] publishing skipped: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID "
            "and TELEGRAM_TOPIC_ID must all be configured."
        )
        return 0

    try:
        topic_id = int(topic_raw)
    except ValueError:
        print("[telegram] TELEGRAM_TOPIC_ID must be an integer.", file=sys.stderr)
        return 1

    if not queue:
        print("[telegram] no online configs are waiting in the queue.")
        return 0

    published = 0
    deadline_monotonic = time.monotonic() + RUN_BUDGET_SECONDS

    while queue:
        if not enough_run_budget(next_send_after, deadline_monotonic):
            print(
                f"[telegram] handing off with {len(queue)} queued; "
                "next scheduled run will continue."
            )
            break

        server_id = queue.pop(0)

        if server_id in sent_ids:
            save_state(sent_ids, queue, next_send_after)
            continue

        # Respect the persisted 30-90 second window first, then refresh main so the
        # online check is as close as possible to the actual Telegram send.
        wait_until_next_slot(next_send_after)

        try:
            latest_servers = load_latest_servers_from_main()
        except Exception as exc:
            queue.insert(0, server_id)
            save_state(sent_ids, queue, next_send_after)
            print(f"[telegram] latest snapshot check failed: {exc}", file=sys.stderr)
            return 1

        server = find_publishable_server(latest_servers, server_id)
        if server is None:
            # It was online when queued but is not online/publishable anymore. Drop
            # it for now; if it comes back online later, sync_queue will append it.
            save_state(sent_ids, queue, next_send_after)
            print(f"[telegram] skipped stale/offline config {server_id}")
            continue

        try:
            message = build_message(server)
            send_message(token, chat_id, topic_id, message)
        except RateLimited as exc:
            queue.insert(0, server_id)
            next_send_after = max(
                next_send_after,
                time.time() + max(exc.retry_after, 1) + 1,
            )
            save_state(sent_ids, queue, next_send_after)
            print(
                f"[telegram] rate limited; retry after {exc.retry_after}s. "
                "State preserved for the next run.",
                file=sys.stderr,
            )
            return 0
        except Exception as exc:
            # Put the failed item back at the front so a transient error does not
            # lose or reorder the queue.
            queue.insert(0, server_id)
            save_state(sent_ids, queue, next_send_after)
            print(f"[telegram] failed for {server_id}: {exc}", file=sys.stderr)
            return 1

        sent_ids.add(server_id)
        published += 1

        # Pick the next allowed send time immediately after a successful message.
        # Keeping this timestamp in persistent state also enforces the minimum delay
        # when a new GitHub Actions run starts right after the previous one.
        delay = random.randint(MIN_SEND_DELAY_SECONDS, MAX_SEND_DELAY_SECONDS)
        next_send_after = time.time() + delay
        save_state(sent_ids, queue, next_send_after)

        print(
            f"[telegram] published {server_id}; next delay {delay}s; "
            f"{len(queue)} still queued."
        )

    print(
        f"[telegram] done. {published} published this run; "
        f"{len(queue)} remain queued."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
