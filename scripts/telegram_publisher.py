"""Publish online configs to a Telegram forum topic using a persistent queue.

Required environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    TELEGRAM_TOPIC_ID

Optional environment variable:
    TELEGRAM_STATE_PATH

Telegram publishing has its own semantic de-duplication layer. The collector keeps
distinct endpoint IPs, while the publisher can collapse CDN-front variants that
share the same protocol, credentials and routing parameters.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import random
import subprocess
import sys
import time
from typing import Dict, List, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import Request, urlopen

from common import country_flag, load_json, rename_raw_config, save_json

SERVERS_PATH = "data/servers.json"
STATE_PATH = os.environ.get("TELEGRAM_STATE_PATH", "data/telegram_state.json")
BRAND_NAME = "@xbroute"

MIN_SEND_DELAY_SECONDS = 30
MAX_SEND_DELAY_SECONDS = 90
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

CDN_AWARE_PROTOCOLS = {"vless", "vmess", "trojan", "hysteria2", "tuic"}


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
        f"⚡ تأخیر تست: {latency_text}",
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
    if not eligible(server):
        return False
    try:
        build_message(server)
    except ValueError:
        return False
    return True


def _is_ip(value: str) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def _b64_json(payload: str) -> Dict | None:
    try:
        payload = payload.split("#", 1)[0].strip()
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def telegram_fingerprint(server: Dict) -> str:
    """Return a stable Telegram-level identity for a config.

    Collector IDs intentionally include address/IP. For CDN-front configs, multiple
    Cloudflare/front IPs with the same credentials + Host/SNI + query parameters are
    effectively the same configuration for a Telegram feed, so the front IP is
    ignored only in that narrow case.
    """
    protocol = str(server.get("protocol") or "").lower()
    raw = str(server.get("raw") or "").strip()

    canonical = raw.split("#", 1)[0]

    if protocol == "vmess" and raw.startswith("vmess://"):
        data = _b64_json(raw[len("vmess://"):])
        if data is not None:
            data = dict(data)
            data.pop("ps", None)

            address = str(data.get("add") or "")
            host = str(data.get("host") or "")
            sni = str(data.get("sni") or "")
            if _is_ip(address) and (host or sni):
                data["add"] = "<cdn-front>"

            canonical = json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

    elif protocol in CDN_AWARE_PROTOCOLS and "://" in raw:
        try:
            parsed = urlsplit(raw.split("#", 1)[0])
            query_pairs = sorted(
                (str(k).lower(), str(v))
                for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            )
            query_map = {}
            for key, value in query_pairs:
                query_map.setdefault(key, []).append(value)

            host_hint = any(
                value
                for key in ("host", "sni", "peer", "servername")
                for value in query_map.get(key, [])
            )

            address = parsed.hostname or ""
            address_key = "<cdn-front>" if _is_ip(address) and host_hint else address.lower()
            username = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            port = parsed.port or 0

            canonical = json.dumps(
                {
                    "scheme": parsed.scheme.lower(),
                    "username": username,
                    "password": password,
                    "address": address_key,
                    "port": port,
                    "query": query_pairs,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            canonical = raw.split("#", 1)[0]

    digest = hashlib.sha256(f"{protocol}|{canonical}".encode("utf-8")).hexdigest()
    return digest[:24]


def load_state() -> Tuple[Set[str], Set[str], List[str], float]:
    raw_state = load_json(
        STATE_PATH,
        {
            "sent": [],
            "sent_fingerprints": [],
            "queue": [],
            "next_send_after": 0,
        },
    )

    if isinstance(raw_state, list):
        sent_raw = raw_state
        fingerprint_raw = []
        queue_raw = []
        next_send_after = 0.0
    elif isinstance(raw_state, dict):
        sent_raw = raw_state.get("sent", [])
        fingerprint_raw = raw_state.get("sent_fingerprints", [])
        queue_raw = raw_state.get("queue", [])
        try:
            next_send_after = float(raw_state.get("next_send_after", 0) or 0)
        except (TypeError, ValueError):
            next_send_after = 0.0
    else:
        sent_raw = []
        fingerprint_raw = []
        queue_raw = []
        next_send_after = 0.0

    sent_ids = {str(item) for item in sent_raw if item}
    sent_fingerprints = {str(item) for item in fingerprint_raw if item}

    queue: List[str] = []
    seen: Set[str] = set()
    for item in queue_raw:
        server_id = str(item)
        if not server_id or server_id in seen or server_id in sent_ids:
            continue
        seen.add(server_id)
        queue.append(server_id)

    return sent_ids, sent_fingerprints, queue, next_send_after


def save_state(
    sent_ids: Set[str],
    sent_fingerprints: Set[str],
    queue: List[str],
    next_send_after: float,
) -> None:
    save_json(
        STATE_PATH,
        {
            "sent": sorted(sent_ids),
            "sent_fingerprints": sorted(sent_fingerprints),
            "queue": queue,
            "next_send_after": int(next_send_after),
        },
    )


def backfill_sent_fingerprints(
    servers: List[Dict],
    sent_ids: Set[str],
    sent_fingerprints: Set[str],
) -> int:
    added = 0
    for server in servers:
        server_id = str(server.get("id") or "")
        if server_id and server_id in sent_ids and server.get("raw"):
            fingerprint = telegram_fingerprint(server)
            if fingerprint not in sent_fingerprints:
                sent_fingerprints.add(fingerprint)
                added += 1
    return added


def sync_queue(
    servers: List[Dict],
    sent_ids: Set[str],
    sent_fingerprints: Set[str],
    queue: List[str],
) -> Tuple[Dict[str, Dict], List[str], int, int, int]:
    """Reconcile queue with latest online snapshot and Telegram-level dedupe."""
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
    queued_fingerprints: Set[str] = set()
    removed_stale = 0
    removed_duplicate = 0

    for server_id in queue:
        if server_id in sent_ids:
            continue

        server = online_by_id.get(server_id)
        if server is None:
            removed_stale += 1
            continue

        fingerprint = telegram_fingerprint(server)
        if fingerprint in sent_fingerprints or fingerprint in queued_fingerprints:
            removed_duplicate += 1
            continue

        if server_id in queued_ids:
            continue

        clean_queue.append(server_id)
        queued_ids.add(server_id)
        queued_fingerprints.add(fingerprint)

    added_new = 0
    for server_id in ordered_online_ids:
        if server_id in sent_ids or server_id in queued_ids:
            continue

        server = online_by_id[server_id]
        fingerprint = telegram_fingerprint(server)
        if fingerprint in sent_fingerprints or fingerprint in queued_fingerprints:
            continue

        clean_queue.append(server_id)
        queued_ids.add(server_id)
        queued_fingerprints.add(fingerprint)
        added_new += 1

    return (
        online_by_id,
        clean_queue,
        added_new,
        removed_stale,
        removed_duplicate,
    )


def load_latest_servers_from_main() -> List[Dict]:
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

    sent_ids, sent_fingerprints, queue, next_send_after = load_state()
    backfilled = backfill_sent_fingerprints(
        servers,
        sent_ids,
        sent_fingerprints,
    )

    _, queue, added_new, removed_stale, removed_duplicate = sync_queue(
        servers,
        sent_ids,
        sent_fingerprints,
        queue,
    )

    save_state(sent_ids, sent_fingerprints, queue, next_send_after)
    print(
        f"[telegram] queue synced: {len(queue)} pending, "
        f"{added_new} added, {removed_stale} stale removed, "
        f"{removed_duplicate} semantic duplicates removed, "
        f"{len(sent_ids)} already sent, {backfilled} fingerprints backfilled."
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
                "next workflow run will continue."
            )
            break

        server_id = queue.pop(0)

        if server_id in sent_ids:
            save_state(sent_ids, sent_fingerprints, queue, next_send_after)
            continue

        wait_until_next_slot(next_send_after)

        try:
            latest_servers = load_latest_servers_from_main()
        except Exception as exc:
            queue.insert(0, server_id)
            save_state(sent_ids, sent_fingerprints, queue, next_send_after)
            print(f"[telegram] latest snapshot check failed: {exc}", file=sys.stderr)
            return 1

        server = find_publishable_server(latest_servers, server_id)
        if server is None:
            save_state(sent_ids, sent_fingerprints, queue, next_send_after)
            print(f"[telegram] skipped stale/offline config {server_id}")
            continue

        fingerprint = telegram_fingerprint(server)
        if fingerprint in sent_fingerprints:
            sent_ids.add(server_id)
            save_state(sent_ids, sent_fingerprints, queue, next_send_after)
            print(f"[telegram] skipped semantic duplicate {server_id}")
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
            save_state(sent_ids, sent_fingerprints, queue, next_send_after)
            print(
                f"[telegram] rate limited; retry after {exc.retry_after}s. "
                "State preserved for the next run.",
                file=sys.stderr,
            )
            return 0
        except Exception as exc:
            queue.insert(0, server_id)
            save_state(sent_ids, sent_fingerprints, queue, next_send_after)
            print(f"[telegram] failed for {server_id}: {exc}", file=sys.stderr)
            return 1

        sent_ids.add(server_id)
        sent_fingerprints.add(fingerprint)
        published += 1

        delay = random.randint(MIN_SEND_DELAY_SECONDS, MAX_SEND_DELAY_SECONDS)
        next_send_after = time.time() + delay
        save_state(sent_ids, sent_fingerprints, queue, next_send_after)

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
