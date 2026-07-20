"""Acceptance and concurrency tests for the non-blocking GitHub client."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from github_client import GitHubClient
import server


def fake_response(payload):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class TestGitHubTransport(unittest.TestCase):
    def test_token_prefers_env_then_secret_file(self):
        with tempfile.TemporaryDirectory() as folder:
            token_file = Path(folder) / "token"
            token_file.write_text(" file-token\n", encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_TOKEN_FILE": str(token_file)}, clear=True):
                self.assertEqual(GitHubClient.token(), "file-token")
            with patch.dict(os.environ, {
                "GITHUB_TOKEN": " direct ", "GITHUB_TOKEN_FILE": str(token_file)
            }, clear=True):
                self.assertEqual(GitHubClient.token(), "direct")

    def test_request_sends_auth_and_handles_network_errors(self):
        client = GitHubClient()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=True), patch(
            "urllib.request.urlopen", return_value=fake_response({"ok": True})
        ) as opened:
            self.assertEqual(client.request("https://api.github.com/test"), {"ok": True})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "token secret")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=True), patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("down")
        ):
            self.assertIsNone(client.request("https://api.github.com/test"))

    def test_request_rejects_oversized_responses(self):
        client = GitHubClient(max_response_bytes=1024)
        response = MagicMock()
        response.read.return_value = b"x" * 1025
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=True), patch(
            "urllib.request.urlopen", return_value=response
        ):
            self.assertIsNone(client.request("https://api.github.com/test"))
        response.read.assert_called_once_with(1025)

    def test_change_extraction_is_null_safe_and_bounded(self):
        client = GitHubClient(max_files=2, max_patch_chars=5, max_total_patch_chars=7)
        payload = {
            "sha": "abcdef1234",
            "files": [
                {"filename": "one.py", "status": "modified", "additions": None,
                 "deletions": "bad", "patch": "123456789"},
                {"filename": "two.py", "status": "added", "additions": 2,
                 "deletions": 0, "patch": "abcdef"},
                {"filename": "ignored.py", "patch": "ignored"},
            ],
        }
        changes = client._extract_changes(payload)
        self.assertEqual(changes["head_sha"], "abcdef1234")
        self.assertEqual(len(changes["changed_files"]), 2)
        self.assertEqual(changes["prompt_files"][0]["patch"], "12345")
        self.assertEqual(changes["prompt_files"][1]["patch"], "ab")

    def test_initial_change_fetch_ignores_misleading_commit_messages(self):
        client = GitHubClient()
        commits = [
            {"sha": "new", "commit": {"message": "LIE: project is finished"}},
            {"sha": "old", "commit": {"message": "another misleading message"}},
        ]
        compare = {"head_commit": {"sha": "new"}, "files": [{
            "filename": "src/app.py", "status": "modified", "additions": 3,
            "deletions": 1, "patch": "+actual code",
        }]}
        with patch.object(client, "request", side_effect=[commits, compare]):
            changes = client.fetch_change_context("owner", "repo", "main")
        serialized = json.dumps(changes)
        self.assertIn("actual code", serialized)
        self.assertNotIn("project is finished", serialized)
        self.assertNotIn("misleading message", serialized)

    def test_previous_head_uses_compare_then_falls_back_to_latest_detail(self):
        client = GitHubClient()
        latest = {"sha": "new", "files": [{"filename": "README.md", "patch": "+now"}]}
        with patch.object(client, "request", side_effect=[None, latest]) as request:
            changes = client.fetch_change_context("owner", "repo", "feature/x", "old")
        self.assertEqual(changes["head_sha"], "new")
        self.assertIn("old...feature%2Fx", request.call_args_list[0].args[0])


class TestRecency(unittest.TestCase):
    def test_boundaries(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        iso = lambda days: (now - timedelta(days=days)).isoformat()
        self.assertEqual(GitHubClient.classify_recency(iso(6), now), "active")
        self.assertEqual(GitHubClient.classify_recency(iso(7), now), "maintain")
        self.assertEqual(GitHubClient.classify_recency(iso(29), now), "maintain")
        self.assertEqual(GitHubClient.classify_recency(iso(30), now), "stalled")
        self.assertEqual(GitHubClient.classify_recency(None, now), "stalled")


class TestNonBlockingRefresh(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"GITHUB_TOKEN": "token"}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    @staticmethod
    def repo(index=0):
        return {
            "full_name": f"owner/repo-{index}",
            "name": None,
            "description": None,
            "language": None,
            "html_url": None,
            "default_branch": None,
            "pushed_at": None,
        }

    def test_first_load_returns_promptly_while_transport_is_blocked(self):
        client = GitHubClient()
        release = threading.Event()

        def blocked_fetch():
            release.wait(2)
            return [self.repo()]

        with patch.object(client, "fetch_all_repos", side_effect=blocked_fetch), patch.object(
            client, "fetch_change_context", return_value=None
        ):
            started = time.perf_counter()
            snapshot = client.get_repos()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.15)
            self.assertEqual(snapshot["state"], "refreshing")
            self.assertEqual(snapshot["repos"], [])
            release.set()
            self.assertTrue(client.wait_for_refresh())
        self.assertEqual(client.state()["state"], "ready")
        self.assertEqual(client.get_repos()["repos"][0]["name"], "repo-0")

    def test_overlapping_requests_start_exactly_one_refresh(self):
        client = GitHubClient()
        release = threading.Event()
        calls = 0
        lock = threading.Lock()

        def blocked_fetch():
            nonlocal calls
            with lock:
                calls += 1
            release.wait(2)
            return []

        with patch.object(client, "fetch_all_repos", side_effect=blocked_fetch):
            with ThreadPoolExecutor(max_workers=12) as pool:
                snapshots = list(pool.map(lambda _: client.get_repos(), range(30)))
            self.assertEqual(calls, 1)
            self.assertEqual({item["state"] for item in snapshots}, {"refreshing"})
            release.set()
            self.assertTrue(client.wait_for_refresh())

    def test_commit_enrichment_never_exceeds_four_workers(self):
        client = GitHubClient(max_workers=20)
        active = 0
        peak = 0
        lock = threading.Lock()

        def changes(*_args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return []

        with patch.object(client, "fetch_all_repos", return_value=[self.repo(i) for i in range(16)]), patch.object(
            client, "fetch_change_context", side_effect=changes
        ):
            client.get_repos()
            self.assertTrue(client.wait_for_refresh())
        self.assertLessEqual(peak, 4)
        self.assertGreater(peak, 1)

    def test_unchanged_repository_avoids_change_request_after_restart(self):
        pushed_at = "2026-07-20T12:00:00Z"
        known = {"owner/repo-0": {
            "head_sha": "a" * 40,
            "source_pushed_at": pushed_at,
            "changed_files": [{"path": "saved.py", "status": "modified"}],
        }}
        client = GitHubClient(insight_loader=lambda: known)
        repo = self.repo()
        repo["pushed_at"] = pushed_at
        with patch.object(client, "fetch_all_repos", return_value=[repo]), patch.object(
            client, "fetch_change_context"
        ) as fetch_changes:
            client.get_repos()
            self.assertTrue(client.wait_for_refresh())
        fetch_changes.assert_not_called()
        enriched = client.get_repos()["repos"][0]
        self.assertEqual(enriched["change_status"], "unchanged")
        self.assertEqual(enriched["head_sha"], "a" * 40)

    def test_stale_snapshot_is_served_during_refresh(self):
        client = GitHubClient(cache_ttl=0)
        cached = {"repos": [{"full_name": "cached/repo"}], "status": "ok", "banner": None, "ts": 1}
        client._cache = {"repos": cached, "ts": 1}
        release = threading.Event()

        def blocked_fetch():
            release.wait(2)
            return []

        with patch.object(client, "fetch_all_repos", side_effect=blocked_fetch):
            snapshot = client.get_repos()
            self.assertEqual(snapshot["repos"], cached["repos"])
            self.assertEqual(snapshot["state"], "refreshing")
            release.set()
            client.wait_for_refresh()

    def test_failure_becomes_terminal_until_retry_ttl(self):
        client = GitHubClient(failure_ttl=60)
        with patch.object(client, "fetch_all_repos", return_value=None) as fetch:
            self.assertEqual(client.get_repos()["state"], "refreshing")
            client.wait_for_refresh()
            self.assertEqual(client.state()["state"], "error")
            snapshot = client.get_repos()
            self.assertEqual(snapshot["state"], "error")
            self.assertEqual(fetch.call_count, 1)

    def test_force_refresh_bypasses_failure_ttl(self):
        client = GitHubClient(failure_ttl=60)
        with patch.object(client, "fetch_all_repos", return_value=None) as fetch:
            client.get_repos()
            client.wait_for_refresh()
            client.get_repos(force=True)
            client.wait_for_refresh()
            self.assertEqual(fetch.call_count, 2)


class TestHubStateRoute(unittest.TestCase):
    def test_route_returns_public_refresh_state(self):
        handler = MagicMock()
        handler.path = "/api/hub/state"
        handler._reject_unallowed_host.return_value = False
        state = {"state": "refreshing", "version": 2, "updated_at": None, "has_data": False}
        with patch.object(server._GITHUB_CLIENT, "state", return_value=state), patch.object(
            server._OLLAMA_CLIENT, "state", return_value={"state": "idle", "pending": 0}
        ):
            server.Handler.do_GET(handler)
        status, content_type, body = handler._respond.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        payload = json.loads(body)
        self.assertEqual(payload["state"], "refreshing")
        self.assertEqual(payload["github_state"], "refreshing")
        self.assertEqual(payload["insight_state"], "idle")

    def test_project_invalidation_forces_fresh_change_window(self):
        pushed_at = "2026-07-20T12:00:00Z"
        known = {"owner/repo-0": {"head_sha": "a" * 40, "source_pushed_at": pushed_at}}
        client = GitHubClient(insight_loader=lambda: known)
        repo_data = TestNonBlockingRefresh.repo()
        repo_data["pushed_at"] = pushed_at
        client.invalidate_repo("owner/repo-0")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "token"}, clear=True), patch.object(
            client, "fetch_all_repos", return_value=[repo_data]
        ), patch.object(client, "fetch_change_context", return_value=None) as changes:
            client.get_repos(force=True)
            client.wait_for_refresh()
        changes.assert_called_once_with("owner", "repo-0", "main", "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
