"""Tests for the /hub page render logic in server.py (Step 1.4).

Covers:
  - _merge_hub_entries(): recency grouping, status_override "done" forcing,
    curated order sorting, missing full_name skipping, empty repos, and that
    load_hub() dict keyed by full_name is read correctly.
  - hub_page(): HTML string output, title, group headings only for non-empty
    groups, escaped repo names, "Summarizing…" placeholder with data-summary,
    Curate link, banner rendering, empty-state, "Needs attention", done pill,
    and the JS poll snippet (fetch + textContent).
  - _hub_card_html(): linked vs plain name, commit subjects, language omission,
    and XSS escaping of injected <script>.

Both get_hub_repos and load_hub are patched at the server module level so no
network calls or real data files are touched.
"""

import os
import sys
import unittest
from unittest.mock import patch

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


def repos_payload(repos, status="ok", banner=None, ts=1234, state="ready", version=1):
    return {"repos": repos, "status": status, "banner": banner, "ts": ts,
            "state": state, "version": version}


# ── _merge_hub_entries tests ─────────────────────────────────────────────────

class TestMergeHubEntries(unittest.TestCase):

    def _merged(self, repos, curated):
        with patch.object(server, "get_hub_repos", return_value=repos_payload(repos)), \
             patch.object(server, "load_hub", return_value=curated):
            return server._merge_hub_entries()

    def test_groups_by_recency(self):
        repos = [
            make_repo("a/active", recency="active"),
            make_repo("b/maintain", recency="maintain"),
            make_repo("c/stalled", recency="stalled"),
        ]
        m = self._merged(repos, {})
        groups = m["groups"]
        self.assertEqual([e["full_name"] for e in groups["active"]], ["a/active"])
        self.assertEqual([e["full_name"] for e in groups["maintain"]], ["b/maintain"])
        self.assertEqual([e["full_name"] for e in groups["stalled"]], ["c/stalled"])
        self.assertEqual(groups["done"], [])

    def test_status_override_done_forces_done_group(self):
        repos = [make_repo("a/repo", recency="active")]
        curated = {"a/repo": {"status_override": "done"}}
        m = self._merged(repos, curated)
        groups = m["groups"]
        self.assertEqual([e["full_name"] for e in groups["done"]], ["a/repo"])
        self.assertEqual(groups["active"], [])

    def test_curated_order_sorts_before_default_999(self):
        repos = [
            make_repo("z/last", recency="active"),
            make_repo("a/first", recency="active"),
            make_repo("m/mid", recency="active"),
        ]
        curated = {
            "a/first": {"order": 1},
            "m/mid": {"order": 5},
            # z/last has no order -> defaults to 999
        }
        m = self._merged(repos, curated)
        order = [e["full_name"] for e in m["groups"]["active"]]
        self.assertEqual(order, ["a/first", "m/mid", "z/last"])

    def test_missing_full_name_skipped(self):
        repos = [
            make_repo("good/repo", recency="active"),
            {"name": "no-full-name", "recency": "active"},  # no full_name
            {"full_name": "   ", "recency": "active"},       # blank full_name
        ]
        m = self._merged(repos, {})
        names = [e["full_name"] for e in m["groups"]["active"]]
        self.assertEqual(names, ["good/repo"])

    def test_empty_repos_all_empty_groups(self):
        m = self._merged([], {})
        for g in ("active", "maintain", "stalled", "done"):
            self.assertEqual(m["groups"][g], [], f"group {g} should be empty")
        self.assertEqual(m["status"], "ok")

    def test_load_hub_dict_read_correctly(self):
        repos = [make_repo("a/repo", description="gh desc", recency="active")]
        curated = {
            "a/repo": {
                "goal": "curated goal",
                "order": 3,
                "status_override": "done",
            }
        }
        m = self._merged(repos, curated)
        e = m["groups"]["done"][0]
        self.assertEqual(e["description"], "curated goal")   # goal overrides gh desc
        self.assertEqual(e["order"], 3)
        self.assertEqual(e["status_override"], "done")
        self.assertTrue(e["has_note"])                       # goal present -> has_note

    def test_status_and_banner_passthrough(self):
        repos = [make_repo("a/repo", recency="active")]
        with patch.object(server, "get_hub_repos",
                          return_value=repos_payload(repos, status="degraded", banner="API slow")), \
             patch.object(server, "load_hub", return_value={}):
            m = server._merge_hub_entries()
        self.assertEqual(m["status"], "degraded")
        self.assertEqual(m["banner"], "API slow")


# ── hub_page tests ───────────────────────────────────────────────────────────

class TestHubPage(unittest.TestCase):

    def _page(self, repos, curated):
        with patch.object(server, "get_hub_repos", return_value=repos_payload(repos)), \
             patch.object(server, "load_hub", return_value=curated):
            return server.hub_page()

    def test_returns_html_string(self):
        html = self._page([make_repo("a/repo", recency="active")], {})
        self.assertIsInstance(html, str)
        self.assertIn("<!DOCTYPE html>", html)

    def test_contains_hub_title(self):
        html = self._page([make_repo("a/repo", recency="active")], {})
        self.assertIn("Hub", html)
        self.assertIn("Control Center", html)

    def test_group_headings_only_for_non_empty(self):
        repos = [
            make_repo("a/active", recency="active"),
            make_repo("b/stalled", recency="stalled"),
        ]
        html = self._page(repos, {})
        self.assertIn("Active", html)
        self.assertIn("Stalled", html)
        self.assertNotIn("Maintaining", html)   # no maintain repos
        self.assertNotIn(">Done<", html)        # no done repos

    def test_repo_name_escaped(self):
        repos = [make_repo("a/repo&<x>", recency="active")]
        html = self._page(repos, {})
        self.assertIn("a/repo&amp;&lt;x&gt;", html)
        self.assertNotIn("a/repo&<x>", html)

    def test_summarizing_placeholder_with_data_summary(self):
        repos = [make_repo("a/repo", recency="active")]
        html = self._page(repos, {})
        self.assertIn("Summarizing", html)
        self.assertIn('data-summary="a/repo"', html)

    def test_curate_link_to_admin_anchor(self):
        repos = [make_repo("a/repo", recency="active")]
        html = self._page(repos, {})
        self.assertIn('href="/hub/admin#a/repo"', html)

    def test_banner_rendered(self):
        repos = [make_repo("a/repo", recency="active")]
        with patch.object(server, "get_hub_repos",
                          return_value=repos_payload(repos, banner="Heads up!")), \
             patch.object(server, "load_hub", return_value={}):
            html = server.hub_page()
        self.assertIn("Heads up!", html)

    def test_empty_state_when_no_repos(self):
        html = self._page([], {})
        self.assertIn("No projects yet", html)
        self.assertIn("GITHUB_TOKEN", html)

    def test_first_load_shows_refresh_state_and_polls_state_endpoint(self):
        payload = repos_payload([], status="refreshing", banner="Refreshing GitHub activity…",
                                state="refreshing")
        with patch.object(server, "get_hub_repos", return_value=payload), patch.object(
            server, "load_hub", return_value={}
        ):
            page = server.hub_page()
        self.assertIn("Loading GitHub activity", page)
        self.assertIn('fetch("/api/hub/state")', page)
        self.assertIn("window.location.reload()", page)

    def test_stalled_no_note_shows_needs_attention(self):
        repos = [make_repo("a/repo", recency="stalled")]
        html = self._page(repos, {})
        self.assertIn("Needs attention", html)

    def test_done_override_shows_done_pill(self):
        repos = [make_repo("a/repo", recency="active")]
        curated = {"a/repo": {"status_override": "done"}}
        html = self._page(repos, curated)
        # The done pill text is "done"
        self.assertIn(">done<", html)


# ── _hub_card_html tests ─────────────────────────────────────────────────────

class TestHubCardHtml(unittest.TestCase):

    def _card(self, entry):
        return server._hub_card_html(entry)

    def test_name_linked_when_html_url_present(self):
        e = {"full_name": "a/repo", "name": "repo", "html_url": "https://github.com/a/repo",
             "description": "", "language": None, "recency": "active", "commits": [],
             "order": 999, "has_note": False, "status_override": ""}
        card = self._card(e)
        self.assertIn('<a href="https://github.com/a/repo"', card)
        self.assertIn(">repo<", card)

    def test_name_plain_when_html_url_absent(self):
        e = {"full_name": "a/repo", "name": "repo", "html_url": "",
             "description": "", "language": None, "recency": "active", "commits": [],
             "order": 999, "has_note": False, "status_override": ""}
        card = self._card(e)
        self.assertNotIn("<a href", card)
        self.assertIn("<h2>repo</h2>", card)

    def test_commit_subjects_rendered(self):
        e = {"full_name": "a/repo", "name": "repo", "html_url": "u",
             "description": "", "language": None, "recency": "active",
             "commits": [{"sha": "1", "subject": "first"}, {"sha": "2", "subject": "second"}],
             "order": 999, "has_note": False, "status_override": ""}
        card = self._card(e)
        self.assertIn("<li>first</li>", card)
        self.assertIn("<li>second</li>", card)

    def test_language_omitted_when_none(self):
        e = {"full_name": "a/repo", "name": "repo", "html_url": "u",
             "description": "", "language": None, "recency": "active", "commits": [],
             "order": 999, "has_note": False, "status_override": ""}
        card = self._card(e)
        self.assertNotIn("hub-lang", card)

    def test_xss_name_injected_is_escaped(self):
        e = {"full_name": "a/repo", "name": '<script>alert(1)</script>', "html_url": "u",
             "description": "", "language": None, "recency": "active", "commits": [],
             "order": 999, "has_note": False, "status_override": ""}
        card = self._card(e)
        self.assertIn("&lt;script&gt;", card)
        self.assertNotIn("<script>alert(1)</script>", card)


# ── JS poll snippet tests ────────────────────────────────────────────────────

class TestHubPageJsPoll(unittest.TestCase):

    def test_js_poll_fetches_summaries_and_uses_textcontent(self):
        with patch.object(server, "get_hub_repos",
                          return_value=repos_payload([make_repo("a/repo", recency="active")])), \
             patch.object(server, "load_hub", return_value={}):
            html = server.hub_page()
        self.assertIn('fetch("/api/hub/summaries")', html)
        self.assertIn("textContent", html)
        self.assertIn('states[fn]==="fallback"', html)
        self.assertIn("el.remove()", html)
        self.assertNotIn("innerHTML", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
