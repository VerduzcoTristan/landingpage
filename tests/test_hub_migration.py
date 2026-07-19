"""Tests for the one-time Hub file migration helper in server.py (Step 1.7).

Covers _migrate_hub_file(), which renames a legacy ``projects.json`` into the
new ``curation.json`` (HUB_FILE) path exactly once.

The module computes HUB_FILE = DATA_DIR / "curation.json" at import time and
runs _migrate_hub_file() once at import, so we set os.environ["DATA_DIR"] to a
fresh temp dir and import (or reload) server fresh for each test to keep them
isolated.
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


class TestMigrateHubFile(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = self._tmp.name
        self.server = load_server_with_temp_data_dir(self.tmp_path)

    def tearDown(self):
        self._tmp.cleanup()

    # 1. Legacy projects.json present, HUB_FILE absent -> rename occurs.
    def test_migrates_legacy_projects_json_into_curation_json(self):
        legacy = self.server.DATA_DIR / "projects.json"
        hub_file = self.server.HUB_FILE  # curation.json
        content = {"a/b": {"goal": "x"}}
        legacy.write_text(json.dumps(content), encoding="utf-8")
        self.assertFalse(hub_file.exists(), "HUB_FILE should not exist before migration")

        self.server._migrate_hub_file()

        # curation.json now exists with the same content.
        self.assertTrue(hub_file.exists(), "HUB_FILE should exist after migration")
        self.assertEqual(json.loads(hub_file.read_text(encoding="utf-8")), content)
        # Legacy file is gone (renamed, not copied).
        self.assertFalse(legacy.exists(), "legacy projects.json should be removed after rename")

    # 2. No legacy, no HUB_FILE -> no-op, no crash, no files created.
    def test_no_legacy_no_hub_file_is_noop(self):
        legacy = self.server.DATA_DIR / "projects.json"
        hub_file = self.server.HUB_FILE
        self.assertFalse(legacy.exists())
        self.assertFalse(hub_file.exists())

        # Must not raise.
        self.server._migrate_hub_file()

        self.assertFalse(legacy.exists(), "no legacy file should be created")
        self.assertFalse(hub_file.exists(), "no HUB_FILE should be created when nothing to migrate")

    # 3. HUB_FILE already exists, legacy present -> legacy preserved (no overwrite).
    def test_existing_hub_file_preserves_legacy(self):
        legacy = self.server.DATA_DIR / "projects.json"
        hub_file = self.server.HUB_FILE
        legacy_content = {"a/b": {"goal": "legacy"}}
        hub_content = {"c/d": {"goal": "current"}}
        legacy.write_text(json.dumps(legacy_content), encoding="utf-8")
        hub_file.write_text(json.dumps(hub_content), encoding="utf-8")

        self.server._migrate_hub_file()

        # HUB_FILE unchanged (not overwritten by legacy).
        self.assertEqual(
            json.loads(hub_file.read_text(encoding="utf-8")),
            hub_content,
            "existing HUB_FILE must not be overwritten by legacy migration",
        )
        # Legacy preserved (simulates re-deploy / partial migration safety).
        self.assertTrue(legacy.exists(), "legacy projects.json must be preserved when HUB_FILE exists")
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8")), legacy_content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
