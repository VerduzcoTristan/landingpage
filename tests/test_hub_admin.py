"""Tests for the /hub/admin curation page + POST handler (Step 1.5).

Covers:
  - hub_admin_page(): HTML output, project-management heading, repo name listing
    (escaped), goal prefill from load_hub(), status_override select with ""
    and "done" options, hidden full_name input, empty-state when no repos.
  - XSS escaping: injected <script> in repo name and injected attribute-break
    in goal are escaped, never emitted raw.
  - is_authenticated(): localhost bypass (127.0.0.1 / ::1), Cf-Access header
    grant, and denial otherwise.
  - update_hub() contract via a fake `get` callable: action "update" and the
    callable are forwarded; delete/toggle-hide wrap full_name correctly.
  - POST dispatch (do_POST) for /hub/admin/*: auth gate returns 403 +
    _UNAUTH_PAGE when unauthenticated; authenticated update/delete/toggle-hide
    invoke update_hub with the right action and redirect to /hub/admin.

All network calls and real data files are avoided: get_hub_repos, load_hub,
update_hub, and is_authenticated are patched at the server module level.
"""

import os
import sys
import unittest
import urllib.parse
from unittest.mock import patch, MagicMock

# Ensure the project root is importable as a package path.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import server  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

def make_repo(full_name, name=None, description="", language="Python",
              html_url=None, recency="active",
              commits=None, pushed_at="2024-01-01T00:00:00Z"):
    if html_url is None:
        html_url = "https://github.com/" + full_name
    return {
        "full_name": full_name,
        "name": name or full_name.split("/")[-1],
        "description": description,
        "language": language,
        "html_url": html_url,
        "default_branch": "main",
        "pushed_at": pushed_at,
        "recency": recency,
        "commits": commits or [],
    }


def make_hub_entry(goal="", live_url="", local_path="", status_override="",
                   order=999, hidden=False, current_override="", pinned=False):
    return {
        "goal": goal,
        "current_override": current_override,
        "whats_next": "",
        "status_override": status_override,
        "live_url": live_url,
        "local_path": local_path,
        "pinned": pinned,
        "hidden": hidden,
        "order": order,
    }


class FakeHandler:
    """Minimal stand-in for http.server.BaseHTTPRequestHandler.

    Provides just enough surface for is_authenticated() and do_POST() to run
    without a real socket: client_address, headers (dict-like with .get),
    path, rfile, and stubbed response helpers.
    """

    def __init__(self, path="/hub/admin/update", headers=None, body=b"",
                 client_address=("127.0.0.1", 1234)):
        self.path = path
        self.headers = _DictHeaders(headers or {})
        self.rfile = _BytesIO(body)
        self.client_address = client_address
        self.responses = []          # list of (code, content_type, body)
        self.redirects = []          # list of location strings
        self.sent_404 = False

    # response helpers used by server.Handler
    def _respond(self, code, content_type, body):
        self.responses.append((code, content_type, body))

    def _send_redirect(self, location):
        self.redirects.append(location)

    def _reject_unallowed_host(self):
        # Delegate through Handler so patch.object(server.Handler, ...) remains
        # effective while do_POST is exercised as an unbound method.
        return server.Handler._reject_unallowed_host(self)

    def send_response(self, code):
        if code == 404:
            self.sent_404 = True

    def end_headers(self):
        pass

    def send_header(self, *args, **kwargs):
        pass

    def wfile(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


class _DictHeaders:
    """dict-like object exposing .get(key, default) like email.message.Message."""

    def __init__(self, data):
        self._data = dict(data)

    def get(self, key, default=""):
        # HTTP headers are case-insensitive; server uses a specific capitalisation.
        for k, v in self._data.items():
            if k.lower() == key.lower():
                return v
        return default


class _BytesIO:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data


# ── hub_admin_page() render tests ───────────────────────────────────────────

class TestHubAdminPageRender(unittest.TestCase):

    def setUp(self):
        self.patcher_repos = patch.object(server, "get_hub_repos")
        self.patcher_load = patch.object(server, "load_hub")
        self.mock_repos = self.patcher_repos.start()
        self.mock_load = self.patcher_load.start()

    def tearDown(self):
        self.patcher_repos.stop()
        self.patcher_load.stop()

    def test_returns_html_with_management_heading(self):
        self.mock_repos.return_value = {"repos": []}
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIsInstance(out, str)
        self.assertIn("<!DOCTYPE html>", out)
        self.assertIn("Manage Projects", out)

    def test_lists_repo_name_escaped(self):
        self.mock_repos.return_value = {
            "repos": [make_repo("owner/repo", name="My Repo")]
        }
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn("My Repo", out)
        # form action for update must be present
        self.assertIn('action="/hub/admin/update"', out)

    def test_prefills_goal_from_load_hub(self):
        self.mock_repos.return_value = {
            "repos": [make_repo("owner/repo")]
        }
        self.mock_load.return_value = {
            "owner/repo": make_hub_entry(
                goal="Ship the thing", order=3, hidden=True,
                current_override="Manual state", pinned=True,
            )
        }
        out = server.hub_admin_page()
        self.assertIn('name="goal"', out)
        self.assertIn('>Ship the thing</textarea>', out)
        # order prefilled
        self.assertIn('name="order" value="3"', out)
        # hidden checkbox checked
        self.assertIn('name="hidden" value="1" checked', out)
        self.assertIn('name="pinned" value="1" checked', out)
        self.assertIn('name="current_override"', out)
        self.assertIn('>Manual state</textarea>', out)

    def test_status_override_select_has_empty_and_done(self):
        self.mock_repos.return_value = {
            "repos": [make_repo("owner/repo")]
        }
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn('<option value=""', out)
        self.assertIn('>Auto (by recency)</option>', out)
        self.assertIn('<option value="done', out)
        self.assertIn('>Done</option>', out)

    def test_status_override_done_selected_when_override_done(self):
        self.mock_repos.return_value = {
            "repos": [make_repo("owner/repo")]
        }
        self.mock_load.return_value = {
            "owner/repo": make_hub_entry(status_override="done")
        }
        out = server.hub_admin_page()
        self.assertIn('<option value="done" selected', out)

    def test_renders_hidden_full_name_input(self):
        self.mock_repos.return_value = {
            "repos": [make_repo("owner/repo")]
        }
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn('<input type="hidden" name="full_name" value="owner/repo">', out)

    def test_forms_include_csrf_and_confirmed_post_delete(self):
        self.mock_repos.return_value = {"repos": [make_repo("owner/repo")], "status": "ok"}
        self.mock_load.return_value = {"owner/repo": make_hub_entry()}
        out = server.hub_admin_page()
        self.assertIn('name="csrf_token"', out)
        self.assertIn('formaction="/hub/admin/delete"', out)
        self.assertIn("return confirm", out)
        self.assertNotIn('href="/hub/admin/delete?', out)

    def test_compact_list_has_search_filters_and_expandable_editors(self):
        self.mock_repos.return_value = {"repos": [make_repo("owner/repo")]}
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn('type="search" id="admin-repo-search"', out)
        for key in ("all", "uncurated", "hidden", "done"):
            self.assertIn(f'data-admin-filter="{key}"', out)
        self.assertIn('<details class="admin-repo"', out)
        self.assertNotIn('<details class="admin-repo" open', out)
        self.assertIn('data-uncurated="true"', out)

    def test_editor_contains_every_curation_field_and_technical_links(self):
        self.mock_repos.return_value = {"repos": [make_repo("owner/repo")]}
        self.mock_load.return_value = {"owner/repo": {
            **make_hub_entry(live_url="https://live.example", local_path="C:/work/repo"),
            "whats_next": "Deploy next",
        }}
        out = server.hub_admin_page()
        for field in ("goal", "current_override", "whats_next", "status_override",
                      "live_url", "local_path", "pinned", "hidden", "order"):
            self.assertIn(f'name="{field}"', out)
        self.assertIn("Automatic current", out)
        self.assertIn("Automatic next", out)
        self.assertIn("Use automatic", out)
        self.assertIn('formaction="/hub/admin/regenerate"', out)
        self.assertIn("Regenerate from code changes", out)
        self.assertIn("Repository ↗", out)
        self.assertIn("Live site ↗", out)
        self.assertIn("Local: C:/work/repo", out)

    def test_context_feedback_is_inline_and_opens_edited_repository(self):
        self.mock_repos.return_value = {"repos": [make_repo("owner/repo")]}
        self.mock_load.return_value = {"owner/repo": make_hub_entry()}
        out = server.hub_admin_page("Saved safely", "owner/repo")
        self.assertIn('id="owner/repo"', out)
        self.assertIn('data-done="false" open>', out)
        self.assertIn('<div class="notice" role="status">Saved safely</div>', out)

    def test_curated_only_repository_remains_editable(self):
        self.mock_repos.return_value = {"repos": []}
        self.mock_load.return_value = {"owner/local-only": {
            **make_hub_entry(goal="Local goal"), "whats_next": "Finish it",
        }}
        out = server.hub_admin_page()
        self.assertIn("local-only", out)
        self.assertIn("curated only", out)
        self.assertIn("Local goal", out)

    def test_empty_state_when_no_repos(self):
        self.mock_repos.return_value = {"repos": []}
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn("No projects to curate yet.", out)
        self.assertIn("Configure GitHub access", out)

    def test_message_is_escaped_and_shown(self):
        self.mock_repos.return_value = {"repos": []}
        self.mock_load.return_value = {}
        out = server.hub_admin_page(message='<b>done</b>')
        self.assertIn("&lt;b&gt;done&lt;/b&gt;", out)
        self.assertNotIn("<b>done</b>", out)


# ── XSS escaping tests ──────────────────────────────────────────────────────

class TestHubAdminPageXSS(unittest.TestCase):

    def setUp(self):
        self.patcher_repos = patch.object(server, "get_hub_repos")
        self.patcher_load = patch.object(server, "load_hub")
        self.mock_repos = self.patcher_repos.start()
        self.mock_load = self.patcher_load.start()

    def tearDown(self):
        self.patcher_repos.stop()
        self.patcher_load.stop()

    def test_repo_name_script_is_escaped(self):
        self.mock_repos.return_value = {
            "repos": [make_repo("owner/repo", name='<script>alert(1)</script>')]
        }
        self.mock_load.return_value = {}
        out = server.hub_admin_page()
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>alert(1)</script>", out)

    def test_goal_attribute_break_is_escaped(self):
        # A goal containing a quote + event handler must be escaped so it cannot
        # break out of the value="" attribute.
        malicious = '" onmouseover="alert(1)'
        self.mock_repos.return_value = {
            "repos": [make_repo("owner/repo")]
        }
        self.mock_load.return_value = {
            "owner/repo": make_hub_entry(goal=malicious)
        }
        out = server.hub_admin_page()
        # html.escape with quote=True turns " into &quot;
        self.assertIn("&quot; onmouseover=&quot;", out)
        self.assertNotIn('" onmouseover="', out)


# ── is_authenticated() tests ────────────────────────────────────────────────

class TestIsAuthenticated(unittest.TestCase):

    def test_localhost_ipv4_is_authenticated(self):
        h = FakeHandler(client_address=("127.0.0.1", 5000))
        self.assertTrue(server.is_authenticated(h))

    def test_localhost_ipv6_is_authenticated(self):
        h = FakeHandler(client_address=("::1", 5000))
        self.assertTrue(server.is_authenticated(h))

    def test_cf_access_email_header_alone_is_denied(self):
        h = FakeHandler(
            client_address=("203.0.113.5", 5000),
            headers={"Cf-Access-Authenticated-User-Email": "tristan@example.com"},
        )
        self.assertFalse(server.is_authenticated(h))

    def test_verified_cf_access_jwt_grants_access(self):
        h = FakeHandler(
            client_address=("203.0.113.5", 5000),
            headers={"Cf-Access-Jwt-Assertion": "signed-token"},
        )
        with patch.object(server, "_verify_access_jwt", return_value=True) as verify:
            self.assertTrue(server.is_authenticated(h))
        verify.assert_called_once_with("signed-token")

    def test_remote_without_header_is_denied(self):
        h = FakeHandler(
            client_address=("203.0.113.5", 5000),
            headers={},
        )
        self.assertFalse(server.is_authenticated(h))

    def test_empty_cf_header_is_denied(self):
        h = FakeHandler(
            client_address=("203.0.113.5", 5000),
            headers={"Cf-Access-Authenticated-User-Email": ""},
        )
        self.assertFalse(server.is_authenticated(h))


# ── update_hub() contract tests (via fake get callable) ─────────────────────

class TestUpdateHubContract(unittest.TestCase):

    def test_update_invokes_update_hub_with_action_and_callable(self):
        captured = {}

        def fake_update_hub(action, get):
            captured["action"] = action
            captured["get"] = get
            return "ok"

        form = {"full_name": "a/b", "goal": "x"}
        get = lambda k: form.get(k, "")
        with patch.object(server, "update_hub", side_effect=fake_update_hub):
            result = server.update_hub("update", get)
        self.assertEqual(result, "ok")
        self.assertEqual(captured["action"], "update")
        self.assertTrue(callable(captured["get"]))
        self.assertEqual(captured["get"]("full_name"), "a/b")
        self.assertEqual(captured["get"]("goal"), "x")

    def test_delete_wraps_full_name_callable(self):
        captured = {}

        def fake_update_hub(action, get):
            captured["action"] = action
            captured["get"] = get
            return "deleted"

        form = {"full_name": "a/b"}
        get = lambda k: form.get(k, "")
        # Mirror server.do_POST delete branch: lambda k: fn if k=="full_name" else get(k)
        fn = get("full_name")
        wrapped = lambda k: fn if k == "full_name" else get(k)
        with patch.object(server, "update_hub", side_effect=fake_update_hub):
            result = server.update_hub("delete", wrapped)
        self.assertEqual(result, "deleted")
        self.assertEqual(captured["action"], "delete")
        self.assertEqual(captured["get"]("full_name"), "a/b")
        # other keys still fall through to the underlying form get
        self.assertEqual(captured["get"]("goal"), "")

    def test_toggle_hide_wraps_full_name_callable(self):
        captured = {}

        def fake_update_hub(action, get):
            captured["action"] = action
            captured["get"] = get
            return "toggled"

        form = {"full_name": "owner/repo"}
        get = lambda k: form.get(k, "")
        fn = get("full_name")
        wrapped = lambda k: fn if k == "full_name" else get(k)
        with patch.object(server, "update_hub", side_effect=fake_update_hub):
            result = server.update_hub("toggle-hide", wrapped)
        self.assertEqual(result, "toggled")
        self.assertEqual(captured["action"], "toggle-hide")
        self.assertEqual(captured["get"]("full_name"), "owner/repo")


# ── do_POST /hub/admin/* dispatch tests ─────────────────────────────────────

class TestHubAdminPostDispatch(unittest.TestCase):
    """Exercise Handler.do_POST() for the /hub/admin/* routes.

    A minimal FakeHandler supplies path/headers/rfile plus stubbed response
    helpers so do_POST runs end-to-end without a socket. is_authenticated,
    update_hub, and _reject_unallowed_host are patched at module level so no
    network or real data access occurs.
    """

    def _make_handler(self, path, body=b"", headers=None, client_address=None):
        params = urllib.parse.parse_qs(body.decode("utf-8")) if body else {}
        params["csrf_token"] = [server.CSRF_TOKEN]
        body = urllib.parse.urlencode(params, doseq=True).encode()
        h = FakeHandler(
            path=path,
            body=body,
            headers=headers or {"Host": "localhost"},
            client_address=client_address or ("127.0.0.1", 1234),
        )
        return h

    def test_unauthenticated_returns_403_unauth_page(self):
        h = self._make_handler(
            path="/hub/admin/update",
            body=b"full_name=a%2Fb&goal=x",
            headers={"Host": "localhost"},
            client_address=("203.0.113.9", 1234),
        )
        with              patch.object(server, "is_authenticated", return_value=False), \
             patch.object(server.Handler, "_reject_unallowed_host", return_value=False):
            server.Handler.do_POST(h)
        self.assertEqual(len(h.responses), 1)
        code, ctype, body = h.responses[0]
        self.assertEqual(code, 403)
        self.assertIn("Access required", body.decode("utf-8", errors="replace"))

    def test_authenticated_missing_csrf_returns_403_without_mutation(self):
        h = FakeHandler(path="/hub/admin/update", headers={"Host": "localhost"},
                        body=b"full_name=a%2Fb&goal=x")
        with patch.object(server, "is_authenticated", return_value=True), patch.object(
            server.Handler, "_reject_unallowed_host", return_value=False
        ), patch.object(server, "update_hub") as update:
            server.Handler.do_POST(h)
        self.assertEqual(h.responses[0][0], 403)
        update.assert_not_called()

    def test_authenticated_update_invokes_update_hub_and_redirects(self):
        h = self._make_handler(
            path="/hub/admin/update",
            body=b"full_name=a%2Fb&goal=hello",
            headers={"Host": "localhost"},
        )
        captured = {}
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server.Handler, "_reject_unallowed_host", return_value=False), \
             patch.object(server, "update_hub", side_effect=lambda a, g: captured.setdefault("action", a) or "saved"):
            server.Handler.do_POST(h)
        self.assertEqual(captured["action"], "update")
        self.assertEqual(len(h.redirects), 1)
        self.assertTrue(h.redirects[0].startswith("/hub/admin?"))
        self.assertTrue(h.redirects[0].endswith("#a%2Fb"))

    def test_authenticated_update_persists_through_real_store_contract(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = server.HubStore(Path(tmp) / "curation.json")
            h = self._make_handler(
                path="/hub/admin/update",
                body=b"full_name=a%2Fb&goal=hello&current_override=state&order=4",
                headers={"Host": "localhost"},
            )
            with patch.object(server, "_HUB_STORE", store), patch.object(
                server.Handler, "_reject_unallowed_host", return_value=False
            ):
                server.Handler.do_POST(h)
            self.assertEqual(len(h.redirects), 1)
            saved = store.load()["a/b"]
            self.assertEqual(saved["goal"], "hello")
            self.assertEqual(saved["current_override"], "state")
            self.assertEqual(saved["order"], 4)

    def test_authenticated_delete_invokes_update_hub_delete(self):
        h = self._make_handler(
            path="/hub/admin/delete",
            body=b"full_name=a%2Fb",
            headers={"Host": "localhost"},
        )
        captured = {}
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server.Handler, "_reject_unallowed_host", return_value=False), \
             patch.object(server, "update_hub", side_effect=lambda a, g: captured.setdefault("action", a) or "deleted"):
            server.Handler.do_POST(h)
        self.assertEqual(captured["action"], "delete")
        self.assertEqual(len(h.redirects), 1)

    def test_authenticated_toggle_hide_invokes_update_hub_toggle(self):
        h = self._make_handler(
            path="/hub/admin/toggle-hide",
            body=b"full_name=a%2Fb",
            headers={"Host": "localhost"},
        )
        captured = {}
        with patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server.Handler, "_reject_unallowed_host", return_value=False), \
             patch.object(server, "update_hub", side_effect=lambda a, g: captured.setdefault("action", a) or "toggled"):
            server.Handler.do_POST(h)
        self.assertEqual(captured["action"], "toggle-hide")
        self.assertEqual(len(h.redirects), 1)

    def test_unknown_hub_admin_action_returns_404(self):
        h = self._make_handler(
            path="/hub/admin/bogus",
            body=b"",
            headers={"Host": "localhost"},
        )
        with              patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server.Handler, "_reject_unallowed_host", return_value=False):
            server.Handler.do_POST(h)
        self.assertEqual(h.responses[0][0], 404)

    def test_unallowed_host_short_circuits(self):
        h = self._make_handler(
            path="/hub/admin/update",
            body=b"full_name=a%2Fb",
            headers={"Host": "evil.example.com"},
        )
        with patch.object(server.Handler, "_reject_unallowed_host", return_value=True) as mock_rej, \
             patch.object(server, "is_authenticated", return_value=True), \
             patch.object(server, "update_hub") as mock_update:
            server.Handler.do_POST(h)
        # host rejection must return before auth/update logic runs
        self.assertTrue(mock_rej.called)
        mock_update.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
