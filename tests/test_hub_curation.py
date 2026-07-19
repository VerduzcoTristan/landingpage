"""Tests for the curation-layer data module in server.py (FR-003).

Covers:
  - load_hub() on missing file
  - save_hub()/load_hub() round-trip (atomic write, normalised entry)
  - _normalise_hub() coercion rules
  - load_hub() legacy LIST-format migration (keyed by full_name)
  - update_hub() actions: update / toggle-hide / delete / error paths

The module computes HUB_FILE = DATA_DIR / "projects.json" at import time, so we
set os.environ["DATA_DIR"] to a fresh temp dir and import server fresh for each
test (via importlib) to keep tests isolated.
"""

import importlib
import json
import os
import sys
import tempfile
import unittest

# Ensure the project root is importable as a package path.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def load_server_with_temp_data_dir(tmp_path):
    """Set DATA_DIR to tmp_path and import (or reload) server fresh."""
    os.environ["DATA_DIR"] = tmp_path
    import server
    return importlib.reload(server)


class FormGet:
    """Callable wrapper mimicking a POST form dict's .get() callable.

    server.update_hub(action, get) calls get("key", default) directly, so `get`
    must be a callable that behaves like dict.get (e.g. request.form.get).
    """

    def __init__(self, d):
        self._d = d

    def __call__(self, key, default=""):
        return self._d.get(key, default)


class TestHubCuration(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = self._tmp.name
        self.server = load_server_with_temp_data_dir(self.tmp_path)

    def tearDown(self):
        self._tmp.cleanup()

    # 1. load_hub() on a missing file returns {}
    def test_load_hub_missing_file_returns_empty_dict(self):
        hub = self.server.load_hub()
        self.assertEqual(hub, {})
        self.assertIsInstance(hub, dict)

    # 2. Round-trip save/load with all optional fields
    def test_save_load_roundtrip_all_fields(self):
        entry = {
            "goal": "g",
            "whats_next": "n",
            "status_override": "done",
            "live_url": "u",
            "local_path": "p",
            "hidden": True,
            "order": 3,
        }
        self.server.save_hub({"owner/repo": entry})

        # Atomic write produced a valid JSON file on disk.
        hub_file = self.server.HUB_FILE
        self.assertTrue(hub_file.exists(), "HUB_FILE should exist after save_hub")
        raw_text = hub_file.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)  # must be valid JSON
        self.assertIn("owner/repo", parsed)

        # No leftover .tmp file from the atomic write.
        tmp_file = hub_file.with_suffix(".tmp")
        self.assertFalse(tmp_file.exists(), "atomic write should not leave a .tmp file")

        loaded = self.server.load_hub()
        self.assertEqual(loaded, {"owner/repo": entry})

    # 3. _normalise_hub coercion rules
    def test_normalise_hub_rejects_non_dict(self):
        self.assertEqual(self.server._normalise_hub([]), {})
        self.assertEqual(self.server._normalise_hub("nope"), {})
        self.assertEqual(self.server._normalise_hub(None), {})
        self.assertEqual(self.server._normalise_hub(42), {})

    def test_normalise_hub_strips_whitespace_and_lowercases_status(self):
        raw = {
            "  owner/repo  ": {
                "goal": "  my goal  ",
                "whats_next": "\tnxt\n",
                "status_override": "  DONE  ",
                "live_url": "  https://x  ",
                "local_path": "  /p  ",
                "hidden": "truthy",  # non-empty string is truthy -> bool() True
                "order": "7",
            }
        }
        clean = self.server._normalise_hub(raw)
        self.assertIn("owner/repo", clean)  # key stripped
        entry = clean["owner/repo"]
        self.assertEqual(entry["goal"], "my goal")
        self.assertEqual(entry["whats_next"], "nxt")
        self.assertEqual(entry["status_override"], "done")  # lowercased + trimmed
        self.assertEqual(entry["live_url"], "https://x")
        self.assertEqual(entry["local_path"], "/p")
        self.assertTrue(entry["hidden"])
        self.assertEqual(entry["order"], 7)
        self.assertIsInstance(entry["order"], int)

    def test_normalise_hub_status_non_done_becomes_empty(self):
        raw = {"o/r": {"status_override": "active"}}
        clean = self.server._normalise_hub(raw)
        self.assertEqual(clean["o/r"]["status_override"], "")

        raw2 = {"o/r": {"status_override": "DONE"}}
        clean2 = self.server._normalise_hub(raw2)
        self.assertEqual(clean2["o/r"]["status_override"], "done")

    def test_normalise_hub_hidden_coercion_and_order_default(self):
        # hidden must be a real bool; order defaults to int 0 when missing
        raw = {"o/r": {"hidden": False}}
        clean = self.server._normalise_hub(raw)
        self.assertFalse(clean["o/r"]["hidden"])
        self.assertIsInstance(clean["o/r"]["hidden"], bool)
        self.assertEqual(clean["o/r"]["order"], 0)

        # order "0" string -> 0 int; order None -> 0
        raw2 = {"o/r": {"order": "0"}}
        self.assertEqual(self.server._normalise_hub(raw2)["o/r"]["order"], 0)
        raw3 = {"o/r": {"order": None}}
        self.assertEqual(self.server._normalise_hub(raw3)["o/r"]["order"], 0)

    def test_normalise_hub_skips_empty_keys(self):
        raw = {"": {"goal": "x"}, "   ": {"goal": "y"}, "ok/repo": {"goal": "z"}}
        clean = self.server._normalise_hub(raw)
        self.assertEqual(set(clean.keys()), {"ok/repo"})

    # 4. Migration of legacy LIST-format projects.json
    def test_load_hub_migrates_legacy_list_format(self):
        legacy = [
            {
                "name": "Foo",
                "description": "Legacy goal",
                "url": "https://foo.example",
                "repo_url": "https://github.com/tristan/foo",
                "status": "active",
                "order": 2,
                "hidden": True,
            },
            {
                # entry with no repo_url -> name fallback key
                "name": "bar/baz",
                "description": "No repo url here",
                "url": "https://bar.example",
                "status": "archived",
                "order": 5,
                "hidden": False,
            },
        ]
        hub_file = self.server.HUB_FILE
        hub_file.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

        migrated = self.server.load_hub()

        # Keyed by full_name derived from repo_url
        self.assertIn("tristan/foo", migrated)
        foo = migrated["tristan/foo"]
        self.assertEqual(foo["goal"], "Legacy goal")        # description -> goal
        self.assertEqual(foo["live_url"], "https://foo.example")  # url -> live_url
        self.assertTrue(foo["hidden"])
        self.assertEqual(foo["order"], 2)
        self.assertEqual(foo["whats_next"], "")
        self.assertEqual(foo["local_path"], "")
        # Old status NOT mapped to status_override
        self.assertEqual(foo["status_override"], "")

        # Name fallback key for entry without repo_url
        self.assertIn("bar/baz", migrated)
        self.assertEqual(migrated["bar/baz"]["goal"], "No repo url here")
        self.assertEqual(migrated["bar/baz"]["status_override"], "")

        # The file on disk was rewritten to the new dict format.
        on_disk = json.loads(hub_file.read_text(encoding="utf-8"))
        self.assertIsInstance(on_disk, dict, "legacy list should be rewritten to dict")
        self.assertIn("tristan/foo", on_disk)

        # A second load_hub() returns the dict directly (no re-migration).
        migrated2 = self.server.load_hub()
        self.assertEqual(migrated2, migrated)

    # 5. update_hub actions
    def test_update_hub_update_creates_entry(self):
        get = {
            "full_name": "owner/repo",
            "goal": "x",
            "whats_next": "y",
            "status_override": "done",
            "live_url": "",
            "local_path": "",
            "hidden": "1",
            "order": "5",
        }
        msg = self.server.update_hub("update", FormGet(get))
        self.assertEqual(msg, "Hub updated.")
        hub = self.server.load_hub()
        self.assertIn("owner/repo", hub)
        entry = hub["owner/repo"]
        self.assertEqual(entry["goal"], "x")
        self.assertEqual(entry["whats_next"], "y")
        self.assertEqual(entry["status_override"], "done")
        self.assertTrue(entry["hidden"])   # "1" -> True
        self.assertEqual(entry["order"], 5)

    def test_update_hub_toggle_hide_flips(self):
        # Seed an entry hidden=False
        self.server.save_hub({"owner/repo": {"goal": "", "whats_next": "",
                                             "status_override": "", "live_url": "",
                                             "local_path": "", "hidden": False, "order": 0}})
        msg = self.server.update_hub("toggle-hide", FormGet({"full_name": "owner/repo"}))
        self.assertEqual(msg, "Hub updated.")
        self.assertTrue(self.server.load_hub()["owner/repo"]["hidden"])

        # Toggle again -> back to False
        self.server.update_hub("toggle-hide", FormGet({"full_name": "owner/repo"}))
        self.assertFalse(self.server.load_hub()["owner/repo"]["hidden"])

    def test_update_hub_delete_removes(self):
        self.server.save_hub({"owner/repo": {"goal": "g", "whats_next": "",
                                             "status_override": "", "live_url": "",
                                             "local_path": "", "hidden": False, "order": 0}})
        msg = self.server.update_hub("delete", FormGet({"full_name": "owner/repo"}))
        self.assertEqual(msg, "Hub updated.")
        self.assertNotIn("owner/repo", self.server.load_hub())

    def test_update_hub_missing_full_name_returns_error(self):
        msg = self.server.update_hub("update", FormGet({"goal": "x"}))
        self.assertEqual(msg, "Repository identifier missing.")

    def test_update_hub_unknown_action_returns_error(self):
        msg = self.server.update_hub("frobnicate", FormGet({"full_name": "owner/repo"}))
        self.assertEqual(msg, "Unknown action.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
