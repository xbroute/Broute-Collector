import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_multidestination_publisher as multi


class FakePublisher:
    @staticmethod
    def telegram_fingerprint(server):
        return "fp-" + str(server["id"])

    @staticmethod
    def publishable(server):
        return server.get("status") == "online"

    @classmethod
    def sync_queue(cls, servers, sent_ids, sent_fps, queue):
        online = {str(s["id"]): s for s in servers if cls.publishable(s)}
        clean = []
        seen = set()
        for sid in queue:
            sid = str(sid)
            if sid in sent_ids or sid not in online or sid in seen:
                continue
            fp = cls.telegram_fingerprint(online[sid])
            if fp in sent_fps:
                continue
            clean.append(sid)
            seen.add(sid)
        added = 0
        for sid, server in online.items():
            if sid in sent_ids or sid in seen:
                continue
            if cls.telegram_fingerprint(server) in sent_fps:
                continue
            clean.append(sid)
            seen.add(sid)
            added += 1
        return online, clean, added, 0, 0


class MultiDestinationStateTests(unittest.TestCase):
    def setUp(self):
        self.servers = [
            {"id": "a", "status": "online"},
            {"id": "b", "status": "online"},
        ]

    def test_legacy_state_is_preserved_but_not_assigned_to_new_destination(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sent": ["legacy-a"], "queue": ["legacy-b"]}, f)
            state = multi._read_state_file(path)
            self.assertEqual(state["legacy"]["sent"], ["legacy-a"])
            self.assertEqual(state["destinations"], {})

    def test_each_destination_has_independent_queue_and_cycle(self):
        one = multi._empty_target_state()
        two = multi._empty_target_state()
        multi.sync_target(FakePublisher, self.servers, one)
        multi.sync_target(FakePublisher, self.servers, two)
        self.assertEqual(one["queue"], ["a", "b"])
        self.assertEqual(two["queue"], ["a", "b"])

        one["queue"].pop(0)
        one["cycle_sent"] = ["a"]
        one["cycle_sent_fingerprints"] = ["fp-a"]
        one["sent"] = ["a"]
        one["sent_fingerprints"] = ["fp-a"]
        multi.sync_target(FakePublisher, self.servers, one)
        self.assertEqual(one["queue"], ["b"])
        self.assertEqual(two["queue"], ["a", "b"])

    def test_finished_destination_rolls_its_own_cycle(self):
        target = multi._empty_target_state()
        target["cycle_sent"] = ["a", "b"]
        target["cycle_sent_fingerprints"] = ["fp-a", "fp-b"]
        target["sent"] = ["a", "b"]
        target["sent_fingerprints"] = ["fp-a", "fp-b"]
        target["cycle"] = 3
        multi.sync_target(FakePublisher, self.servers, target)
        self.assertEqual(target["cycle"], 4)
        self.assertEqual(target["cycle_sent"], [])
        self.assertEqual(target["queue"], ["a", "b"])

    def test_offline_cooldown_does_not_delete_all_time_history(self):
        target = multi._empty_target_state()
        target["sent"] = ["old"]
        target["sent_fingerprints"] = ["fp-old"]
        target["cooldowns"] = {"fp-a": int(time.time()) + 600}
        multi.sync_target(FakePublisher, self.servers, target)
        self.assertEqual(target["queue"], ["b"])
        self.assertEqual(target["sent"], ["old"])


if __name__ == "__main__":
    unittest.main()
