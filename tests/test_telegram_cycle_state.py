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
        publisher.STATE_PATH = os.path.join(self.tempdir.name, "state.json")
        publisher.SERVERS_PATH = os.path.join(self.tempdir.name, "servers.json")

    def tearDown(self):
        publisher.STATE_PATH = self.old_state_path
        publisher.SERVERS_PATH = self.old_servers_path
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

        # Persisting an exhausted migrated round prepares round 2, while the
        # all-time history remains intact.
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
        # All-time history is retained; a new cycle is not implemented by
        # deleting sent history.
        self.assertTrue(all_ids.issubset(set(state["sent"])))


if __name__ == "__main__":
    unittest.main()
