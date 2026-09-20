"""Multi-destination Telegram config publisher.

Each registered group/channel has independent queue, cycle, dedupe history,
pacing and template state. Destination configuration is encrypted on the
telegram-bot-state branch and refreshed while a publisher run is active.
"""
from __future__ import annotations

import html
import json
import os
import random
import subprocess
import sys
import time
from typing import Any, Dict, List, Set, Tuple

import telegram_publisher as base
from telegram_copy_format import CopyableTelegramMessage, decorate_send_payload
from telegram_destinations import (
    DEFAULT_TEMPLATE,
    DESTINATION_STATE_BRANCH,
    DESTINATION_STATE_PATH,
    decrypt_store,
    normalize_template,
)
from telegram_promo_config import DEFAULT_BUTTON_TEXT, DEFAULT_PROMO_URL, local_promo, remote_promo
from telegram_publisher_control import remote_enabled

STATE_PATH = os.environ.get("TELEGRAM_STATE_PATH", "../state/telegram_multi_state.json")
DESTINATION_FILE = os.environ.get(
    "TELEGRAM_DESTINATION_STATE_PATH",
    "../botstate/telegram_destinations.json",
)
ONLINE_SUBSCRIPTION_URL = os.environ.get(
    "BROUTE_ONLINE_SUBSCRIPTION_URL",
    "https://xbroute.github.io/Broute-Collector/data/online-sub-base64.txt",
).strip()

CHECK_INTERVAL_SECONDS = max(
    2, int(os.environ.get("TELEGRAM_CONTROL_CHECK_INTERVAL_SECONDS", "5"))
)
SERVERS_REFRESH_SECONDS = max(
    30, int(os.environ.get("TELEGRAM_SERVERS_REFRESH_SECONDS", "60"))
)
RUN_BUDGET_SECONDS = int(
    os.environ.get("TELEGRAM_RUN_BUDGET_SECONDS", str(9 * 60))
)
RUN_STOP_RESERVE_SECONDS = int(
    os.environ.get("TELEGRAM_RUN_STOP_RESERVE_SECONDS", "45")
)
STATE_PUSH_RETRIES = max(
    1, int(os.environ.get("TELEGRAM_STATE_PUSH_RETRIES", "5"))
)
REQUEST_RETRIES = max(
    1, int(os.environ.get("TELEGRAM_REQUEST_RETRIES", "4"))
)
MAX_TEXT_LENGTH = 4096
STATE_VERSION = 1


class PublishingDisabled(RuntimeError):
    pass


def _git(
    args: List[str],
    cwd: str,
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _default_target_state() -> Dict[str, Any]:
    return {
        "sent": [],
        "sent_fingerprints": [],
        "cycle": 1,
        "cycle_sent": [],
        "cycle_sent_fingerprints": [],
        "queue": [],
        "next_send_after": 0,
        "cycle_started_at": "",
        "suspended_until": 0,
        "last_error": "",
    }


def _normalize_target_state(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    state = _default_target_state()
    for key in ("sent", "sent_fingerprints", "cycle_sent", "cycle_sent_fingerprints", "queue"):
        value = data.get(key, [])
        state[key] = [str(x) for x in value if x] if isinstance(value, list) else []
    try:
        state["cycle"] = max(1, int(data.get("cycle", 1) or 1))
    except (TypeError, ValueError):
        state["cycle"] = 1
    for key in ("next_send_after", "suspended_until"):
        try:
            state[key] = max(0, int(float(data.get(key, 0) or 0)))
        except (TypeError, ValueError):
            state[key] = 0
    state["cycle_started_at"] = str(data.get("cycle_started_at") or "")
    state["last_error"] = str(data.get("last_error") or "")[:500]
    return state


def load_state() -> Dict[str, Any]:
    raw = _read_json(STATE_PATH, {})
    if not isinstance(raw, dict):
        raw = {}
    targets = raw.get("destinations", {})
    if not isinstance(targets, dict):
        targets = {}
    return {
        "version": STATE_VERSION,
        "destinations": {
            str(chat_id): _normalize_target_state(value)
            for chat_id, value in targets.items()
        },
    }


def _aggregate_compatibility(state: Dict[str, Any]) -> Dict[str, Any]:
    """Expose old top-level metrics so the existing bot status UI remains useful."""
    sent: Set[str] = set()
    sent_fps: Set[str] = set()
    cycle_sent: Set[str] = set()
    queue: List[str] = []
    cycles: List[int] = []
    for chat_id, raw in state.get("destinations", {}).items():
        target = _normalize_target_state(raw)
        sent.update(target["sent"])
        sent_fps.update(target["sent_fingerprints"])
        cycle_sent.update(target["cycle_sent"])
        cycles.append(int(target["cycle"]))
        queue.extend(f"{chat_id}:{server_id}" for server_id in target["queue"])
    payload = {
        "version": STATE_VERSION,
        "destinations": state.get("destinations", {}),
        "sent": sorted(sent),
        "sent_fingerprints": sorted(sent_fps),
        "cycle": max(cycles) if cycles else 1,
        "cycle_sent": sorted(cycle_sent),
        "queue": queue,
        "next_send_after": min(
            [
                int(_normalize_target_state(v).get("next_send_after", 0) or 0)
                for v in state.get("destinations", {}).values()
                if int(_normalize_target_state(v).get("next_send_after", 0) or 0) > 0
            ]
            or [0]
        ),
    }
    return payload


def checkpoint_state(state: Dict[str, Any], reason: str) -> None:
    payload = _aggregate_compatibility(state)
    _write_json_atomic(STATE_PATH, payload)

    state_dir = os.path.dirname(os.path.abspath(STATE_PATH))
    if not os.path.isdir(os.path.join(state_dir, ".git")):
        return

    filename = os.path.basename(STATE_PATH)
    for args in (
        ["config", "user.name", "github-actions[bot]"],
        ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        ["add", filename],
    ):
        result = _git(args, state_dir)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git state checkpoint failed")

    diff = _git(["diff", "--cached", "--quiet"], state_dir)
    if diff.returncode == 0:
        return

    commit = _git(
        ["commit", "-m", f"chore: checkpoint multi-destination Telegram state ({reason}) [automated]"],
        state_dir,
    )
    if commit.returncode != 0:
        raise RuntimeError(commit.stderr.strip() or "state commit failed")

    last_error = ""
    for attempt in range(1, STATE_PUSH_RETRIES + 1):
        pushed = _git(["push", "origin", "HEAD:telegram-state"], state_dir, timeout=45)
        if pushed.returncode == 0:
            return
        last_error = pushed.stderr.strip()
        time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(
        f"could not persist Telegram multi-destination state: {last_error}"
    )


def _destination_repo_dir() -> str:
    return os.path.dirname(os.path.abspath(DESTINATION_FILE))


def _load_destination_payload_from_text(text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("encrypted Telegram destination file is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("encrypted Telegram destination state has invalid structure")
    return decrypt_store(payload)


def load_destinations_remote() -> Dict[str, Any]:
    repo_dir = _destination_repo_dir()
    if os.path.isdir(os.path.join(repo_dir, ".git")):
        fetched = _git(["fetch", "origin", DESTINATION_STATE_BRANCH, "--quiet"], repo_dir)
        if fetched.returncode == 0:
            shown = _git(
                ["show", f"origin/{DESTINATION_STATE_BRANCH}:{DESTINATION_STATE_PATH}"],
                repo_dir,
                timeout=10,
            )
            if shown.returncode == 0:
                return _load_destination_payload_from_text(shown.stdout)

    local = _read_json(DESTINATION_FILE, {})
    if isinstance(local, dict) and local:
        return decrypt_store(local)
    return {"manager_user_ids": [], "destinations": []}


def ensure_global_enabled() -> None:
    if not remote_enabled("."):
        raise PublishingDisabled("global Telegram publisher master switch is OFF")


def load_latest_servers() -> List[Dict[str, Any]]:
    fetched = _git(["fetch", "origin", "main", "--quiet"], ".")
    if fetched.returncode == 0:
        shown = _git(["show", "origin/main:data/servers.json"], ".", timeout=15)
        if shown.returncode == 0:
            try:
                data = json.loads(shown.stdout)
                if isinstance(data, list):
                    return [dict(x) for x in data if isinstance(x, dict)]
            except json.JSONDecodeError:
                pass

    data = _read_json(base.SERVERS_PATH, [])
    return [dict(x) for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _eligible_servers(servers: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    ordered: List[str] = []
    for server in servers:
        if not base.eligible(server):
            continue
        server_id = str(server.get("id") or "")
        if not server_id or server_id in by_id:
            continue
        try:
            base.brand_raw_config(
                str(server.get("raw") or ""),
                str(server.get("protocol") or "").lower(),
            )
        except ValueError:
            continue
        by_id[server_id] = server
        ordered.append(server_id)
    return by_id, ordered


def _sync_base(
    servers: List[Dict[str, Any]],
    sent_ids: Set[str],
    sent_fps: Set[str],
    queue: List[str],
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    online_by_id, ordered = _eligible_servers(servers)
    clean: List[str] = []
    queued_ids: Set[str] = set()
    queued_fps: Set[str] = set()

    for server_id in queue:
        if server_id in sent_ids:
            continue
        server = online_by_id.get(str(server_id))
        if server is None:
            continue
        fingerprint = base.telegram_fingerprint(server)
        if fingerprint in sent_fps or fingerprint in queued_fps:
            continue
        clean.append(str(server_id))
        queued_ids.add(str(server_id))
        queued_fps.add(fingerprint)

    for server_id in ordered:
        if server_id in sent_ids or server_id in queued_ids:
            continue
        server = online_by_id[server_id]
        fingerprint = base.telegram_fingerprint(server)
        if fingerprint in sent_fps or fingerprint in queued_fps:
            continue
        clean.append(server_id)
        queued_ids.add(server_id)
        queued_fps.add(fingerprint)

    return online_by_id, clean


def _prioritize_unseen(
    target: Dict[str, Any],
    online_by_id: Dict[str, Dict[str, Any]],
    queue: List[str],
) -> List[str]:
    all_time_fps = set(target.get("sent_fingerprints", []))
    unseen: List[str] = []
    recycled: List[str] = []
    for server_id in queue:
        server = online_by_id.get(server_id)
        if server is None:
            recycled.append(server_id)
            continue
        if base.telegram_fingerprint(server) in all_time_fps:
            recycled.append(server_id)
        else:
            unseen.append(server_id)
    return unseen + recycled


def prepare_target_state(target: Dict[str, Any], servers: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    cycle_sent = set(target.get("cycle_sent", []))
    cycle_fps = set(target.get("cycle_sent_fingerprints", []))
    online_by_id, queue = _sync_base(
        servers,
        cycle_sent,
        cycle_fps,
        list(target.get("queue", [])),
    )
    queue = _prioritize_unseen(target, online_by_id, queue)

    if not queue and online_by_id:
        # Everything currently publishable has completed this destination's
        # active round. Start a fresh round without deleting all-time history.
        refill_by_id, refill = _sync_base(servers, set(), set(), [])
        if refill:
            old_cycle = int(target.get("cycle", 1) or 1)
            target["cycle"] = old_cycle + 1
            target["cycle_sent"] = []
            target["cycle_sent_fingerprints"] = []
            target["cycle_started_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            queue = _prioritize_unseen(target, refill_by_id, refill)
            online_by_id = refill_by_id
            print(
                f"[telegram-multi] destination cycle {old_cycle} complete; "
                f"prepared cycle {old_cycle + 1} with {len(queue)} configs.",
                flush=True,
            )

    target["queue"] = queue
    if not target.get("cycle_started_at"):
        target["cycle_started_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
    return online_by_id


def _template_values(
    destination: Dict[str, Any],
    server: Dict[str, Any],
    branded_config: str,
) -> Dict[str, str]:
    flag = base.country_flag(str(server.get("country") or "")) or "🌍"
    protocol = str(server.get("protocol") or "Unknown").lower()
    transport = str(server.get("transport") or "tcp").lower()
    latency = server.get("latency")
    return {
        "flag": flag,
        "country": str(server.get("country_name") or "Unknown"),
        "protocol": base.PROTOCOL_LABELS.get(protocol, protocol.upper()),
        "security": base.security_label(server),
        "network": base.TRANSPORT_LABELS.get(transport, transport or "Unknown"),
        "latency": f"{latency}ms" if latency is not None else "نامشخص",
        "config": branded_config,
        "brand": base.BRAND_NAME,
        "subscription_url": ONLINE_SUBSCRIPTION_URL,
        "destination": str(destination.get("title") or ""),
    }


def render_target_message(
    destination: Dict[str, Any],
    server: Dict[str, Any],
) -> CopyableTelegramMessage:
    template = normalize_template(str(destination.get("template") or DEFAULT_TEMPLATE))
    protocol = str(server.get("protocol") or "").lower()
    config = base.brand_raw_config(str(server.get("raw") or ""), protocol)
    values = _template_values(destination, server, config)

    plain = template
    for key, value in values.items():
        plain = plain.replace("{" + key + "}", value)
    if len(plain) > MAX_TEXT_LENGTH:
        raise ValueError("rendered destination message exceeds Telegram 4096-character limit")

    rendered = html.escape(template, quote=False)
    for key, value in values.items():
        replacement = (
            f"<pre>{html.escape(value, quote=False)}</pre>"
            if key == "config"
            else html.escape(value, quote=False)
        )
        rendered = rendered.replace("{" + key + "}", replacement)
    return CopyableTelegramMessage(rendered, config)


def _append_url_button(payload: Dict[str, Any], text: str, url: str) -> Dict[str, Any]:
    decorated = dict(payload)
    markup = decorated.get("reply_markup")
    if isinstance(markup, dict):
        markup = dict(markup)
        rows = [
            list(row)
            for row in markup.get("inline_keyboard", [])
            if isinstance(row, list)
        ]
    else:
        markup = {}
        rows = []
    rows.append([{"text": text, "url": url}])
    markup["inline_keyboard"] = rows
    decorated["reply_markup"] = markup
    return decorated


def _current_promo() -> Dict[str, str]:
    try:
        return remote_promo(".")
    except Exception:
        try:
            return local_promo(".")
        except Exception:
            return {"url": DEFAULT_PROMO_URL, "text": DEFAULT_BUTTON_TEXT}


def build_payload(
    destination: Dict[str, Any],
    message: CopyableTelegramMessage,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": int(destination["chat_id"]),
        "text": message,
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }
    thread_id = destination.get("message_thread_id")
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    payload = decorate_send_payload(payload)
    payload = _append_url_button(
        payload,
        "🔄 لینک سابسکریپشن",
        ONLINE_SUBSCRIPTION_URL,
    )
    promo = _current_promo()
    payload = _append_url_button(payload, promo["text"], promo["url"])
    return payload


def send_payload(token: str, payload: Dict[str, Any]) -> None:
    last_error: Exception | None = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            result = base._telegram_request_once(token, payload)
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error: {result}")
            return
        except base.RateLimited:
            raise
        except base.TransientTelegramError as exc:
            last_error = exc
            if attempt == REQUEST_RETRIES:
                break
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(
        f"Telegram transient failure after {REQUEST_RETRIES} attempts: {last_error}"
    )


def _active_destinations(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(item)
        for item in store.get("destinations", [])
        if isinstance(item, dict)
        and item.get("enabled") is True
        and item.get("bot_status") == "administrator"
        and item.get("chat_id")
    ]


def _write_output(should_continue: bool) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"should_continue={'true' if should_continue else 'false'}\n")


def _has_work(
    state: Dict[str, Any],
    destinations: List[Dict[str, Any]],
) -> bool:
    for destination in destinations:
        target = state.get("destinations", {}).get(str(destination["chat_id"]), {})
        if _normalize_target_state(target).get("queue"):
            return True
    return False


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[telegram-multi] TELEGRAM_BOT_TOKEN is required.", file=sys.stderr, flush=True)
        _write_output(False)
        return 1

    try:
        ensure_global_enabled()
    except PublishingDisabled:
        print("[telegram-multi] global master switch is OFF.", flush=True)
        _write_output(False)
        return 0

    state = load_state()
    servers = load_latest_servers()
    last_server_refresh = time.monotonic()
    last_destination_refresh = 0.0
    destination_store: Dict[str, Any] = {"manager_user_ids": [], "destinations": []}
    deadline = time.monotonic() + RUN_BUDGET_SECONDS

    try:
        while True:
            now_mono = time.monotonic()
            if now_mono >= deadline - RUN_STOP_RESERVE_SECONDS:
                break

            ensure_global_enabled()

            if now_mono - last_destination_refresh >= CHECK_INTERVAL_SECONDS:
                destination_store = load_destinations_remote()
                last_destination_refresh = now_mono

            if now_mono - last_server_refresh >= SERVERS_REFRESH_SECONDS:
                servers = load_latest_servers()
                last_server_refresh = now_mono

            destinations = _active_destinations(destination_store)
            if not destinations:
                print("[telegram-multi] no enabled admin destination is registered.", flush=True)
                break

            online_maps: Dict[str, Dict[str, Dict[str, Any]]] = {}
            state_targets = state.setdefault("destinations", {})
            for destination in destinations:
                chat_key = str(destination["chat_id"])
                target = _normalize_target_state(state_targets.get(chat_key))
                online_maps[chat_key] = prepare_target_state(target, servers)
                state_targets[chat_key] = target

            checkpoint_state(state, "queue-sync")

            now = time.time()
            candidates: List[Tuple[float, Dict[str, Any]]] = []
            for destination in destinations:
                chat_key = str(destination["chat_id"])
                target = state_targets[chat_key]
                if not target.get("queue"):
                    continue
                suspended = float(target.get("suspended_until", 0) or 0)
                due = max(
                    float(target.get("next_send_after", 0) or 0),
                    suspended,
                )
                candidates.append((due, destination))

            if not candidates:
                break

            candidates.sort(key=lambda item: item[0])
            due_at, destination = candidates[0]
            if due_at > now:
                sleep_for = min(
                    float(CHECK_INTERVAL_SECONDS),
                    due_at - now,
                    max(0.0, deadline - time.monotonic() - RUN_STOP_RESERVE_SECONDS),
                )
                if sleep_for <= 0:
                    break
                time.sleep(sleep_for)
                continue

            chat_key = str(destination["chat_id"])
            target = state_targets[chat_key]
            queue = list(target.get("queue", []))
            if not queue:
                continue
            server_id = str(queue[0])
            server = online_maps.get(chat_key, {}).get(server_id)
            if server is None:
                target["queue"] = queue[1:]
                checkpoint_state(state, "stale-target-item")
                continue

            fresh = base.live_validate(server)
            if fresh is None:
                # Keep it eligible for a later attempt, but move it behind other
                # candidates so one dead endpoint cannot block the destination.
                target["queue"] = queue[1:] + [server_id]
                target["next_send_after"] = int(time.time() + max(15, min(60, int(destination.get("min_delay_seconds", 30)))))
                target["last_error"] = "live validation failed"
                checkpoint_state(state, "live-skip")
                continue

            fingerprint = base.telegram_fingerprint(fresh)
            try:
                message = render_target_message(destination, fresh)
            except Exception as exc:
                # Template/config combinations that exceed Telegram limits are
                # skipped only for this cycle, not added to all-time sent history.
                target["queue"] = queue[1:]
                target["cycle_sent"] = sorted(set(target.get("cycle_sent", [])) | {server_id})
                target["cycle_sent_fingerprints"] = sorted(
                    set(target.get("cycle_sent_fingerprints", [])) | {fingerprint}
                )
                target["last_error"] = f"render skipped: {str(exc)[:300]}"
                checkpoint_state(state, "render-skip")
                continue

            payload = build_payload(destination, message)
            try:
                send_payload(token, payload)
            except base.RateLimited as exc:
                target["next_send_after"] = int(time.time() + max(1, exc.retry_after))
                target["last_error"] = f"rate limited for {exc.retry_after}s"
                checkpoint_state(state, "rate-limit")
                continue
            except Exception as exc:
                # Do not let one removed/misconfigured chat stop every other
                # destination. Keep the queue intact and temporarily suspend it.
                target["suspended_until"] = int(time.time() + 10 * 60)
                target["last_error"] = str(exc)[:500]
                checkpoint_state(state, "destination-error")
                print(
                    "[telegram-multi] one destination send failed; suspended for 10 minutes.",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            low = int(destination.get("min_delay_seconds", 30))
            high = int(destination.get("max_delay_seconds", 90))
            delay = random.randint(min(low, high), max(low, high))
            target["queue"] = queue[1:]
            target["sent"] = sorted(set(target.get("sent", [])) | {server_id})
            target["sent_fingerprints"] = sorted(
                set(target.get("sent_fingerprints", [])) | {fingerprint}
            )
            target["cycle_sent"] = sorted(
                set(target.get("cycle_sent", [])) | {server_id}
            )
            target["cycle_sent_fingerprints"] = sorted(
                set(target.get("cycle_sent_fingerprints", [])) | {fingerprint}
            )
            target["next_send_after"] = int(time.time() + delay)
            target["suspended_until"] = 0
            target["last_error"] = ""
            checkpoint_state(state, "sent")
            print(
                f"[telegram-multi] published one config; next slot in {delay}s; "
                f"destination queue={len(target['queue'])}.",
                flush=True,
            )

    except PublishingDisabled:
        print(
            "[telegram-multi] global master switch changed to OFF; stopping gracefully.",
            flush=True,
        )
        checkpoint_state(state, "global-off")
        _write_output(False)
        return 0
    except Exception as exc:
        print(f"[telegram-multi] fatal error: {exc}", file=sys.stderr, flush=True)
        try:
            checkpoint_state(state, "fatal")
        except Exception:
            pass
        _write_output(False)
        return 1

    try:
        destinations = _active_destinations(load_destinations_remote())
    except Exception:
        destinations = _active_destinations(destination_store)

    # One last prepare pass ensures a just-completed cycle is refilled before
    # deciding whether a successor run is useful.
    state_targets = state.setdefault("destinations", {})
    for destination in destinations:
        chat_key = str(destination["chat_id"])
        target = _normalize_target_state(state_targets.get(chat_key))
        prepare_target_state(target, servers)
        state_targets[chat_key] = target

    checkpoint_state(state, "handoff")
    should_continue = _has_work(state, destinations)
    _write_output(should_continue)
    print(
        f"[telegram-multi] done; enabled destinations={len(destinations)}; "
        f"successor={'yes' if should_continue else 'no'}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
