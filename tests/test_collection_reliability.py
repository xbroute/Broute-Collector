import json
import os
import pathlib
import tempfile
import threading
import unittest
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import collector
import common
import generator
import generator_managed
from deduplicator import canonical_raw_connection_key
from generate_online_subscription import write_online_subscription


def previous_server():
    raw = "vless://id@8.8.8.8:443?security=tls#label"
    return {"id": canonical_raw_connection_key(raw, "vless"), "raw": raw,
            "protocol": "vless", "address": "8.8.8.8", "port": 443,
            "valid": True, "status": "online", "secure": True,
            "source_name": "one", "source_url": "https://example.com/one",
            "last_checked": "2026-09-30T12:00:00Z", "success_count": 5}


class CollectionReliabilityTests(unittest.TestCase):
    def test_bounded_parallel_fetch_keeps_allowlist_order(self):
        barrier = threading.Barrier(2)
        threads = set()
        sources = {"subscription_sources": [
            {"url": "https://example.com/one", "enabled": True},
            {"url": "https://example.com/two", "enabled": True},
        ], "settings": {"fetch_workers": 2}}

        def fetch(url, *args):
            threads.add(threading.get_ident())
            barrier.wait(timeout=2)
            return url

        with patch.object(collector, "load_json", return_value=sources), patch.object(collector, "fetch_url", side_effect=fetch):
            result = collector.collect()
        self.assertEqual(len(threads), 2)
        self.assertEqual([item["content"] for item in result], [item["url"] for item in sources["subscription_sources"]])

    def test_failed_request_is_distinct_from_successful_empty_response(self):
        sources = {"subscription_sources": [
            {"url": "https://example.com/one", "enabled": True},
            {"url": "https://example.com/two", "enabled": True},
        ]}

        def fetch(url, *args):
            if url.endswith("one"):
                raise collector.SourceFetchError("down")
            return ""

        with patch.object(collector, "load_json", return_value=sources), patch.object(collector, "fetch_url", side_effect=fetch):
            result = collector.collect()
        self.assertTrue(result[0]["fetch_failed"])
        self.assertFalse(result[1].get("fetch_failed", False))
        self.assertEqual(result[1]["content"], "")

    def test_managed_failure_preserves_only_opaque_source_reference(self):
        secret = "https://example.com/sub/private-secret"
        with patch.object(generator_managed, "fetch_subscription", side_effect=generator_managed.SourceValidationError("down")):
            result = generator_managed._fetch_managed_source({"id": "safe-id", "url": secret})
        self.assertTrue(result["fetch_failed"])
        self.assertEqual(result["source_url"], "managed://safe-id")
        self.assertNotIn(secret, json.dumps(result))

    def test_failure_history_is_unavailable_and_expires(self):
        previous = previous_server()
        failed = [{"source_url": previous["source_url"], "fetch_failed": True}]
        retained = generator.retain_unavailable_sources([previous], [], failed)
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["status"], "unknown")
        self.assertTrue(retained[0]["source_unavailable"])
        self.assertEqual(retained[0]["success_count"], 5)
        retained[0]["source_failure_since"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertEqual(generator.retain_unavailable_sources(retained, [], failed), [])

    def test_successful_empty_or_disabled_source_removes_old_configs(self):
        previous = previous_server()
        for results in ([], [{"source_url": previous["source_url"], "content": ""}],
                        [{"source_url": previous["source_url"], "fetch_failed": True},
                         {"source_url": previous["source_url"], "content": ""}]):
            with self.subTest(results=results):
                self.assertEqual(generator.retain_unavailable_sources([previous], [], results), [])

    def test_failed_source_does_not_duplicate_a_current_connection(self):
        previous = previous_server()
        self.assertEqual(generator.retain_unavailable_sources([previous], [previous],
                         [{"source_url": previous["source_url"], "fetch_failed": True}]), [])

    def test_shared_record_keeps_only_unavailable_source_memberships(self):
        previous = previous_server()
        previous["sources"] = [{"source_name": "one", "source_url": previous["source_url"]},
                               {"source_name": "deleted", "source_url": "https://example.com/deleted"}]
        retained = generator.retain_unavailable_sources([previous], [],
                   [{"source_url": previous["source_url"], "fetch_failed": True}])
        self.assertEqual(len(retained[0]["sources"]), 1)

    def test_generated_failed_source_history_never_enters_public_feeds(self):
        previous = previous_server()
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                pathlib.Path("data").mkdir()
                common.save_json("data/servers.json", [previous])
                failed = [{"source_name": "one", "source_url": previous["source_url"],
                           "content": "", "fetch_failed": True}]
                with patch.object(generator, "collect", return_value=failed), patch.object(generator, "count_active_sources", return_value=1):
                    generator.main()
                self.assertEqual(write_online_subscription(), 0)
                stored = common.load_json("data/servers.json", [])
                self.assertTrue(stored[0]["source_unavailable"])
                self.assertEqual(pathlib.Path("data/sub.txt").read_text(), "")
                self.assertEqual(common.load_json("data/status.json", {})["collection"]["failed"], 1)
            finally:
                os.chdir(old_cwd)

    def test_json_replace_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "snapshot.json"
            common.save_json(str(path), {"old": True})
            with patch.object(common.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    common.save_json(str(path), {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(len(list(pathlib.Path(directory).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
