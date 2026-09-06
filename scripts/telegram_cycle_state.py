"""Persistent cycle state for continuous Telegram publishing.

The base publisher keeps a durable queue and a sent set. This adapter preserves
that all-time history while introducing a per-cycle sent set. When the current
cycle has exhausted every currently publishable semantic config, the next cycle
is prepared automatically from the latest snapshot instead of leaving the
publisher permanently idle.

Live subscriptions can add configs while a cycle already has a large recycled
backlog. Those truly new semantic configs are promoted ahead of configs that
have already been published in an earlier cycle, so freshness is not hidden
behind hundreds of routine repeats. Ordering remains stable within each class.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Set, Tuple


def _read_json(path: str, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else dict(default)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(default)


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


class CycleStateAdapter:
    def __init__(self, publisher_module: Any):
        self.publisher = publisher_module
        self.state_path = publisher_module.STATE_PATH
        # Keep an immutable reference to the base queue synchronizer before
        # install_cycle_state replaces publisher.sync_queue with our wrapper.
        self._base_sync_queue = publisher_module.sync_queue
        self.state: Dict[str, Any] = {}
        self._load_raw()

    def _load_raw(self) -> None:
        raw = _read_json(
            self.state_path,
            {
                "sent": [],
                "sent_fingerprints": [],
                "queue": [],
                "next_send_after": 0,
            },
        )

        all_time_sent = [str(x) for x in raw.get("sent", []) if x]
        all_time_fps = [str(x) for x in raw.get("sent_fingerprints", []) if x]

        # Backward-compatible migration: before cycle support, the all-time sent
        # set was also the dedupe set for the active publishing round. Treat that
        # existing history as cycle #1 so nothing suddenly re-sends on deploy.
        cycle_sent_raw = raw.get("cycle_sent")
        cycle_fps_raw = raw.get("cycle_sent_fingerprints")
        if not isinstance(cycle_sent_raw, list):
            cycle_sent_raw = list(all_time_sent)
        if not isinstance(cycle_fps_raw, list):
            cycle_fps_raw = list(all_time_fps)

        try:
            cycle = max(1, int(raw.get("cycle", 1) or 1))
        except (TypeError, ValueError):
            cycle = 1

        try:
            next_send_after = float(raw.get("next_send_after", 0) or 0)
        except (TypeError, ValueError):
            next_send_after = 0.0

        self.state = {
            "sent": sorted(set(all_time_sent)),
            "sent_fingerprints": sorted(set(all_time_fps)),
            "cycle": cycle,
            "cycle_sent": sorted({str(x) for x in cycle_sent_raw if x}),
            "cycle_sent_fingerprints": sorted({str(x) for x in cycle_fps_raw if x}),
            "queue": [str(x) for x in raw.get("queue", []) if x],
            "next_send_after": int(next_send_after),
            "cycle_started_at": str(raw.get("cycle_started_at") or ""),
        }

    def _current_servers(self) -> List[Dict[str, Any]]:
        try:
            with open(self.publisher.SERVERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def load_state(self) -> Tuple[Set[str], Set[str], List[str], float]:
        # Reload from disk in case a queue-sync checkpoint was written before a
        # later wrapper call in the same process.
        self._load_raw()

        sent_ids = set(self.state["cycle_sent"])
        sent_fps = set(self.state["cycle_sent_fingerprints"])

        queue: List[str] = []
        seen: Set[str] = set()
        for item in self.state.get("queue", []):
            server_id = str(item)
            if not server_id or server_id in seen or server_id in sent_ids:
                continue
            seen.add(server_id)
            queue.append(server_id)

        return sent_ids, sent_fps, queue, float(self.state.get("next_send_after", 0) or 0)

    def _prioritize_all_time_unseen(
        self,
        online_by_id: Dict[str, Dict[str, Any]],
        queue: List[str],
    ) -> List[str]:
        """Stable-partition queue so never-published semantic configs come first."""
        all_time_fps = set(self.state.get("sent_fingerprints", []))
        unseen: List[str] = []
        recycled: List[str] = []

        for server_id in queue:
            server = online_by_id.get(str(server_id))
            if server is None:
                recycled.append(str(server_id))
                continue
            fingerprint = self.publisher.telegram_fingerprint(server)
            if fingerprint not in all_time_fps:
                unseen.append(str(server_id))
            else:
                recycled.append(str(server_id))

        prioritized = unseen + recycled
        if unseen and prioritized != list(queue):
            print(
                f"[telegram-cycle] prioritized {len(unseen)} never-sent config(s) "
                "ahead of recycled backlog.",
                flush=True,
            )
        return prioritized

    def sync_queue(
        self,
        servers: List[Dict],
        cycle_sent: Set[str],
        cycle_fingerprints: Set[str],
        queue: List[str],
    ):
        """Delegate normal dedupe/stale cleanup, then prioritize true newcomers."""
        result = self._base_sync_queue(
            servers,
            cycle_sent,
            cycle_fingerprints,
            queue,
        )
        online_by_id, clean_queue, added_new, removed_stale, removed_duplicate = result
        prioritized = self._prioritize_all_time_unseen(online_by_id, clean_queue)
        return (
            online_by_id,
            prioritized,
            added_new,
            removed_stale,
            removed_duplicate,
        )

    def _plan_queue(
        self,
        cycle_sent: Set[str],
        cycle_fps: Set[str],
        queue: List[str],
    ) -> Tuple[List[str], bool]:
        """Return queue-to-persist and whether a brand-new cycle was started."""
        if queue:
            return list(queue), False

        servers = self._current_servers()
        if not servers:
            return [], False

        # First check whether the current cycle still has unseen/currently-online
        # candidates. This covers live-check skips and newly appeared configs.
        online_by_id, pending, *_ = self._base_sync_queue(
            servers,
            cycle_sent,
            cycle_fps,
            [],
        )
        if pending:
            return self._prioritize_all_time_unseen(online_by_id, pending), False

        # A new cycle is valid only if there is at least one publishable config
        # in the latest snapshot. Do not spin empty cycles when everything is
        # offline/invalid.
        if not online_by_id:
            return [], False

        refill_online, refill, *_ = self._base_sync_queue(servers, set(), set(), [])
        if not refill:
            return [], False

        return self._prioritize_all_time_unseen(refill_online, refill), True

    def save_state(
        self,
        cycle_sent: Set[str],
        cycle_fingerprints: Set[str],
        queue: List[str],
        next_send_after: float,
    ) -> None:
        all_time_sent = set(self.state.get("sent", [])) | set(cycle_sent)
        all_time_fps = set(self.state.get("sent_fingerprints", [])) | set(cycle_fingerprints)

        # Make newly sent fingerprints visible to queue prioritization immediately
        # in the same process, before _plan_queue evaluates any refill.
        self.state["sent"] = sorted(all_time_sent)
        self.state["sent_fingerprints"] = sorted(all_time_fps)

        queue_to_save, rolled = self._plan_queue(
            set(cycle_sent),
            set(cycle_fingerprints),
            list(queue),
        )

        cycle = int(self.state.get("cycle", 1) or 1)
        cycle_started_at = str(self.state.get("cycle_started_at") or "")

        if rolled:
            cycle += 1
            persisted_cycle_sent: Set[str] = set()
            persisted_cycle_fps: Set[str] = set()
            cycle_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            print(
                f"[telegram-cycle] cycle {cycle - 1} complete; "
                f"prepared cycle {cycle} with {len(queue_to_save)} configs.",
                flush=True,
            )
        else:
            persisted_cycle_sent = set(cycle_sent)
            persisted_cycle_fps = set(cycle_fingerprints)
            if not cycle_started_at:
                cycle_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        payload = {
            "sent": sorted(all_time_sent),
            "sent_fingerprints": sorted(all_time_fps),
            "cycle": cycle,
            "cycle_sent": sorted(persisted_cycle_sent),
            "cycle_sent_fingerprints": sorted(persisted_cycle_fps),
            "queue": queue_to_save,
            "next_send_after": int(next_send_after),
            "cycle_started_at": cycle_started_at,
        }

        self.state = payload
        _write_json_atomic(self.state_path, payload)


def install_cycle_state(publisher_module: Any) -> CycleStateAdapter:
    adapter = CycleStateAdapter(publisher_module)
    publisher_module.load_state = adapter.load_state
    publisher_module.save_state = adapter.save_state
    publisher_module.sync_queue = adapter.sync_queue
    return adapter
