import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import telegram_cycle_state
import telegram_publisher as publisher


def sample_server(server_id: str, raw_suffix: str = "", status: str = "online"):
    return {
        "id": server_id,
        "status": status,
        "valid": True,
        "should_remove": False,
        "raw": f"vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls&type=ws&path=/{raw_suffix}#{server_id}",
        "protocol": "vless",
        "address": "example.com",
        "port": 443,
        "transport": "ws",
        "security": "tls",
        "tls": True,
        "latency": 40,
        "country": "US",
        "country_name": "United States",
    }


class TelegramCycleStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_state_path = publisher.STATE_PATH
        self.old_servers_path = publisher.SERVERS_PATH
        self.old_sync_queue = publisher.sync_queue
        publisher.STATE_PATH = os.path.join(self.tempdir.name, "state.json")
        publisher.SERVERS_PATH = os.path.join(self.tempdir.name, "servers.json")

    def tearDown(self):
        publisher.STATE_PATH = self.old_state_path
        publisher.SERVERS_PATH = self.old_servers_path
        publisher.sync_queue = self.old_sync_queue
        self.tempdir.cleanup()

    def write_servers(self, servers):
        with open(publisher.SERVERS_PATH, "w", encoding="utf-8") as f:
            json.dump(servers, f)

    def write_state(self, state):
        with open(publisher.STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def read_state(self):
        with open(publisher.STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_old_state_migrates_without_immediate_resend_then_rolls(self):
        server = sample_server("srv-1")
        fp = publisher.telegram_fingerprint(server)
        self.write_servers([server])
        self.write_state(
            {
                "sent": ["srv-1"],
                "sent_fingerprints": [fp],
                "queue": [],
                "next_send_after": 0,
            }
        )

        adapter = telegram_cycle_state.CycleStateAdapter(publisher)
        cycle_sent, cycle_fps, queue, next_send = adapter.load_state()
        self.assertEqual(cycle_sent, {"srv-1"})
        self.assertEqual(cycle_fps, {fp})
        self.assertEqual(queue, [])

        adapter.save_state(cycle_sent, cycle_fps, queue, next_send)
        state = self.read_state()
        self.assertEqual(state["cycle"], 2)
        self.assertEqual(state["cycle_sent"], [])
        self.assertEqual(state["cycle_sent_fingerprints"], [])
        self.assertEqual(state["sent"], ["srv-1"])
        self.assertEqual(state["queue"], ["srv-1"])

    def test_unseen_config_is_prioritized_before_new_cycle(self):
        first = sample_server("srv-1", "a")
        second = sample_server("srv-2", "b")
        fp1 = publisher.telegram_fingerprint(first)
        self.write_servers([first, second])
        self.write_state(
            {
                "sent": ["srv-1"],
                "sent_fingerprints": [fp1],
                "cycle": 1,
                "cycle_sent": ["srv-1"],
                "cycle_sent_fingerprints": [fp1],
                "queue": [],
                "next_send_after": 0,
            }
        )

        adapter = telegram_cycle_state.CycleStateAdapter(publisher)
        cycle_sent, cycle_fps, queue, next_send = adapter.load_state()
        adapter.save_state(cycle_sent, cycle_fps, queue, next_send)
        state = self.read_state()

        self.assertEqual(state["cycle"], 1)
        self.assertEqual(state["cycle_sent"], ["srv-1"])
        self.assertEqual(state["queue"], ["srv-2"])

    def test_new_live_config_jumps_ahead_of_recycled_existing_backlog(self):
        old_a = sample_server("old-a", "old-a")
        old_b = sample_server("old-b", "old-b")
        newcomer = sample_server("new-c", "new-c")
        old_fp_a = publisher.telegram_fingerprint(old_a)
        old_fp_b = publisher.telegram_fingerprint(old_b)
        self.write_servers([old_a, old_b, newcomer])
        self.write_state(
            {
                "sent": ["historic-a", "historic-b"],
                "sent_fingerprints": [old_fp_a, old_fp_b],
                "cycle": 2,
                "cycle_sent": [],
                "cycle_sent_fingerprints": [],
                "queue": ["old-a", "old-b"],
                "next_send_after": 0,
            }
        )

        adapter = telegram_cycle_state.CycleStateAdapter(publisher)
        online_by_id, queue, added, stale, duplicate = adapter.sync_queue(
            [old_a, old_b, newcomer],
            set(),
            set(),
            ["old-a", "old-b"],
        )

        self.assertEqual(set(online_by_id), {"old-a", "old-b", "new-c"})
        self.assertEqual(added, 1)
        self.assertEqual(stale, 0)
        self.assertEqual(duplicate, 0)
        self.assertEqual(queue, ["new-c", "old-a", "old-b"])

    def test_semantically_seen_id_change_is_not_misclassified_as_new(self):
        previous_id = sample_server("old-id", "same")
        replacement_id = sample_server("new-id", "same")
        # Same connection payload except cosmetic fragment/id. Fingerprint is
        # deliberately semantic, so canonical-ID migrations do not become a
        # priority/resend storm.
        previous_id["raw"] = previous_id["raw"].rsplit("#", 1)[0] + "#old-label"
        replacement_id["raw"] = previous_id["raw"].rsplit("#", 1)[0] + "#new-label"
        old_fp = publisher.telegram_fingerprint(previous_id)
        self.write_servers([replacement_id])
        self.write_state(
            {
                "sent": ["old-id"],
                "sent_fingerprints": [old_fp],
                "cycle": 2,
                "cycle_sent": [],
                "cycle_sent_fingerprints": [],
                "queue": [],
                "next_send_after": 0,
            }
        )

        adapter = telegram_cycle_state.CycleStateAdapter(publisher)
        _, queue, added, *_ = adapter.sync_queue(
            [replacement_id],
            set(),
            set(),
            [],
        )
        self.assertEqual(added, 1)
        self.assertEqual(queue, ["new-id"])
        self.assertEqual(
            publisher.telegram_fingerprint(replacement_id),
            old_fp,
        )
        # It may participate in a later cycle, but it is not classified as an
        # all-time newcomer solely because its collector ID changed.
        self.assertEqual(
            adapter._prioritize_all_time_unseen({"new-id": replacement_id}, queue),
            ["new-id"],
        )

    def test_install_wraps_sync_queue_once(self):
        server = sample_server("srv-1", "a")
        self.write_servers([server])
        self.write_state(
            {
                "sent": [],
                "sent_fingerprints": [],
                "cycle": 1,
                "cycle_sent": [],
                "cycle_sent_fingerprints": [],
                "queue": [],
                "next_send_after": 0,
            }
        )
        adapter = telegram_cycle_state.install_cycle_state(publisher)
        self.assertEqual(publisher.sync_queue, adapter.sync_queue)
        _, queue, *_ = publisher.sync_queue([server], set(), set(), [])
        self.assertEqual(queue, ["srv-1"])

    def test_no_empty_cycle_spin_when_nothing_publishable(self):
        server = sample_server("srv-off", status="offline")
        self.write_servers([server])
        self.write_state(
            {
                "sent": [],
                "sent_fingerprints": [],
                "cycle": 3,
                "cycle_sent": [],
                "cycle_sent_fingerprints": [],
                "queue": [],
                "next_send_after": 0,
            }
        )

        adapter = telegram_cycle_state.CycleStateAdapter(publisher)
        cycle_sent, cycle_fps, queue, next_send = adapter.load_state()
        adapter.save_state(cycle_sent, cycle_fps, queue, next_send)
        state = self.read_state()

        self.assertEqual(state["cycle"], 3)
        self.assertEqual(state["queue"], [])

    def test_real_repository_snapshot_refills_after_complete_cycle(self):
        """Production-shaped integration test; never talks to Telegram/network."""
        snapshot_path = os.path.join(ROOT, "data", "servers.json")
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)

        publishable = [server for server in snapshot if publisher.publishable(server)]
        if not publishable:
            self.skipTest("repository snapshot has no publishable configs")

        self.write_servers(snapshot)
        all_ids = {str(server["id"]) for server in publishable}
        all_fps = {publisher.telegram_fingerprint(server) for server in publishable}
        self.write_state(
            {
                "sent": sorted(all_ids),
                "sent_fingerprints": sorted(all_fps),
                "cycle": 7,
                "cycle_sent": sorted(all_ids),
                "cycle_sent_fingerprints": sorted(all_fps),
                "queue": [],
                "next_send_after": 0,
            }
        )

        adapter = telegram_cycle_state.CycleStateAdapter(publisher)
        cycle_sent, cycle_fps, queue, next_send = adapter.load_state()
        self.assertEqual(queue, [])
        adapter.save_state(cycle_sent, cycle_fps, queue, next_send)
        state = self.read_state()

        self.assertEqual(state["cycle"], 8)
        self.assertEqual(state["cycle_sent"], [])
        self.assertGreater(len(state["queue"]), 0)
        self.assertTrue(set(state["queue"]).issubset(all_ids))
        self.assertTrue(all_ids.issubset(set(state["sent"])))


if __name__ == "__main__":
    unittest.main()
