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

    def test_commit_normalization_is_null_safe(self):
        client = GitHubClient()
        payload = [
            {"sha": None, "commit": {"message": None}},
            {"sha": "abcdef1234", "commit": {"message": "Subject\nbody"}},
        ]
        with patch.object(client, "request", return_value=payload):
            commits = client.fetch_recent_commits("owner", "repo", "main")
        self.assertEqual(commits[0], {"sha": "", "subject": "", "body": ""})
        self.assertEqual(commits[1]["sha"], "abcdef12")
        self.assertEqual(commits[1]["subject"], "Subject")


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
            client, "fetch_recent_commits", return_value=[]
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

        def commits(*_args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return []

        with patch.object(client, "fetch_all_repos", return_value=[self.repo(i) for i in range(16)]), patch.object(
            client, "fetch_recent_commits", side_effect=commits
        ):
            client.get_repos()
            self.assertTrue(client.wait_for_refresh())
        self.assertLessEqual(peak, 4)
        self.assertGreater(peak, 1)

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
        with patch.object(server._GITHUB_CLIENT, "state", return_value=state):
            server.Handler.do_GET(handler)
        status, content_type, body = handler._respond.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(body), state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
