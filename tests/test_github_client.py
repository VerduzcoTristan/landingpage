"""Tests for the GitHub client (Hub data source) in server.py (Step 1.2).

Covers:
  - classify_recency() boundary cases (active / maintain / stalled)
  - _gh_token() env reading + stripping
  - _gh_request() success + failure modes (HTTPError / URLError / bad JSON / no token)
  - fetch_all_repos() pagination + failure modes
  - fetch_recent_commits() parsing + failure modes
  - get_hub_repos() caching, stale-while-revalidate, token-missing, error paths

No real network calls: urllib.request.urlopen is patched with a fake context
manager. os.environ is patched to control GITHUB_TOKEN.
"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# Ensure the project root is importable.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def load_server_with_temp_data_dir(tmp_path):
    """Set DATA_DIR to tmp_path and import (or reload) server fresh."""
    os.environ["DATA_DIR"] = tmp_path
    import server
    return importlib.reload(server)


def make_fake_response(payload):
    """Build a fake context-manager response whose .read() yields JSON bytes."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def make_urlopen_side_effect(payload=None, exc=None):
    """Return a side_effect for urllib.request.urlopen.

    If exc is set, calling urlopen raises it. Otherwise returns a fake response
    wrapping `payload`.
    """
    if exc is not None:
        return lambda *a, **k: (_ for _ in ()).throw(exc)
    fake = make_fake_response(payload)

    def _open(*a, **k):
        return fake
    return _open


class TestClassifyRecency(unittest.TestCase):
    """Boundary cases for classify_recency(pushed_at)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = load_server_with_temp_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _iso(self, days_ago, extra_hours=0):
        dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=extra_hours)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_today_is_active(self):
        self.assertEqual(self.server.classify_recency(self._iso(0)), "active")

    def test_ten_days_is_maintain(self):
        self.assertEqual(self.server.classify_recency(self._iso(10)), "maintain")

    def test_sixty_days_is_stalled(self):
        self.assertEqual(self.server.classify_recency(self._iso(60)), "stalled")

    def test_empty_string_is_stalled(self):
        self.assertEqual(self.server.classify_recency(""), "stalled")

    def test_invalid_is_stalled(self):
        self.assertEqual(self.server.classify_recency("not-a-date"), "stalled")

    def test_six_days_23h_is_active_boundary(self):
        # 6d23h -> delta.days == 6 -> active
        self.assertEqual(self.server.classify_recency(self._iso(6, extra_hours=23)), "active")

    def test_exactly_7d_is_maintain(self):
        # delta.days == 7 -> not < 7 -> maintain
        self.assertEqual(self.server.classify_recency(self._iso(7)), "maintain")

    def test_29d_is_maintain(self):
        self.assertEqual(self.server.classify_recency(self._iso(29)), "maintain")

    def test_exactly_30d_is_stalled(self):
        # delta.days == 30 -> not < 30 -> stalled
        self.assertEqual(self.server.classify_recency(self._iso(30)), "stalled")


class TestGhToken(unittest.TestCase):
    """_gh_token() reads + strips GITHUB_TOKEN."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = load_server_with_temp_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_token_stripped(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "  abc123  "}, clear=False):
            self.assertEqual(self.server._gh_token(), "abc123")

    def test_token_unset_returns_empty(self):
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.server._gh_token(), "")


class TestGhRequest(unittest.TestCase):
    """_gh_request() success + failure modes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = load_server_with_temp_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_200_returns_dict_and_sends_auth_header(self):
        payload = {"hello": "world"}
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(payload)) as mock_open:
            result = self.server._gh_request("https://api.github.com/x")
        self.assertEqual(result, payload)
        # Authorization header must be set on the request
        sent_req = mock_open.call_args[0][0]
        self.assertEqual(sent_req.get_header("Authorization"), "token tok")

    def test_http_error_returns_none(self):
        import urllib.error
        exc = urllib.error.HTTPError("https://api.github.com/x", 500, "boom", {}, None)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(exc=exc)):
            self.assertIsNone(self.server._gh_request("https://api.github.com/x"))

    def test_url_error_returns_none(self):
        import urllib.error
        exc = urllib.error.URLError("network down")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(exc=exc)):
            self.assertIsNone(self.server._gh_request("https://api.github.com/x"))

    def test_json_decode_error_returns_none(self):
        bad = MagicMock()
        bad.read.return_value = b"not json{"
        bad.__enter__.return_value = bad
        bad.__exit__.return_value = False
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=lambda *a, **k: bad):
            self.assertIsNone(self.server._gh_request("https://api.github.com/x"))

    def test_missing_token_returns_none_and_no_urlopen(self):
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True), \
             patch("urllib.request.urlopen") as mock_open:
            self.assertIsNone(self.server._gh_request("https://api.github.com/x"))
            mock_open.assert_not_called()


class TestFetchAllRepos(unittest.TestCase):
    """fetch_all_repos() pagination + failure modes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = load_server_with_temp_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_success_returns_list(self):
        repos = [{"full_name": "a/b"}, {"full_name": "c/d"}]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(repos)):
            result = self.server.fetch_all_repos()
        self.assertEqual(result, repos)

    def test_stops_pagination_when_less_than_100(self):
        # First page returns 2 items (<100) -> loop breaks, only one call.
        page = [{"full_name": "a/b"}, {"full_name": "c/d"}]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(page)) as mock_open:
            result = self.server.fetch_all_repos()
        self.assertEqual(result, page)
        self.assertEqual(mock_open.call_count, 1)

    def test_missing_token_returns_none(self):
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True), \
             patch("urllib.request.urlopen") as mock_open:
            self.assertIsNone(self.server.fetch_all_repos())
            mock_open.assert_not_called()

    def test_http_error_returns_none(self):
        import urllib.error
        exc = urllib.error.HTTPError("https://api.github.com/x", 403, "nope", {}, None)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(exc=exc)):
            self.assertIsNone(self.server.fetch_all_repos())


class TestFetchRecentCommits(unittest.TestCase):
    """fetch_recent_commits() parsing + failure modes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = load_server_with_temp_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_item(self, sha, message):
        return {"sha": sha, "commit": {"message": message}}

    def test_parses_subject_body_and_sha_prefix(self):
        data = [
            self._commit_item("abcdef1234567890", "Subject line\nBody line one\nBody line two"),
            self._commit_item("1111222233334444", "Second commit\nMore body"),
        ]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(data)):
            commits = self.server.fetch_recent_commits("o", "r", "main")
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["sha"], "abcdef12")
        self.assertEqual(commits[0]["subject"], "Subject line")
        self.assertEqual(commits[0]["body"], "Body line one Body line two")
        self.assertEqual(commits[1]["sha"], "11112222")
        self.assertEqual(commits[1]["subject"], "Second commit")

    def test_body_truncated_to_400(self):
        long_body = "word " * 200  # far exceeds 400 chars
        data = [self._commit_item("deadbeefcafe1234", "Subj\n" + long_body)]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(data)):
            commits = self.server.fetch_recent_commits("o", "r", "main")
        self.assertLessEqual(len(commits[0]["body"]), 400)
        self.assertEqual(commits[0]["sha"], "deadbeef")

    def test_http_error_returns_empty(self):
        import urllib.error
        exc = urllib.error.HTTPError("https://api.github.com/x", 500, "boom", {}, None)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(exc=exc)):
            self.assertEqual(self.server.fetch_recent_commits("o", "r", "main"), [])

    def test_non_list_returns_empty(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect({"not": "a list"})):
            self.assertEqual(self.server.fetch_recent_commits("o", "r", "main"), [])

    def test_missing_token_returns_empty(self):
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True), \
             patch("urllib.request.urlopen") as mock_open:
            self.assertEqual(self.server.fetch_recent_commits("o", "r", "main"), [])
            mock_open.assert_not_called()


class TestGetHubRepos(unittest.TestCase):
    """get_hub_repos() caching, stale-while-revalidate, error paths."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = load_server_with_temp_data_dir(self._tmp.name)
        # Reset module-level cache before each test.
        self.server._GH_CACHE = {"repos": None, "ts": 0.0}

    def tearDown(self):
        self._tmp.cleanup()

    def _repo(self, full_name, pushed_at, branch="main"):
        return {
            "full_name": full_name,
            "name": full_name.split("/")[-1],
            "description": "desc",
            "language": "Python",
            "html_url": f"https://github.com/{full_name}",
            "default_branch": branch,
            "pushed_at": pushed_at,
        }

    def _recent_iso(self, days_ago):
        dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_no_token_returns_token_missing(self):
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True), \
             patch("urllib.request.urlopen") as mock_open:
            result = self.server.get_hub_repos()
        self.assertEqual(result["status"], "token_missing")
        self.assertEqual(result["repos"], [])
        self.assertIsInstance(result["banner"], str)
        self.assertIn("GITHUB_TOKEN", result["banner"])
        mock_open.assert_not_called()

    def test_token_and_200_returns_ok_with_enriched_repos(self):
        repos = [self._repo("o/r", self._recent_iso(2))]
        commits = [{"sha": "abcdef1234567890", "commit": {"message": "did a thing\nbody line"}}]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(repos)) as mock_open:
            # fetch_recent_commits also calls urlopen; make the commits call return commits
            def _side(*a, **k):
                req = a[0]
                url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
                if "/commits" in url:
                    return make_fake_response(commits)
                return make_fake_response(repos)
            mock_open.side_effect = _side
            result = self.server.get_hub_repos()
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["banner"])
        self.assertEqual(len(result["repos"]), 1)
        r = result["repos"][0]
        self.assertEqual(r["full_name"], "o/r")
        self.assertEqual(r["recency"], "active")
        self.assertEqual(len(r["commits"]), 1)
        self.assertEqual(r["commits"][0]["sha"], "abcdef12")
        self.assertEqual(r["commits"][0]["subject"], "did a thing")
        self.assertEqual(r["commits"][0]["body"], "body line")

    def test_token_and_http_error_no_cache_returns_error(self):
        import urllib.error
        exc = urllib.error.HTTPError("https://api.github.com/x", 500, "boom", {}, None)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(exc=exc)):
            result = self.server.get_hub_repos()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["repos"], [])
        self.assertIsInstance(result["banner"], str)

    def test_token_and_http_error_with_cache_returns_stale(self):
        import urllib.error
        # Seed a valid cached result first.
        cached = {"repos": [{"full_name": "cached/repo", "recency": "active", "commits": []}],
                  "status": "ok", "banner": None, "ts": 123.0}
        self.server._GH_CACHE = {"repos": cached, "ts": 123.0}
        exc = urllib.error.HTTPError("https://api.github.com/x", 500, "boom", {}, None)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(exc=exc)):
            result = self.server.get_hub_repos()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["repos"], cached["repos"])
        self.assertIsInstance(result["banner"], str)

    def test_force_bypasses_ttl(self):
        # Seed a cached result that is "fresh" (within TTL) so the non-force path
        # would return it directly. With force=True we should hit the network.
        cached = {"repos": [{"full_name": "stale/repo"}], "status": "ok", "banner": None, "ts": 0.0}
        self.server._GH_CACHE = {"repos": cached, "ts": 0.0}
        repos = [self._repo("fresh/repo", self._recent_iso(1))]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=False), \
             patch("urllib.request.urlopen", side_effect=make_urlopen_side_effect(repos)) as mock_open:
            def _side(*a, **k):
                req = a[0]
                url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
                if "/commits" in url:
                    return make_fake_response([])
                return make_fake_response(repos)
            mock_open.side_effect = _side
            result = self.server.get_hub_repos(force=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["repos"][0]["full_name"], "fresh/repo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
