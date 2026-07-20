import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hub_store import InsightStore, normalise_insight, normalise_insights


class TestInsightNormalisation(unittest.TestCase):
    def test_unknown_and_sensitive_fields_are_discarded(self):
        clean = normalise_insight({
            "current_state": "  Current  ",
            "next_step": " Next ",
            "confidence": "HIGH",
            "state": "ready",
            "patch": "secret diff",
            "prompt": "secret prompt",
            "model": "private model",
            "token": "secret",
        })
        self.assertEqual(clean["current_state"], "Current")
        self.assertEqual(clean["next_step"], "Next")
        self.assertEqual(clean["confidence"], "high")
        self.assertNotIn("patch", clean)
        self.assertNotIn("prompt", clean)
        self.assertNotIn("model", clean)
        self.assertNotIn("token", clean)

    def test_malformed_entries_degrade_independently(self):
        clean = normalise_insights({
            "ok/repo": {"current_state": "Ready", "state": "ready"},
            "bad/repo": ["wrong"],
            "": {"current_state": "ignored"},
        })
        self.assertEqual(set(clean), {"ok/repo", "bad/repo"})
        self.assertEqual(clean["ok/repo"]["current_state"], "Ready")
        self.assertEqual(clean["bad/repo"]["state"], "unavailable")

    def test_changed_files_and_history_are_bounded(self):
        clean = normalise_insight({
            "changed_files": [
                {"path": f"src/{index}.py", "status": "modified", "additions": index}
                for index in range(40)
            ],
            "history": [
                {"current_state": f"state {index}", "next_step": "next"}
                for index in range(12)
            ],
        })
        self.assertEqual(len(clean["changed_files"]), 20)
        self.assertEqual(len(clean["history"]), 5)


class TestInsightStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "project-insights.json"
        self.store = InsightStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def record(sha: str, state: str) -> dict:
        return {
            "current_state": state,
            "next_step": f"next {state}",
            "confidence": "medium",
            "head_sha": sha,
            "source_pushed_at": "2026-07-20T12:00:00Z",
            "changed_files": [{
                "path": "server.py", "status": "modified", "additions": 2, "deletions": 1,
            }],
            "additions": 2,
            "deletions": 1,
            "generated_at": "2026-07-20T12:01:00Z",
            "state": "ready",
        }

    def test_missing_and_corrupt_files_return_empty(self):
        self.assertEqual(self.store.load(), {})
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.store.load(), {})

    def test_atomic_round_trip_strips_raw_context(self):
        record = self.record("a" * 40, "first")
        record["patches"] = ["raw patch must not persist"]
        stored = self.store.put_generated("owner/repo", record)
        self.assertEqual(stored["current_state"], "first")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("raw patch", raw)
        self.assertEqual(self.store.load()["owner/repo"]["head_sha"], "a" * 40)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_history_keeps_five_distinct_predecessors(self):
        for index in range(8):
            self.store.put_generated("owner/repo", self.record(str(index) * 40, f"state {index}"))
        entry = self.store.load()["owner/repo"]
        self.assertEqual(entry["current_state"], "state 7")
        self.assertEqual(len(entry["history"]), 5)
        self.assertEqual(entry["history"][0]["current_state"], "state 6")
        self.assertEqual(entry["history"][-1]["current_state"], "state 2")

    def test_same_record_does_not_duplicate_history(self):
        record = self.record("a" * 40, "same")
        self.store.put_generated("owner/repo", record)
        self.store.put_generated("owner/repo", record)
        self.assertEqual(self.store.load()["owner/repo"]["history"], [])

    def test_state_change_preserves_last_good_content(self):
        self.store.put_generated("owner/repo", self.record("a" * 40, "last good"))
        updated = self.store.set_state("owner/repo", "stale", "ollama")
        self.assertEqual(updated["current_state"], "last good")
        self.assertEqual(updated["state"], "stale")
        self.assertEqual(updated["failure_kind"], "ollama")

    def test_concurrent_updates_keep_valid_json(self):
        def write(index):
            self.store.put_generated(
                f"owner/repo-{index}", self.record(f"{index:040d}"[-40:], f"state {index}")
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(30)))
        loaded = self.store.load()
        self.assertEqual(len(loaded), 30)
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_remove_is_scoped_to_one_repository(self):
        self.store.put_generated("owner/one", self.record("1" * 40, "one"))
        self.store.put_generated("owner/two", self.record("2" * 40, "two"))
        self.store.remove("owner/one")
        self.assertEqual(set(self.store.load()), {"owner/two"})


if __name__ == "__main__":
    unittest.main()
