"""Publish live, deduplicated VPN configs to a Telegram forum topic.

The collector discovers candidates and stores them in data/servers.json.
Before every Telegram send, this publisher performs a fresh TCP liveness check
using the same validator logic as the collector. Persistent state is kept on the
telegram-state branch so already-sent configs are not posted again.
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

from common import country_flag, load_json, rename_raw_config
from validator import resolve_and_check_tcp

SERVERS_PATH = "data/servers.json"
STATE_PATH = os.environ.get("TELEGRAM_STATE_PATH", "data/telegram_state.json")
BRAND_NAME = "@xbroute"

MIN_SEND_DELAY_SECONDS = int(os.environ.get("TELEGRAM_MIN_SEND_DELAY_SECONDS", "30"))
MAX_SEND_DELAY_SECONDS = int(os.environ.get("TELEGRAM_MAX_SEND_DELAY_SECONDS", "90"))
RUN_BUDGET_SECONDS = int(os.environ.get("TELEGRAM_RUN_BUDGET_SECONDS", str(9 * 60)))
RUN_STOP_RESERVE_SECONDS = int(os.environ.get("TELEGRAM_RUN_STOP_RESERVE_SECONDS", "45"))
MAX_TELEGRAM_TEXT_LENGTH = 4096
LIVE_CHECK_RETRIES = max(1, int(os.environ.get("TELEGRAM_LIVE_CHECK_RETRIES", "2")))
STATE_PUSH_RETRIES = max(1, int(os.environ.get("TELEGRAM_STATE_PUSH_RETRIES", "5")))
TELEGRAM_REQUEST_RETRIES = max(1, int(os.environ.get("TELEGRAM_REQUEST_RETRIES", "4")))

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


def brand_raw_config(raw: str, protocol: str) -> str:
    if protocol == "vmess":
        branded = rename_raw_config(raw, protocol, BRAND_NAME)
        if branded != raw:
            return branded

        # rename_raw_config deliberately returns the original raw on decode errors.
        # Accept equality only when the VMess was already branded correctly.
        if raw.startswith("vmess://"):
            data = _b64_json(raw[len("vmess://"):])
            if data is not None and str(data.get("ps") or "") == BRAND_NAME:
                return raw
        raise ValueError("could not rebrand VMess config")

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


def telegram_fingerprint(server: Dict) -> str:
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
            query_map: Dict[str, List[str]] = {}
            for key, value in query_pairs:
                query_map.setdefault(key, []).append(value)

            host_hint = any(
                value
                for key in ("host", "sni", "peer", "servername")
                for value in query_map.get(key, [])
            )

            address = parsed.hostname or ""
            address_key = "<cdn-front>" if _is_ip(address) and host_hint else address.lower()

            canonical = json.dumps(
                {
                    "scheme": parsed.scheme.lower(),
                    "username": unquote(parsed.username or ""),
                    "password": unquote(parsed.password or ""),
                    "address": address_key,
                    "port": parsed.port or 0,
                    "query": query_pairs,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            canonical = raw.split("#", 1)[0]

    return hashlib.sha256(
        f"{protocol}|{canonical}".encode("utf-8")
    ).hexdigest()[:24]


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
    payload = {
        "sent": sorted(sent_ids),
        "sent_fingerprints": sorted(sent_fingerprints),
        "queue": queue,
        "next_send_after": int(next_send_after),
    }

    directory = os.path.dirname(os.path.abspath(STATE_PATH))
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{STATE_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATE_PATH)


def _git(
    args: List[str],
    cwd: str,
    *,
    check: bool = False,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def checkpoint_state(
    sent_ids: Set[str],
    sent_fingerprints: Set[str],
    queue: List[str],
    next_send_after: float,
    reason: str,
) -> None:
    save_state(sent_ids, sent_fingerprints, queue, next_send_after)

    state_dir = os.path.dirname(os.path.abspath(STATE_PATH))
    if not os.path.isdir(os.path.join(state_dir, ".git")):
        return

    filename = os.path.basename(STATE_PATH)
    _git(["config", "user.name", "github-actions[bot]"], state_dir, check=True)
    _git(
        ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        state_dir,
        check=True,
    )
    _git(["add", filename], state_dir, check=True)

    diff = _git(["diff", "--cached", "--quiet"], state_dir)
    if diff.returncode == 0:
        return

    commit = _git(
        ["commit", "-m", f"chore: checkpoint Telegram state ({reason}) [automated]"],
        state_dir,
    )
    if commit.returncode != 0:
        raise RuntimeError(
            f"could not commit Telegram state checkpoint: {commit.stderr.strip()}"
        )

    last_error = ""
    for attempt in range(1, STATE_PUSH_RETRIES + 1):
        pushed = _git(["push", "origin", "HEAD:telegram-state"], state_dir, timeout=45)
        if pushed.returncode == 0:
            return
        last_error = pushed.stderr.strip()
        time.sleep(min(2 ** attempt, 15))

    raise RuntimeError(
        f"could not persist Telegram state after {STATE_PUSH_RETRIES} push attempts: "
        f"{last_error}"
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


class RateLimited(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__(f"Telegram rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


class TransientTelegramError(RuntimeError):
    pass


def _telegram_request_once(token: str, payload: Dict) -> Dict:
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
        if exc.code in {500, 502, 503, 504}:
            raise TransientTelegramError(f"Telegram HTTP {exc.code}") from exc
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError) as exc:
        raise TransientTelegramError(f"Telegram connection error: {exc}") from exc


def send_message(token: str, chat_id: str, topic_id: int, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "message_thread_id": topic_id,
        "text": text,
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }

    last_error: Exception | None = None
    for attempt in range(1, TELEGRAM_REQUEST_RETRIES + 1):
        try:
            result = _telegram_request_once(token, payload)
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error: {result}")
            return
        except RateLimited:
            raise
        except TransientTelegramError as exc:
            last_error = exc
            if attempt == TELEGRAM_REQUEST_RETRIES:
                break
            time.sleep(min(2 ** attempt, 15))

    raise RuntimeError(
        f"Telegram transient failure after {TELEGRAM_REQUEST_RETRIES} attempts: "
        f"{last_error}"
    )


def live_validate(server: Dict) -> Dict | None:
    try:
        address = str(server["address"])
        port = int(server["port"])
    except (KeyError, TypeError, ValueError):
        return None

    for attempt in range(1, LIVE_CHECK_RETRIES + 1):
        check = resolve_and_check_tcp(address, port)
        if check.get("online"):
            fresh = dict(server)
            fresh["status"] = "online"
            fresh["latency"] = check.get("latency_ms")
            fresh["last_checked"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            return fresh
        if attempt < LIVE_CHECK_RETRIES:
            time.sleep(1)

    return None


def wait_until_next_slot(next_send_after: float) -> None:
    sleep_for = next_send_after - time.time()
    if sleep_for > 0:
        print(f"[telegram] pacing delay: sleeping {sleep_for:.1f}s", flush=True)
        time.sleep(sleep_for)


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
        print("[telegram] servers.json is not a list.", file=sys.stderr, flush=True)
        return 1

    sent_ids, sent_fingerprints, queue, next_send_after = load_state()
    backfilled = backfill_sent_fingerprints(
        servers,
        sent_ids,
        sent_fingerprints,
    )

    online_by_id, queue, added_new, removed_stale, removed_duplicate = sync_queue(
        servers,
        sent_ids,
        sent_fingerprints,
        queue,
    )

    try:
        checkpoint_state(
            sent_ids,
            sent_fingerprints,
            queue,
            next_send_after,
            "queue-sync",
        )
    except Exception as exc:
        print(f"[telegram] could not checkpoint queue sync: {exc}", file=sys.stderr, flush=True)
        return 1

    print(
        f"[telegram] queue synced: {len(queue)} pending, "
        f"{added_new} added, {removed_stale} stale removed, "
        f"{removed_duplicate} semantic duplicates removed, "
        f"{len(sent_ids)} already sent, {backfilled} fingerprints backfilled.",
        flush=True,
    )

    if not token or not chat_id or not topic_raw:
        print(
            "[telegram] publishing skipped: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID "
            "and TELEGRAM_TOPIC_ID must all be configured.",
            flush=True,
        )
        return 0

    try:
        topic_id = int(topic_raw)
    except ValueError:
        print("[telegram] TELEGRAM_TOPIC_ID must be an integer.", file=sys.stderr, flush=True)
        return 1

    if not queue:
        print("[telegram] no online configs are waiting in the queue.", flush=True)
        return 0

    published = 0
    skipped_offline = 0
    deadline_monotonic = time.monotonic() + RUN_BUDGET_SECONDS

    while queue:
        if not enough_run_budget(next_send_after, deadline_monotonic):
            print(
                f"[telegram] handing off with {len(queue)} queued; "
                "next workflow run will continue.",
                flush=True,
            )
            break

        server_id = queue.pop(0)

        if server_id in sent_ids:
            continue

        server = online_by_id.get(server_id)
        if server is None:
            try:
                checkpoint_state(
                    sent_ids, sent_fingerprints, queue, next_send_after, "stale-skip"
                )
            except Exception as exc:
                print(f"[telegram] state checkpoint failed: {exc}", file=sys.stderr, flush=True)
                return 1
            continue

        fingerprint = telegram_fingerprint(server)
        if fingerprint in sent_fingerprints:
            sent_ids.add(server_id)
            try:
                checkpoint_state(
                    sent_ids, sent_fingerprints, queue, next_send_after, "dedupe-skip"
                )
            except Exception as exc:
                print(f"[telegram] state checkpoint failed: {exc}", file=sys.stderr, flush=True)
                return 1
            print(f"[telegram] skipped semantic duplicate {server_id}", flush=True)
            continue

        wait_until_next_slot(next_send_after)

        fresh_server = live_validate(server)
        if fresh_server is None:
            skipped_offline += 1
            try:
                checkpoint_state(
                    sent_ids, sent_fingerprints, queue, next_send_after, "offline-skip"
                )
            except Exception as exc:
                print(f"[telegram] state checkpoint failed: {exc}", file=sys.stderr, flush=True)
                return 1
            print(
                f"[telegram] live check failed; skipped currently-offline config {server_id}",
                flush=True,
            )
            continue

        try:
            message = build_message(fresh_server)
            send_message(token, chat_id, topic_id, message)
        except RateLimited as exc:
            queue.insert(0, server_id)
            next_send_after = max(
                next_send_after,
                time.time() + max(exc.retry_after, 1) + 1,
            )
            try:
                checkpoint_state(
                    sent_ids, sent_fingerprints, queue, next_send_after, "rate-limit"
                )
            except Exception as checkpoint_exc:
                print(
                    f"[telegram] state checkpoint failed after rate limit: {checkpoint_exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            print(
                f"[telegram] rate limited; retry after {exc.retry_after}s. "
                "State preserved for the next run.",
                file=sys.stderr,
                flush=True,
            )
            return 0
        except Exception as exc:
            queue.insert(0, server_id)
            try:
                checkpoint_state(
                    sent_ids, sent_fingerprints, queue, next_send_after, "send-failure"
                )
            except Exception as checkpoint_exc:
                print(
                    f"[telegram] send failed ({exc}); state checkpoint also failed: "
                    f"{checkpoint_exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            print(f"[telegram] failed for {server_id}: {exc}", file=sys.stderr, flush=True)
            return 1

        sent_ids.add(server_id)
        sent_fingerprints.add(fingerprint)
        published += 1

        delay = random.randint(MIN_SEND_DELAY_SECONDS, MAX_SEND_DELAY_SECONDS)
        next_send_after = time.time() + delay

        try:
            checkpoint_state(
                sent_ids, sent_fingerprints, queue, next_send_after, "sent"
            )
        except Exception as exc:
            print(
                f"[telegram] message sent but durable state checkpoint failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1

        print(
            f"[telegram] published {server_id}; live latency "
            f"{fresh_server.get('latency')}ms; next delay {delay}s; "
            f"{len(queue)} still queued.",
            flush=True,
        )

    print(
        f"[telegram] done. {published} published, {skipped_offline} live-check skips; "
        f"{len(queue)} remain queued.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
