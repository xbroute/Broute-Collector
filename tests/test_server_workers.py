import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generator
import generator_managed as managed
try:
    from server_workers import collect_generation
except ImportError:
    collect_generation = None


class MemoryState:
    """Only isolates the filesystem pipeline; SQL transactions have separate real-PG tests."""
    def __init__(self):
        self.documents = {}
    def get(self, name, default=None):
        return copy.deepcopy(self.documents.get(name, default))
    def put(self, name, value):
        self.documents[name] = copy.deepcopy(value)
    def guard(self):
        pass
    @contextmanager
    def transaction(self):
        yield


class RepeatedCollectionTests(unittest.TestCase):
    def test_managed_wrapper_does_not_accumulate_source_counts_between_runs(self):
        observed = []
        with patch.object(managed, "managed_sources", return_value=[{"id": "test"}]), \
             patch.object(generator, "count_active_sources", return_value=7) as count, \
             patch.object(generator, "main", side_effect=lambda: observed.append(generator.count_active_sources())):
            managed.main()
            managed.main()
            self.assertIs(generator.count_active_sources, count)
        self.assertEqual(observed, [8, 8])

    @unittest.skipIf(collect_generation is None, "install requirements-server.txt")
    def test_complete_generation_switch_and_manual_sources_survive_repeated_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "data").mkdir(parents=True)
            (repo / "data/manual.txt").write_text("vless://test-id@8.8.8.8:443?security=tls\n")
            (repo / "data/sources.json").write_text(json.dumps({"manual_sources": [{"name": "manual", "path": "data/manual.txt", "enabled": True}]}))
            state = MemoryState()
            with patch.object(generator, "validate_server", side_effect=lambda row, prev: dict(row, status="online", latency=1)):
                collect_generation(state, root / "output", source_repo=repo)
                first = (root / "output/current").resolve()
                collect_generation(state, root / "output", source_repo=repo)
            current = root / "output/current"
            self.assertNotEqual(current.resolve(), first)
            self.assertEqual(len(json.loads((current / "data/servers.json").read_text())), 1)
            self.assertIn("vless://", (current / "data/sub.txt").read_text())
            self.assertEqual(len(state.get("collector_snapshot")["servers"]), 1)
            with patch.object(generator, "main", side_effect=RuntimeError("disk failure")):
                prior = current.resolve()
                with self.assertRaises(RuntimeError):
                    collect_generation(state, root / "output", source_repo=repo)
                self.assertEqual(current.resolve(), prior)
