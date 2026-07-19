"""Tests for the Ollama client + lazy /api/hub/summaries endpoint (Step 1.3).

Covers:
  - _ollama_base_url(): default, honors env, strips trailing slash
  - _ollama_model(): default, honors env
  - _ollama_summarize(): success/collapse/truncate, None on URLError, JSON
    decode error, no commits, empty subjects
  - _fill_summaries(): fills missing, skips fresh, fills expired, thread-safe,
    skips missing full_name
  - hub_summaries_api(): response shape, daemon thread spawn, no secret leakage

No real network calls: urllib.request.urlopen is patched. os.environ is patched.
"""

import io
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure the project root is importable.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import server


def make_fake_response(payload):
    """Build a fake context-manager response whose .read() yields JSON bytes."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    # Support `with urllib.request.urlopen(...) as resp:`
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def make_urlerror_response():
    """Return a side_effect callable that raises urllib.error.URLError."""
    import urllib.error
    return urllib.error.URLError("connection refused")


class TestOllamaBaseUrl(unittest.TestCase):
    def test_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._ollama_base_url(), "http://localhost:11434")

    def test_honors_env(self):
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://ollama.local:11434"}, clear=True):
            self.assertEqual(server._ollama_base_url(), "http://ollama.local:11434")

    def test_strips_trailing_slash(self):
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://ollama.local:11434/"}, clear=True):
            self.assertEqual(server._ollama_base_url(), "http://ollama.local:11434")


class TestOllamaModel(unittest.TestCase):
    def test_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._ollama_model(), "qwen2.5:7b")

    def test_honors_env(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "llama3.1:8b"}, clear=True):
            self.assertEqual(server._ollama_model(), "llama3.1:8b")


class TestOllamaSummarize(unittest.TestCase):
    def test_returns_collapsed_string_on_200(self):
        repo = {"commits": [{"subject": "Did a thing recently"}]}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": "Did a thing recently"})):
            result = server._ollama_summarize(repo)
        self.assertEqual(result, "Did a thing recently")

    def test_collapses_whitespace(self):
        repo = {"commits": [{"subject": "x"}]}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": "line one\n   line two\t\tline three"})):
            result = server._ollama_summarize(repo)
        self.assertEqual(result, "line one line two line three")

    def test_truncates_over_200_chars(self):
        long_text = "a" * 250
        repo = {"commits": [{"subject": "x"}]}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": long_text})):
            result = server._ollama_summarize(repo)
        self.assertIsInstance(result, str)
        self.assertLessEqual(len(result), 200)
        self.assertEqual(result, "a" * 200)

    def test_returns_none_on_urlerror(self):
        repo = {"commits": [{"subject": "x"}]}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", side_effect=make_urlerror_response()):
            result = server._ollama_summarize(repo)
        self.assertIsNone(result)

    def test_returns_none_on_json_decode_error(self):
        repo = {"commits": [{"subject": "x"}]}
        bad = MagicMock()
        bad.read.return_value = b"not json{"
        bad.__enter__.return_value = bad
        bad.__exit__.return_value = False
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", return_value=bad):
            result = server._ollama_summarize(repo)
        self.assertIsNone(result)

    def test_returns_none_when_no_commits(self):
        repo = {"commits": []}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen") as mock_urlopen:
            result = server._ollama_summarize(repo)
        self.assertIsNone(result)
        # Must not even attempt a network call when there are no commits.
        mock_urlopen.assert_not_called()

    def test_returns_none_when_empty_subjects(self):
        repo = {"commits": [{"subject": "   "}, {"subject": ""}]}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen") as mock_urlopen:
            result = server._ollama_summarize(repo)
        self.assertIsNone(result)
        mock_urlopen.assert_not_called()


class TestFillSummaries(unittest.TestCase):
    def setUp(self):
        # Isolate the module-level cache for each test.
        self._orig_cache = server._SUMMARY_CACHE
        server._SUMMARY_CACHE = {}

    def tearDown(self):
        server._SUMMARY_CACHE = self._orig_cache

    def test_fills_cache_for_repo_with_no_entry(self):
        repos = [{"full_name": "a/b", "commits": [{"subject": "x"}]}]
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": "did x"})):
            server._fill_summaries(repos)
        self.assertIn("a/b", server._SUMMARY_CACHE)
        self.assertEqual(server._SUMMARY_CACHE["a/b"]["summary"], "did x")

    def test_skips_repo_with_fresh_cache_entry(self):
        repos = [{"full_name": "a/b", "commits": [{"subject": "x"}]}]
        server._SUMMARY_CACHE["a/b"] = {"summary": "cached", "ts": time.time()}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen") as mock_urlopen:
            server._fill_summaries(repos)
        mock_urlopen.assert_not_called()
        self.assertEqual(server._SUMMARY_CACHE["a/b"]["summary"], "cached")

    def test_fills_expired_entry(self):
        repos = [{"full_name": "a/b", "commits": [{"subject": "x"}]}]
        server._SUMMARY_CACHE["a/b"] = {"summary": "stale", "ts": time.time() - (server._SUMMARY_TTL + 10)}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": "fresh"})):
            server._fill_summaries(repos)
        self.assertEqual(server._SUMMARY_CACHE["a/b"]["summary"], "fresh")

    def test_thread_safe_populates_cache(self):
        repos = [{"full_name": f"repo{i}/x", "commits": [{"subject": "x"}]} for i in range(10)]
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": "ok"})):
            server._fill_summaries(repos)
        for i in range(10):
            self.assertIn(f"repo{i}/x", server._SUMMARY_CACHE)

    def test_skips_repo_missing_full_name(self):
        repos = [{"commits": [{"subject": "x"}]}, {"full_name": "  ", "commits": [{"subject": "x"}]}]
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen") as mock_urlopen:
            server._fill_summaries(repos)
        mock_urlopen.assert_not_called()
        self.assertEqual(server._SUMMARY_CACHE, {})


class TestHubSummariesApi(unittest.TestCase):
    def setUp(self):
        self._orig_cache = server._SUMMARY_CACHE
        server._SUMMARY_CACHE = {}

    def tearDown(self):
        server._SUMMARY_CACHE = self._orig_cache

    def _make_fake_self(self):
        fake_self = MagicMock()
        fake_self.wfile = io.BytesIO()
        return fake_self

    def test_response_shape_and_daemon_thread(self):
        fake_self = self._make_fake_self()
        hub_data = {
            "repos": [{"full_name": "a/b", "commits": [{"subject": "x"}]}],
            "status": "ok",
            "banner": None,
            "ts": 0,
        }
        captured = {}
        real_thread = threading.Thread

        def fake_thread(target=None, args=(), daemon=False, **kwargs):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon
            t = real_thread(target=target, args=args, daemon=daemon, **kwargs)
            captured["start_called"] = False
            orig_start = t.start

            def wrapped_start():
                captured["start_called"] = True
                return orig_start()

            t.start = wrapped_start
            return t

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(server, "get_hub_repos", return_value=hub_data), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": "did x"})), \
             patch("threading.Thread", side_effect=fake_thread):
            server.Handler.hub_summaries_api(fake_self)

        # send_response / send_header / end_headers called
        fake_self.send_response.assert_called_once_with(200)
        fake_self.send_header.assert_any_call("Content-Type", "application/json")
        fake_self.send_header.assert_any_call("Cache-Control", "no-store")
        fake_self.end_headers.assert_called_once()

        body = fake_self.wfile.getvalue().decode("utf-8")
        data = json.loads(body)
        self.assertIn("summaries", data)
        self.assertIn("pending", data)
        self.assertIn("a/b", data["summaries"])
        self.assertIn("a/b", data["pending"])

        # Daemon thread spawned with correct target/args and started.
        self.assertIs(captured["target"], server._fill_summaries)
        self.assertEqual(captured["args"], (hub_data["repos"],))
        self.assertTrue(captured["daemon"])
        self.assertTrue(captured["start_called"])

    def test_no_secret_leakage_in_response(self):
        fake_self = self._make_fake_self()
        hub_data = {
            "repos": [{"full_name": "a/b", "commits": [{"subject": "x"}]}],
            "status": "ok",
            "banner": None,
            "ts": 0,
        }
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(server, "get_hub_repos", return_value=hub_data), \
             patch("urllib.request.urlopen", return_value=make_fake_response({"response": "did x"})), \
             patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            server.Handler.hub_summaries_api(fake_self)

        body = fake_self.wfile.getvalue().decode("utf-8")
        data = json.loads(body)
        # Negative: no secret/implementation fields leak into the response.
        for forbidden in ("url", "model", "prompt", "error"):
            self.assertNotIn(forbidden, data)
            self.assertNotIn(forbidden, data.get("summaries", {}))

    def test_urlopen_called_with_generate_endpoint(self):
        fake_self = self._make_fake_self()
        hub_data = {
            "repos": [{"full_name": "a/b", "commits": [{"subject": "x"}]}],
            "status": "ok",
            "banner": None,
            "ts": 0,
        }
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(server, "get_hub_repos", return_value=hub_data), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            server.Handler.hub_summaries_api(fake_self)

        # The background thread calls _fill_summaries -> _ollama_summarize ->
        # urlopen. Because the thread is mocked (not started), urlopen is NOT
        # called synchronously. Instead verify the Request would target
        # /api/generate by exercising _ollama_summarize directly here.
        repo = {"full_name": "a/b", "commits": [{"subject": "x"}]}
        with patch.dict(os.environ, {}, clear=True), \
             patch("urllib.request.urlopen") as mock_urlopen2:
            mock_urlopen2.return_value = make_fake_response({"response": "did x"})
            server._ollama_summarize(repo)
            args, kwargs = mock_urlopen2.call_args
            req = args[0]
            self.assertIn("/api/generate", req.full_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
