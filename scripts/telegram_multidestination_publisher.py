"""Independent multi-destination Telegram publisher.

Each active destination has its own queue, cycle history, pacing window and
next-send timestamp. A failure or pause on one destination never blocks the
others. The encrypted destination registry is refreshed from telegram-bot-state
while a publisher run is alive, so per-destination ON/OFF changes take effect
without waiting for the next workflow run.
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
from typing import Any, Dict, List, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from telegram_destinations import (
    DESTINATION_STATE_BRANCH,
    DESTINATION_STATE_PATH,
    active_destinations,
    decrypt_state,
    default_state,
    load_state_file,
)

STATE_VERSION = 2
REGISTRY_REFRESH_SECONDS = max(
    5, int(os.environ.get("TELEGRAM_DESTINATION_REFRESH_SECONDS", "10"))
)
OFFLINE_COOLDOWN_SECONDS = max(
    60, int(os.environ.get("TELEGRAM_OFFLINE_COOLDOWN_SECONDS", "600"))
)
PERMANENT_FAILURE_COOLDOWN_SECONDS = max(
    300, int(os.environ.get("TELEGRAM_DESTINATION_FAILURE_COOLDOWN_SECONDS", "3600"))
)


def _empty_target_state() -> Dict[str, Any]:
    return {
        "sent": [],
        "sent_fingerprints": [],
        "cycle": 1,
        "cycle_sent": [],
        "cycle_sent_fingerprints": [],
        "queue": [],
        "next_send_after": 0,
        "cycle_started_at": "",
        "cooldowns": {},
        "last_error": "",
        "error_count": 0,
        "suspended_until": 0,
    }


def _normalize_target_state(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    state = _empty_target_state()
    for key in ("sent", "sent_fingerprints", "cycle_sent", "cycle_sent_fingerprints", "queue"):
        values = raw.get(key, [])
        state[key] = [str(x) for x in values if x] if isinstance(values, list) else []
    try:
        state["cycle"] = max(1, int(raw.get("cycle", 1) or 1))
    except (TypeError, ValueError):
        state["cycle"] = 1
    for key in ("next_send_after", "suspended_until"):
        try:
            state[key] = int(float(raw.get(key, 0) or 0))
        except (TypeError, ValueError):
            state[key] = 0
    state["cycle_started_at"] = str(raw.get("cycle_started_at") or "")
    state["last_error"] = str(raw.get("last_error") or "")[:500]
    try:
        state["error_count"] = max(0, int(raw.get("error_count", 0) or 0))
    except (TypeError, ValueError):
        state["error_count"] = 0
    cooldowns = raw.get("cooldowns", {})
    if isinstance(cooldowns, dict):
        now = int(time.time())
        state["cooldowns"] = {
            str(k): int(v)
            for k, v in cooldowns.items()
            if k and isinstance(v, (int, float)) and int(v) > now
        }
    return state


def _read_state_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        raw = {}

    if isinstance(raw, dict) and int(raw.get("version", 0) or 0) == STATE_VERSION:
        destinations = raw.get("destinations", {})
        return {
            "version": STATE_VERSION,
            "legacy": raw.get("legacy", {}),
            "destinations": {
                str(k): _normalize_target_state(v)
                for k, v in destinations.items()
                if isinstance(v, dict)
            } if isinstance(destinations, dict) else {},
            "pending_total": int(raw.get("pending_total", 0) or 0),
            "next_due_after": int(raw.get("next_due_after", 0) or 0),
        }

    # Preserve the previous single-destination state for audit/history, but do
    # not assign it to a newly registered destination. A new destination should
    # start its own independent publication cycle from the current snapshot.
    return {
        "version": STATE_VERSION,
        "legacy": raw if isinstance(raw, dict) else {},
        "destinations": {},
        "pending_total": 0,
        "next_due_after": 0,
    }


def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _checkpoint(publisher: Any, state: Dict[str, Any], active_ids: Set[str], reason: str) -> None:
    pending = 0
    due_values: List[int] = []
    for dest_id in active_ids:
        target = _normalize_target_state(state.get("destinations", {}).get(dest_id))
        pending += len(target.get("queue", []))
        if target.get("queue"):
            due_values.append(int(target.get("next_send_after", 0) or 0))

    state["version"] = STATE_VERSION
    state["pending_total"] = pending
    state["next_due_after"] = min(due_values) if due_values else 0
    _atomic_write(publisher.STATE_PATH, state)

    state_dir = os.path.dirname(os.path.abspath(publisher.STATE_PATH))
    if not os.path.isdir(os.path.join(state_dir, ".git")):
        return

    filename = os.path.basename(publisher.STATE_PATH)
    publisher._git(["config", "user.name", "github-actions[bot]"], state_dir, check=True)
    publisher._git(
        ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        state_dir,
        check=True,
    )
    publisher._git(["add", filename], state_dir, check=True)
    diff = publisher._git(["diff", "--cached", "--quiet"], state_dir)
    if diff.returncode == 0:
        return

    commit = publisher._git(
        ["commit", "-m", f"chore: checkpoint multi-destination Telegram state ({reason}) [automated]"],
        state_dir,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"could not commit Telegram state: {commit.stderr.strip()}")

    last_error = ""
    for attempt in range(1, publisher.STATE_PUSH_RETRIES + 1):
        pushed = publisher._git(["push", "origin", "HEAD:telegram-state"], state_dir, timeout=45)
        if pushed.returncode == 0:
            return
        last_error = pushed.stderr.strip()
        time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"could not persist Telegram state: {last_error}")


def _github_destination_payload() -> Dict[str, Any] | None:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repository or not token:
        return None
    path = quote(DESTINATION_STATE_PATH, safe="/")
    ref = quote(DESTINATION_STATE_BRANCH, safe="")
    req = Request(
        f"https://api.github.com/repos/{repository}/contents/{path}?ref={ref}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=20) as response:
            item = json.loads(response.read().decode("utf-8"))
        raw = base64.b64decode(str(item.get("content") or "").replace("\n", ""))
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not refresh Telegram destinations: {exc}") from exc


def load_registry() -> Dict[str, Any]:
    payload = _github_destination_payload()
    if payload is not None:
        return decrypt_state(payload)
    path = os.environ.get(
        "TELEGRAM_DESTINATION_STATE_PATH",
        "../botstate/telegram_destinations.json",
    )
    return load_state_file(path)


def _prioritize_unseen(publisher: Any, online_by_id: Dict[str, Dict], queue: List[str], target: Dict[str, Any]) -> List[str]:
    all_time = set(target.get("sent_fingerprints", []))
    unseen: List[str] = []
    recycled: List[str] = []
    for server_id in queue:
        server = online_by_id.get(str(server_id))
        if server is None:
            recycled.append(str(server_id))
            continue
        if publisher.telegram_fingerprint(server) in all_time:
            recycled.append(str(server_id))
        else:
            unseen.append(str(server_id))
    return unseen + recycled


def _filter_cooldowns(publisher: Any, online_by_id: Dict[str, Dict], queue: List[str], target: Dict[str, Any]) -> List[str]:
    now = int(time.time())
    cooldowns = target.get("cooldowns", {})
    if not isinstance(cooldowns, dict):
        return queue
    result = []
    for server_id in queue:
        server = online_by_id.get(server_id)
        if server is None:
            continue
        fp = publisher.telegram_fingerprint(server)
        if int(cooldowns.get(fp, 0) or 0) > now:
            continue
        result.append(server_id)
    return result


def sync_target(publisher: Any, servers: List[Dict], target: Dict[str, Any]) -> Dict[str, Dict]:
    cycle_sent = set(target.get("cycle_sent", []))
    cycle_fps = set(target.get("cycle_sent_fingerprints", []))
    queue = [str(x) for x in target.get("queue", []) if x]

    online_by_id, queue, *_ = publisher.sync_queue(servers, cycle_sent, cycle_fps, queue)
    queue = _filter_cooldowns(publisher, online_by_id, queue, target)
    queue = _prioritize_unseen(publisher, online_by_id, queue, target)

    if not queue:
        online_by_id, pending, *_ = publisher.sync_queue(servers, cycle_sent, cycle_fps, [])
        pending = _filter_cooldowns(publisher, online_by_id, pending, target)
        if pending:
            queue = _prioritize_unseen(publisher, online_by_id, pending, target)
        elif online_by_id:
            refill_online, refill, *_ = publisher.sync_queue(servers, set(), set(), [])
            refill = _filter_cooldowns(publisher, refill_online, refill, target)
            if refill:
                target["cycle"] = int(target.get("cycle", 1) or 1) + 1
                target["cycle_sent"] = []
                target["cycle_sent_fingerprints"] = []
                target["cycle_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                queue = _prioritize_unseen(publisher, refill_online, refill, target)
                online_by_id = refill_online
                print(
                    f"[telegram-multi] destination cycle rolled to {target['cycle']} "
                    f"with {len(queue)} configs.",
                    flush=True,
                )

    target["queue"] = queue
    return online_by_id


def _destination_map(registry: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get("id")): dict(item) for item in active_destinations(registry) if item.get("id")}


def _permanent_destination_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "forbidden",
        "chat not found",
        "bot was kicked",
        "bot is not a member",
        "not enough rights",
        "have no rights",
        "message thread not found",
        "topic_closed",
    )
    return any(marker in text for marker in markers)


def _enough_budget(next_send_after: float, deadline_monotonic: float, publisher: Any) -> bool:
    wait_needed = max(0.0, next_send_after - time.time())
    return deadline_monotonic - time.monotonic() >= wait_needed + publisher.RUN_STOP_RESERVE_SECONDS


def _wait_for_due(ensure_enabled, due_at: float, dest_id: str) -> bool:
    while True:
        ensure_enabled()
        registry = load_registry()
        if dest_id not in _destination_map(registry):
            return False
        remaining = due_at - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(float(REGISTRY_REFRESH_SECONDS), remaining))


def main(publisher: Any, ensure_enabled) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[telegram-multi] TELEGRAM_BOT_TOKEN is required.", file=sys.stderr, flush=True)
        return 1

    servers = publisher.load_json(publisher.SERVERS_PATH, [])
    if not isinstance(servers, list):
        print("[telegram-multi] servers.json is not a list.", file=sys.stderr, flush=True)
        return 1

    state = _read_state_file(publisher.STATE_PATH)
    state.setdefault("destinations", {})
    registry = load_registry()
    active_map = _destination_map(registry)

    if not active_map:
        _checkpoint(publisher, state, set(), "no-active-destination")
        print("[telegram-multi] no active Telegram destination; nothing to publish.", flush=True)
        return 0

    online_cache: Dict[str, Dict[str, Dict]] = {}
    for dest_id in active_map:
        target = _normalize_target_state(state["destinations"].get(dest_id))
        state["destinations"][dest_id] = target
        online_cache[dest_id] = sync_target(publisher, servers, target)

    _checkpoint(publisher, state, set(active_map), "queue-sync")
    print(
        f"[telegram-multi] {len(active_map)} active destination(s); "
        f"{state.get('pending_total', 0)} total queued.",
        flush=True,
    )

    deadline = time.monotonic() + publisher.RUN_BUDGET_SECONDS
    published = 0
    live_skips = 0

    while True:
        ensure_enabled()
        registry = load_registry()
        active_map = _destination_map(registry)
        active_ids = set(active_map)

        if not active_ids:
            _checkpoint(publisher, state, set(), "all-destinations-off")
            print("[telegram-multi] all destinations are OFF; stopping cleanly.", flush=True)
            break

        available: List[Tuple[float, str]] = []
        now = time.time()
        for dest_id, config in active_map.items():
            target = _normalize_target_state(state["destinations"].get(dest_id))
            state["destinations"][dest_id] = target
            if int(target.get("suspended_until", 0) or 0) > now:
                continue
            online_cache[dest_id] = sync_target(publisher, servers, target)
            if target.get("queue"):
                available.append((float(target.get("next_send_after", 0) or 0), dest_id))

        if not available:
            _checkpoint(publisher, state, active_ids, "no-due-work")
            print("[telegram-multi] no publishable configs are waiting for active destinations.", flush=True)
            break

        due_at, dest_id = min(available, key=lambda item: (item[0], item[1]))
        if not _enough_budget(due_at, deadline, publisher):
            _checkpoint(publisher, state, active_ids, "handoff")
            print(
                f"[telegram-multi] handing off with {state.get('pending_total', 0)} queued.",
                flush=True,
            )
            break

        if not _wait_for_due(ensure_enabled, due_at, dest_id):
            continue

        registry = load_registry()
        active_map = _destination_map(registry)
        config = active_map.get(dest_id)
        if config is None:
            continue

        target = _normalize_target_state(state["destinations"].get(dest_id))
        state["destinations"][dest_id] = target
        online_by_id = sync_target(publisher, servers, target)
        if not target.get("queue"):
            continue

        server_id = str(target["queue"].pop(0))
        server = online_by_id.get(server_id)
        if server is None:
            _checkpoint(publisher, state, set(active_map), "stale-skip")
            continue

        fingerprint = publisher.telegram_fingerprint(server)
        if fingerprint in set(target.get("cycle_sent_fingerprints", [])):
            _checkpoint(publisher, state, set(active_map), "dedupe-skip")
            continue

        fresh = publisher.live_validate(server)
        if fresh is None:
            target.setdefault("cooldowns", {})[fingerprint] = int(time.time()) + OFFLINE_COOLDOWN_SECONDS
            live_skips += 1
            _checkpoint(publisher, state, set(active_map), "offline-cooldown")
            print(
                f"[telegram-multi] live check failed for {server_id}; "
                f"destination={dest_id}; cooldown={OFFLINE_COOLDOWN_SECONDS}s.",
                flush=True,
            )
            continue

        try:
            message = publisher.build_message(fresh)
            publisher.send_message(
                token,
                str(config.get("chat_id")),
                config.get("thread_id"),
                message,
            )
        except publisher.RateLimited as exc:
            target["queue"].insert(0, server_id)
            pause_until = int(time.time()) + max(int(exc.retry_after), 1) + 1
            for active_id in active_map:
                current = _normalize_target_state(state["destinations"].get(active_id))
                current["next_send_after"] = max(int(current.get("next_send_after", 0) or 0), pause_until)
                state["destinations"][active_id] = current
            _checkpoint(publisher, state, set(active_map), "rate-limit")
            continue
        except Exception as exc:
            target["queue"].insert(0, server_id)
            target["error_count"] = int(target.get("error_count", 0) or 0) + 1
            target["last_error"] = str(exc)[:500]
            delay = PERMANENT_FAILURE_COOLDOWN_SECONDS if _permanent_destination_error(exc) else 60
            target["suspended_until"] = int(time.time()) + delay
            _checkpoint(publisher, state, set(active_map), "destination-send-failure")
            print(
                f"[telegram-multi] destination {dest_id} send failed; "
                f"suspended {delay}s; other destinations continue: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue

        cycle_sent = set(target.get("cycle_sent", []))
        cycle_fps = set(target.get("cycle_sent_fingerprints", []))
        all_sent = set(target.get("sent", []))
        all_fps = set(target.get("sent_fingerprints", []))
        cycle_sent.add(server_id)
        cycle_fps.add(fingerprint)
        all_sent.add(server_id)
        all_fps.add(fingerprint)
        target["cycle_sent"] = sorted(cycle_sent)
        target["cycle_sent_fingerprints"] = sorted(cycle_fps)
        target["sent"] = sorted(all_sent)
        target["sent_fingerprints"] = sorted(all_fps)
        target["last_error"] = ""
        target["error_count"] = 0
        target["suspended_until"] = 0
        delay = random.randint(int(config.get("min_delay", 30)), int(config.get("max_delay", 90)))
        target["next_send_after"] = int(time.time()) + delay
        published += 1

        # If this send emptied the queue, prepare a refill/new cycle before the
        # checkpoint so the workflow knows whether a successor is actually needed.
        online_cache[dest_id] = sync_target(publisher, servers, target)
        _checkpoint(publisher, state, set(active_map), "sent")
        print(
            f"[telegram-multi] published {server_id} to destination={dest_id}; "
            f"latency={fresh.get('latency')}ms; next delay={delay}s; "
            f"destination queue={len(target.get('queue', []))}.",
            flush=True,
        )

    print(
        f"[telegram-multi] done. {published} delivered; {live_skips} live-check skips; "
        f"{state.get('pending_total', 0)} active-destination items remain.",
        flush=True,
    )
    return 0
