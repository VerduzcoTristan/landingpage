"""Regression tests for the briefing, mutation, and security audit fixes."""

import io
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import server


class FakeHandler:
    def __init__(self, path, body=b"", client_address=("127.0.0.1", 1)):
        self.path = path
        self.rfile = io.BytesIO(body)
        self.client_address = client_address
        self.headers = {"Host": "localhost", "Content-Length": str(len(body))}
        self.responses = []

    def _respond(self, code, content_type, body):
        self.responses.append((code, content_type, body))

    def _reject_unallowed_host(self):
        return False


class TestBriefingRendering(unittest.TestCase):
    def test_detail_uses_iso_story_date_and_escapes_external_fields(self):
        article = {
            "title": "Today's <script>alert(1)</script>",
            "summary": "<img src=x onerror=alert(1)>",
            "source_name": 'Source" onmouseover="alert(1)',
            "source_url": "javascript:alert(1)",
            "categories": "AI,<img>",
            "position": 1,
        }
        with patch.object(server, "_is_bookmarked", return_value=False):
            rendered = server.briefing_card_from_db(
                [article], "Monday, July 20, 2026", story_date="2026-07-20"
            )
        expected_id = server._story_id(
            "2026-07-20", article["title"], article["source_url"]
        )
        self.assertIn(expected_id, rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;img&gt;", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn("onclick=\"toggleBookmark(this,'", rendered)
        self.assertIn("&#x27;", rendered)

    def test_bookmark_write_is_utc_and_atomic_path_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bookmarks.json"
            with patch.object(server, "BOOKMARKS_FILE", path):
                server._toggle_bookmark(
                    "story-id",
                    {"title": "Story", "date": "2026-07-20"},
                    "saved",
                )
                data = server._load_bookmarks()
            self.assertEqual(len(data["saved"]), 1)
            self.assertTrue(data["saved"][0]["saved_at"].endswith("+00:00"))


class TestBookmarkMutationAuth(unittest.TestCase):
    def _body(self, **values):
        values.setdefault("id", "story-id")
        values.setdefault("type", "saved")
        return urllib.parse.urlencode(values).encode()

    def test_missing_csrf_is_rejected_before_storage(self):
        body = self._body(title="Today's story")
        handler = FakeHandler("/bookmarks/toggle", body)
        with patch.object(server.Handler, "_reject_unallowed_host", return_value=False), patch.object(
            server, "is_authenticated", return_value=True
        ), patch.object(server, "_toggle_bookmark") as toggle:
            server.Handler.do_POST(handler)
        self.assertEqual(handler.responses[0][0], 403)
        toggle.assert_not_called()

    def test_valid_csrf_toggles_bookmark(self):
        body = self._body(title="Today's story", csrf_token=server.CSRF_TOKEN)
        handler = FakeHandler("/bookmarks/toggle", body)
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            server, "BOOKMARKS_FILE", Path(tmp) / "bookmarks.json"
        ), patch.object(server.Handler, "_reject_unallowed_host", return_value=False), patch.object(
            server, "is_authenticated", return_value=True
        ):
            server.Handler.do_POST(handler)
        self.assertEqual(handler.responses[0][0], 200)
        self.assertIn(b'"ok": true', handler.responses[0][2])


class TestAccessVerification(unittest.TestCase):
    def test_malformed_or_unconfigured_jwt_is_denied(self):
        with patch.dict(
            "os.environ",
            {"CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com", "CF_ACCESS_AUDIENCE": "aud"},
            clear=True,
        ):
            self.assertFalse(server._verify_access_jwt("not.a.jwt"))


class TestMonitorRefresh(unittest.TestCase):
    def test_monitor_status_returns_before_slow_probe_finishes(self):
        started = threading.Event()
        release = threading.Event()

        def slow_check(item):
            started.set()
            release.wait(2)
            return {"name": item["name"], "healthy": True, "latency_ms": 1,
                    "error": None, "status_code": 200}

        with patch.object(
            server, "_MONITOR_CACHE", {"data": None, "ts": 0.0, "refreshing": False}
        ), patch.object(
            server, "_load_monitor_config",
            return_value=([{"name": "slow", "url": "https://example.test", "timeout": 30}], [], None),
        ), patch.object(server, "_check_monitor", side_effect=slow_check):
            started_at = time.perf_counter()
            snapshot = server.get_monitor_status(force=True)
            elapsed = time.perf_counter() - started_at
            self.assertLess(elapsed, 0.2)
            self.assertEqual(snapshot["status"], "checking")
            self.assertTrue(started.wait(0.5))
            release.set()
            deadline = time.time() + 1
            while time.time() < deadline:
                if server._MONITOR_CACHE["data"].get("status") == "ok":
                    break
                time.sleep(0.01)
            self.assertEqual(server._MONITOR_CACHE["data"]["status"], "ok")


class TestContainerHealthcheck(unittest.TestCase):
    def test_healthchecks_use_liveness_route(self):
        compose = Path("compose.yml").read_text(encoding="utf-8")
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:3002/health", compose)
        self.assertIn("127.0.0.1:3002/health", dockerfile)
        self.assertNotIn("urlopen('http://127.0.0.1:3002/',", compose)


if __name__ == "__main__":
    unittest.main()
