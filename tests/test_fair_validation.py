import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import deduplicator
import generator
import validation_scheduler as scheduler
from common import parse_config_line


def record(record_id, source, *, last_checked=None):
    item = {
        "id": record_id,
        "raw": f"vless://00000000-0000-0000-0000-{record_id[-12:].zfill(12)}@example.com:443?security=tls&type=ws",
        "protocol": "vless",
        "address": "example.com",
        "port": 443,
        "uuid_or_password": record_id,
        "transport": "ws",
        "security": "tls",
        "tls": True,
        "host": "example.com",
        "sni": "example.com",
        "valid": True,
        "secure": True,
        "source_name": source,
        "source_url": f"https://{source}.example/sub",
        "sources": [
            {
                "source_name": source,
                "source_url": f"https://{source}.example/sub",
            }
        ],
        "status": "unknown",
        "latency": None,
        "should_remove": False,
    }
    previous = None
    if last_checked is not None:
        previous = dict(item)
        previous.update(
            {
                "status": "online",
                "latency": 10,
                "last_checked": last_checked,
                "last_seen": last_checked,
                "success_count": 1,
                "fail_count": 0,
                "consecutive_failures": 0,
                "country": "US",
                "country_name": "United States",
            }
        )
    return item, previous


class CanonicalIdentityTests(unittest.TestCase):
    def test_query_order_and_fragment_do_not_change_identity(self):
        a = SimpleNamespace(
            protocol="vless",
            raw=(
                "vless://u@example.com:443/path?security=reality&type=tcp"
                "&pbk=abc&sid=01#source-a"
            ),
        )
        b = SimpleNamespace(
            protocol="vless",
            raw=(
                "vless://u@example.com:443/path?sid=01&pbk=abc"
                "&type=tcp&security=reality#source-b"
            ),
        )
        self.assertEqual(
            deduplicator.canonical_connection_key(a),
            deduplicator.canonical_connection_key(b),
        )

    def test_path_and_reality_key_changes_are_distinct(self):
        base = SimpleNamespace(
            protocol="vless",
            raw="vless://u@example.com:443/a?security=reality&type=tcp&pbk=abc",
        )
        path_changed = SimpleNamespace(
            protocol="vless",
            raw="vless://u@example.com:443/b?security=reality&type=tcp&pbk=abc",
        )
        key_changed = SimpleNamespace(
            protocol="vless",
            raw="vless://u@example.com:443/a?security=reality&type=tcp&pbk=xyz",
        )
        self.assertNotEqual(
            deduplicator.canonical_connection_key(base),
            deduplicator.canonical_connection_key(path_changed),
        )
        self.assertNotEqual(
            deduplicator.canonical_connection_key(base),
            deduplicator.canonical_connection_key(key_changed),
        )

    def test_vmess_display_name_is_ignored_but_connection_fields_are_not(self):
        import base64
        import json

        def vmess(ps, path):
            data = {
                "v": "2",
                "ps": ps,
                "add": "example.com",
                "port": "443",
                "id": "00000000-0000-0000-0000-000000000000",
                "net": "ws",
                "host": "cdn.example.com",
                "path": path,
                "tls": "tls",
            }
            payload = base64.urlsafe_b64encode(
                json.dumps(data, separators=(",", ":")).encode()
            ).decode().rstrip("=")
            return SimpleNamespace(protocol="vmess", raw=f"vmess://{payload}")

        self.assertEqual(
            deduplicator.canonical_connection_key(vmess("A", "/same")),
            deduplicator.canonical_connection_key(vmess("B", "/same")),
        )
        self.assertNotEqual(
            deduplicator.canonical_connection_key(vmess("A", "/same")),
            deduplicator.canonical_connection_key(vmess("A", "/changed")),
        )


class ProvenanceTests(unittest.TestCase):
    def test_deduplicate_preserves_every_source_membership(self):
        raw_a = (
            "vless://00000000-0000-0000-0000-000000000001@example.com:443"
            "?security=tls&type=ws&path=%2Fa#one"
        )
        raw_b = raw_a.rsplit("#", 1)[0] + "#two"
        cfg_a = parse_config_line(raw_a)
        cfg_b = parse_config_line(raw_b)
        self.assertIsNotNone(cfg_a)
        self.assertIsNotNone(cfg_b)

        result = deduplicator.deduplicate(
            [
                {"config": cfg_a, "source_name": "A", "source_url": "https://a.example/sub"},
                {"config": cfg_b, "source_name": "B", "source_url": "managed://bbbb"},
            ]
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["sources"],
            [
                {"source_name": "A", "source_url": "https://a.example/sub"},
                {"source_name": "B", "source_url": "managed://bbbb"},
            ],
        )


class FairSchedulerTests(unittest.TestCase):
    def test_unseen_late_source_is_prioritized_before_old_refreshes(self):
        records = []
        previous = {}
        for i in range(10):
            item, prev = record(f"old-{i}", "early", last_checked="2026-09-06T10:00:00Z")
            records.append(item)
            previous[item["id"]] = prev
        for i in range(3):
            item, _ = record(f"new-{i}", "managed")
            records.append(item)

        selected, _, stats = scheduler.select_for_validation(records, previous, 5)
        selected_ids = {item["id"] for item in selected}
        self.assertTrue({"new-0", "new-1", "new-2"}.issubset(selected_ids))
        self.assertEqual(stats["new_unvalidated"], 3)
        self.assertEqual(len(selected), 5)

    def test_budget_is_fair_across_many_sources(self):
        records = []
        previous = {}
        for source_index in range(30):
            for item_index in range(5):
                item, prev = record(
                    f"s{source_index:02d}-{item_index}",
                    f"source-{source_index:02d}",
                    last_checked="2026-09-06T10:00:00Z",
                )
                records.append(item)
                previous[item["id"]] = prev

        selected, _, _ = scheduler.select_for_validation(records, previous, 30)
        selected_sources = {item["source_name"] for item in selected}
        self.assertEqual(len(selected_sources), 30)

    def test_large_new_source_progresses_across_runs_without_starvation(self):
        records = [record(f"new-{i:03d}", "managed")[0] for i in range(600)]
        selected1, deferred1, _ = scheduler.select_for_validation(records, {}, 500)
        self.assertEqual(len(selected1), 500)
        self.assertEqual(len(deferred1), 100)

        previous = {}
        for item in selected1:
            prev = dict(item)
            prev["last_checked"] = "2026-09-06T12:00:00Z"
            previous[item["id"]] = prev

        selected2, _, stats2 = scheduler.select_for_validation(records, previous, 500)
        selected2_ids = {item["id"] for item in selected2}
        self.assertTrue({item["id"] for item in deferred1}.issubset(selected2_ids))
        self.assertEqual(stats2["new_unvalidated"], 100)


class GeneratorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.old_budget = generator.MAX_VALIDATIONS_PER_RUN

    def tearDown(self):
        generator.MAX_VALIDATIONS_PER_RUN = self.old_budget

    def test_snapshot_is_not_truncated_when_validation_budget_is_smaller(self):
        generator.MAX_VALIDATIONS_PER_RUN = 2
        records = [record(f"id-{i}", "managed")[0] for i in range(6)]

        def fake_validate(item, previous_state=None):
            item = dict(item)
            item.update(
                {
                    "status": "online",
                    "latency": 5,
                    "last_checked": "2026-09-06T12:00:00Z",
                    "last_seen": "2026-09-06T12:00:00Z",
                    "success_count": 1,
                    "fail_count": 0,
                    "consecutive_failures": 0,
                    "country": "US",
                    "country_name": "United States",
                    "should_remove": False,
                }
            )
            return item

        with mock.patch("generator.validate_server", side_effect=fake_validate):
            output, stats = generator.run_validation(records, {})

        self.assertEqual(len(output), 6)
        self.assertEqual(stats["selected"], 2)
        self.assertEqual(sum(1 for item in output if item["status"] == "online"), 2)
        self.assertEqual(sum(1 for item in output if item["status"] == "unknown"), 4)

    def test_should_remove_tombstone_is_kept_in_snapshot(self):
        generator.MAX_VALIDATIONS_PER_RUN = 1
        item, _ = record("dead-1", "managed")

        def dead_validate(value, previous_state=None):
            value = dict(value)
            value.update(
                {
                    "status": "offline",
                    "latency": None,
                    "last_checked": "2026-09-06T12:00:00Z",
                    "success_count": 0,
                    "fail_count": 3,
                    "consecutive_failures": 3,
                    "country": "US",
                    "country_name": "United States",
                    "should_remove": True,
                }
            )
            return value

        with mock.patch("generator.validate_server", side_effect=dead_validate):
            output, _ = generator.run_validation([item], {})

        self.assertEqual(len(output), 1)
        self.assertTrue(output[0]["should_remove"])
        self.assertEqual(output[0]["consecutive_failures"], 3)

    def test_unknown_and_removed_are_not_written_to_public_subscription(self):
        online, _ = record("online-1", "managed")
        online["status"] = "online"
        unknown, _ = record("unknown-1", "managed")
        unknown["status"] = "unknown"
        removed, _ = record("removed-1", "managed")
        removed["status"] = "offline"
        removed["should_remove"] = True

        with tempfile.TemporaryDirectory() as tempdir:
            old = {
                "OUTPUT_FILES": generator.OUTPUT_FILES,
                "SUB_PATH": generator.SUB_PATH,
                "SUB_B64_PATH": generator.SUB_B64_PATH,
                "SECURE_PATH": generator.SECURE_PATH,
                "ALL_PATH": generator.ALL_PATH,
            }
            try:
                generator.OUTPUT_FILES = {
                    "vless": os.path.join(tempdir, "vless.txt"),
                    "vmess": os.path.join(tempdir, "vmess.txt"),
                    "trojan": os.path.join(tempdir, "trojan.txt"),
                    "shadowsocks": os.path.join(tempdir, "ss.txt"),
                    "hysteria2": os.path.join(tempdir, "hy2.txt"),
                }
                generator.SUB_PATH = os.path.join(tempdir, "sub.txt")
                generator.SUB_B64_PATH = os.path.join(tempdir, "sub-base64.txt")
                generator.SECURE_PATH = os.path.join(tempdir, "secure.txt")
                generator.ALL_PATH = os.path.join(tempdir, "all.txt")
                generator.write_output_files([online, unknown, removed])
                with open(generator.SUB_PATH, encoding="utf-8") as handle:
                    text = handle.read()
            finally:
                for key, value in old.items():
                    setattr(generator, key, value)

        self.assertIn("online-1", text)
        self.assertNotIn("unknown-1", text)
        self.assertNotIn("removed-1", text)


if __name__ == "__main__":
    unittest.main()
