"""Tests for the Hub action endpoints added in Step 1.6 (refresh + backup).

Covers:
  - POST /hub/admin/refresh  (force-bust hub cache, redirect)
  - POST /hub/admin/backup   (tar.gz of DATA_DIR returned as octet-stream)

Auth gate (is_authenticated) is exercised for BOTH endpoints:
  - unauthenticated -> 403 + "Access required", and the protected side-effect
    (get_hub_repos / tarfile.open) is NOT invoked.
  - authenticated   -> the protected side-effect runs and the right response
    shape is produced.

No real network calls and no real file reads/writes: get_hub_repos,
is_authenticated, DATA_DIR, and tarfile.open are patched at the server module
level. The backup test points DATA_DIR at a temp dir containing a known file
and verifies the returned bytes are a valid gzip tar containing that file.

All tests use the Python stdlib only (unittest + unittest.mock).
"""

import io
import os
import sys
import tarfile
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch, MagicMock

# Ensure the project root is importable as a package path.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import server  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

class _DictHeaders:
    """dict-like object exposing .get(key, default) like email.message.Message."""

    def __init__(self, data):
        self._data = dict(data)

    def get(self, key, default=""):
        for k, v in self._data.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeHandler:
    """Minimal stand-in for http.server.BaseHTTPRequestHandler.

    Provides just enough surface for is_authenticated() and do_POST() to run
    without a real socket: client_address, headers (dict-like with .get),
    path, rfile, and stubbed response helpers. Header writes are captured so
    the backup endpoint's Content-Disposition / Content-Type can be asserted.
    """

    def __init__(self, path="/hub/admin/refresh", headers=None, body=b"",
                 client_address=("127.0.0.1", 1234)):
        if path.startswith("/hub/admin/") and not body:
            body = urllib.parse.urlencode({"csrf_token": server.CSRF_TOKEN}).encode()
        self.path = path
        self.headers = _DictHeaders(headers or {})
        self.rfile = _BytesIO(body)
        self.client_address = client_address
        self.responses = []          # list of (code, content_type, body)
        self.redirects = []          # list of location strings
        self.sent_404 = False
        self._status_code = None
        self._resp_headers = []      # list of (name, value) from send_header
        self.wfile = io.BytesIO()    # real buffer so backup bytes can be read

    # response helpers used by server.Handler
    def _respond(self, code, content_type, body):
        self.responses.append((code, content_type, body))

    def _send_redirect(self, location):
        self.redirects.append(location)

    def send_response(self, code):
        self._status_code = code
        if code == 404:
            self.sent_404 = True

    def send_header(self, name, value):
        self._resp_headers.append((name, value))

    def end_headers(self):
        pass

    def _reject_unallowed_host(self):
        # In tests we always send an allowed Host header, so never reject.
        return False


class _BytesIO:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data


# ── Auth gate tests (both endpoints) ─────────────────────────────────────────

class TestHubActionAuthGate(unittest.TestCase):
    """Unauthenticated requests must be rejected with 403 and no side-effects."""

    def _make_handler(self, path, body=b"", headers=None, client_address=None):
        return FakeHandler(
            path=path,
            body=body,
            headers=headers or {"Host": "localhost"},
            client_address=client_address or ("203.0.113.9", 1234),
        )

    def test_refresh_unauthenticated_returns_403(self):
        h = self._make_handler(path="/hub/admin/refresh")
        with patch.object(server, "is_authenticated", return_value=False), \
             patch.object(server, "get_hub_repos") as mock_repos:
            server.Handler.do_POST(h)
        self.assertEqual(len(h.responses), 1)
        code, ctype, body = h.responses[0]
        self.assertEqual(code, 403)
        self.assertIn("Access required", body.decode("utf-8", errors="replace"))
        # The protected side-effect must NOT run when unauthenticated.
        mock_repos.assert_not_called()

    def test_backup_unauthenticated_returns_403(self):
        h = self._make_handler(path="/hub/admin/backup")
        with patch.object(server, "is_authenticated", return_value=False), \
             patch("tarfile.open") as mock_tar:
            server.Handler.do_POST(h)
        self.assertEqual(len(h.responses), 1)
        code, ctype, body = h.responses[0]
        self.assertEqual(code, 403)
        self.assertIn("Access required", body.decode("utf-8", errors="replace"))
        # The protected side-effect must NOT run when unauthenticated.
        mock_tar.assert_not_called()


# ── Refresh success tests ────────────────────────────────────────────────────

class TestHubActionRefresh(unittest.TestCase):

    def _make_handler(self, path, body=b"", headers=None, client_address=None):
        return FakeHandler(
            path=path,
            body=body,
            headers=headers or {"Host": "localhost"},
            client_address=client_address or ("127.0.0.1", 1234),
        )

    def test_refresh_authenticated_calls_get_hub_repos_force(self):
        h = self._make_handler(path="/hub/admin/refresh")
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server, "get_hub_repos") as mock_repos, \
             patch.object(server._GITHUB_CLIENT, "invalidate") as github_invalidate, \
             patch.object(server._OLLAMA_CLIENT, "invalidate") as summary_invalidate:
            server.Handler.do_POST(h)
            # force=True must be passed to bust the cache
            mock_repos.assert_called_once_with(force=True)
            github_invalidate.assert_called_once_with()
            summary_invalidate.assert_called_once_with()

    def test_refresh_authenticated_redirects_to_hub_admin(self):
        h = self._make_handler(path="/hub/admin/refresh")
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server, "get_hub_repos"):
            server.Handler.do_POST(h)
        self.assertEqual(len(h.redirects), 1)
        location = h.redirects[0]
        self.assertTrue(location.startswith("/hub/admin?"))
        self.assertIn("message=", location)
        self.assertIn("Hub+refreshed", location)


# ── Backup success tests ────────────────────────────────────────────────────

class TestHubActionBackup(unittest.TestCase):

    def _make_handler(self, path, body=b"", headers=None, client_address=None):
        return FakeHandler(
            path=path,
            body=body,
            headers=headers or {"Host": "localhost"},
            client_address=client_address or ("127.0.0.1", 1234),
        )

    def _header(self, h, name):
        for n, v in h._resp_headers:
            if n.lower() == name.lower():
                return v
        return None

    def test_backup_authenticated_uses_tarfile_wgz(self):
        tmp = tempfile.mkdtemp()
        h = self._make_handler(path="/hub/admin/backup")
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server, "DATA_DIR", tmp), \
             patch("tarfile.open") as mock_tar:
            server.Handler.do_POST(h)
            # tarfile.open must be opened in gzip-write mode
            mock_tar.assert_called_once()
            _, kwargs = mock_tar.call_args
            self.assertEqual(kwargs.get("mode"), "w:gz")

    def test_backup_uses_bounded_spool_instead_of_whole_archive_bytes(self):
        tmp = tempfile.mkdtemp()
        h = self._make_handler(path="/hub/admin/backup")
        real_spool = tempfile.SpooledTemporaryFile
        calls = []

        def tracked_spool(*args, **kwargs):
            calls.append(kwargs)
            return real_spool(*args, **kwargs)

        with patch.object(server, "is_authenticated", return_value=True), patch.object(
            server, "DATA_DIR", tmp
        ), patch("tempfile.SpooledTemporaryFile", side_effect=tracked_spool):
            server.Handler.do_POST(h)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["max_size"], 2 * 1024 * 1024)

    def test_backup_authenticated_sets_octet_stream_and_disposition(self):
        tmp = tempfile.mkdtemp()
        h = self._make_handler(path="/hub/admin/backup")
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server, "DATA_DIR", tmp):
            server.Handler.do_POST(h)
        self.assertEqual(h._status_code, 200)
        ctype = self._header(h, "Content-Type")
        self.assertEqual(ctype, "application/octet-stream")
        disp = self._header(h, "Content-Disposition")
        self.assertIsNotNone(disp)
        self.assertIn("hub-backup-", disp)
        self.assertIn(".tar.gz", disp)
        # Cache-Control must prevent caching of the download
        self.assertEqual(self._header(h, "Cache-Control"), "no-store")
        # Content-Length must be a non-empty numeric string
        clen = self._header(h, "Content-Length")
        self.assertIsNotNone(clen)
        self.assertGreater(int(clen), 0)

    def test_backup_returns_valid_targz_containing_data_dir(self):
        tmp = tempfile.mkdtemp()
        # Write a known small file inside the patched DATA_DIR.
        known_name = "monitors.json"
        known_content = b'{"hello": "world"}'
        with open(os.path.join(tmp, known_name), "wb") as fh:
            fh.write(known_content)

        h = self._make_handler(path="/hub/admin/backup")
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server, "DATA_DIR", tmp):
            server.Handler.do_POST(h)

        raw = h.wfile.getvalue()
        self.assertTrue(len(raw) > 0)
        # The returned bytes must be a valid gzip-compressed tar archive.
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            names = tar.getnames()
            # arcname is "hub-backup", so the file appears as hub-backup/<name>
            self.assertIn("hub-backup/" + known_name, names)
            member = tar.extractfile("hub-backup/" + known_name)
            self.assertIsNotNone(member)
            self.assertEqual(member.read(), known_content)


# ── No-shell guarantee for backup ────────────────────────────────────────────

class TestHubActionBackupNoShell(unittest.TestCase):
    """The backup endpoint must never shell out (no subprocess usage)."""

    def _make_handler(self, path, body=b"", headers=None, client_address=None):
        return FakeHandler(
            path=path,
            body=body,
            headers=headers or {"Host": "localhost"},
            client_address=client_address or ("127.0.0.1", 1234),
        )

    def test_backup_does_not_use_subprocess(self):
        # server.py must not import subprocess at module level, and the backup
        # branch must not reference it. We assert both: no module-level import
        # and that subprocess.run is never called during a backup.
        self.assertNotIn("subprocess", getattr(server, "__dict__", {}),
                         "server must not import subprocess at module level")

        tmp = tempfile.mkdtemp()
        h = self._make_handler(path="/hub/admin/backup")
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server, "DATA_DIR", tmp), \
             patch("subprocess.run") as mock_run:
            server.Handler.do_POST(h)
        mock_run.assert_not_called()


# ── UI buttons present in hub_admin_page ────────────────────────────────────

class TestHubActionUIButtons(unittest.TestCase):
    """The curation page must expose forms posting to both action endpoints."""

    def setUp(self):
        self.patcher_repos = patch.object(server, "get_hub_repos")
        self.patcher_load = patch.object(server, "load_hub")
        self.mock_repos = self.patcher_repos.start()
        self.mock_load = self.patcher_load.start()

    def tearDown(self):
        self.patcher_repos.stop()
        self.patcher_load.stop()

    def test_page_has_refresh_form(self):
        self.mock_repos.return_value = {"repos": []}
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn('action="/hub/admin/refresh"', out)
        self.assertIn("Refresh hub now", out)

    def test_page_has_backup_form(self):
        self.mock_repos.return_value = {"repos": []}
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn('action="/hub/admin/backup"', out)
        self.assertIn("Download backup", out)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_handler(self, path, body=b"", headers=None, client_address=None):  # pragma: no cover
    return FakeHandler(
        path=path,
        body=body,
        headers=headers or {"Host": "localhost"},
        client_address=client_address or ("127.0.0.1", 1234),
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
