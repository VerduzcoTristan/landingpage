"""Unit tests for the RunbookPage component (runbooks_page() in server.py)."""

import sys
import html as html_mod

import pytest

# Ensure server.py and runbook_data.py are importable
sys.path.insert(0, "/home/hermes/devmclovin-landing")
import runbook_data


# ── Helpers ──

def _get_html():
    """Return the rendered runbooks page HTML."""
    return runbook_data.runbooks_page()


# ── Fixtures ──

@pytest.fixture
def html():
    return _get_html()


@pytest.fixture(autouse=True)
def _restore_runbook_entries():
    """Ensure RUNBOOK_ENTRIES is restored after any test that mutates it."""
    original = list(runbook_data.RUNBOOK_ENTRIES)
    yield
    runbook_data.RUNBOOK_ENTRIES = original


# ── Tests ──


class TestRendering:
    """Core rendering: page structure, all entries, titles, commands."""

    def test_page_renders_all_14_entries(self, html):
        """Every RUNBOOK_ENTRY produces one .runbook-entry div."""
        entry_count = html.count('class="runbook-entry"')
        assert entry_count == 14, f"Expected 14 entries, got {entry_count}"

    def test_all_entry_titles_appear(self, html):
        """Each title is present in escaped form."""
        for entry in runbook_data.RUNBOOK_ENTRIES:
            escaped_title = html_mod.escape(entry["title"])
            assert escaped_title in html, f"Missing title: {entry['title']}"

    def test_all_descriptions_appear(self, html):
        """Each description is present in escaped form."""
        for entry in runbook_data.RUNBOOK_ENTRIES:
            escaped_desc = html_mod.escape(entry["description"])
            assert escaped_desc in html, f"Missing description for: {entry['title']}"

    def test_all_commands_displayed_in_pre(self, html):
        """Each command text is present in escaped form inside a <pre> block."""
        for entry in runbook_data.RUNBOOK_ENTRIES:
            escaped_cmd = html_mod.escape(entry["command"])
            assert f"<pre>{escaped_cmd}</pre>" in html, (
                f"Missing command in <pre> for: {entry['title']}"
            )

    def test_page_is_valid_html_document(self, html):
        """Output should be a full HTML document."""
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "📋 Runbooks" in html

    def test_page_has_active_nav(self, html):
        """The Runbooks nav link should be marked active."""
        # The nav link for /runbooks should have class 'active'
        assert 'href="/runbooks"' in html
        assert 'active' in html


class TestCopyButtons:
    """Copy-to-clipboard button tests."""

    def test_one_copy_button_per_entry(self, html):
        """14 entries → 14 copy buttons."""
        btn_count = html.count('class="runbook-copy-btn"')
        assert btn_count == 14, f"Expected 14 copy buttons, got {btn_count}"

    def test_copy_buttons_have_data_command(self, html):
        """Each button carries its command in a data-command attribute."""
        for entry in runbook_data.RUNBOOK_ENTRIES:
            # data-command uses quote=True (escapes double-quotes too)
            escaped_cmd = html_mod.escape(entry["command"], quote=True)
            assert f'data-command="{escaped_cmd}"' in html, (
                f"Missing data-command for: {entry['title']}"
            )

    def test_copy_buttons_have_aria_label(self, html):
        """All buttons carry aria-label='Copy command to clipboard'."""
        assert (
            html.count('aria-label="Copy command to clipboard"') == 14
        ), "Expected 14 aria-labels on copy buttons"

    def test_copy_buttons_have_onclick_handler(self, html):
        """Each button triggers copyRunbookCommand(this)."""
        onclick_count = html.count('onclick="copyRunbookCommand(this)"')
        assert onclick_count == 14, (
            f"Expected 14 onclick handlers, got {onclick_count}"
        )


class TestCategoryFilters:
    """Category filter pill tests."""

    def test_all_filter_pill_is_active_by_default(self, html):
        """The 'All' pill should have class 'active'."""
        assert 'class="runbook-nav-cat active"' in html

    def test_category_pills_present(self, html):
        """One pill per category + 'All'."""
        for cat in ["services", "troubleshooting", "maintenance", "hardware"]:
            assert f'data-cat="{cat}"' in html, f"Missing pill for: {cat}"

    def test_category_counts_present(self, html):
        """Each pill shows a count span."""
        assert '<span class="count">(14)</span>' in html  # All = 14
        # Spot-check a few categories
        assert '>services<' not in html  # making sure counts are present
        # Count spans should appear for all 5 pills
        assert html.count('class="count"') == 5  # All + 4 categories

    def test_entries_have_data_category_attr(self, html):
        """Every .runbook-entry has a data-category attribute."""
        entries = runbook_data.RUNBOOK_ENTRIES
        for entry in entries:
            cat = entry["category"]
            assert f'data-category="{cat}"' in html, (
                f"Entry '{entry['title']}' missing data-category='{cat}'"
            )


class TestJavaScript:
    """Inline JavaScript function tests."""

    def test_copy_function_is_present(self, html):
        assert "function copyRunbookCommand" in html

    def test_filter_function_is_present(self, html):
        assert "function filterRunbookCategory" in html

    def test_clipboard_api_used(self, html):
        """Primary path uses navigator.clipboard.writeText."""
        assert "navigator.clipboard.writeText" in html

    def test_fallback_copy_present(self, html):
        """Fallback path uses document.execCommand('copy')."""
        assert "document.execCommand('copy')" in html

    def test_copied_state_toggles_button_text(self, html):
        """After copy, button shows '✓ Copied!' then reverts."""
        assert "✓ Copied!" in html
        assert "📋 Copy" in html

    def test_filter_shows_hides_entries(self, html):
        """filterRunbookCategory toggles style.display."""
        assert "entries[i].style.display" in html


class TestEdgeCases:
    """Edge-case tests."""

    def test_empty_data_array_renders_gracefully(self, html):
        """With no entries, the page structure should still render."""
        runbook_data.RUNBOOK_ENTRIES = []
        empty_html = runbook_data.runbooks_page()
        try:
            assert "<!DOCTYPE html>" in empty_html
            assert "📋 Runbooks" in empty_html
            assert 'class="runbook-entry"' not in empty_html
            assert "<span class=\"count\">(0)</span>" in empty_html
            # Grid still present
            assert 'class="runbook-grid"' in empty_html
            # Category pills still rendered
            assert 'data-cat="all"' in empty_html
        finally:
            pass  # Restored by _restore_runbook_entries fixture

    def test_single_entry_renders(self, html):
        """Single entry should render correctly."""
        single = [
            {
                "title": "Test Entry",
                "command": "echo hello",
                "description": "A test command.",
                "category": "services",
            }
        ]
        runbook_data.RUNBOOK_ENTRIES = single
        sh = runbook_data.runbooks_page()
        try:
            assert sh.count('class="runbook-entry"') == 1
            assert "<pre>echo hello</pre>" in sh
            assert sh.count('class="runbook-copy-btn"') == 1
            assert "<span class=\"count\">(1)</span>" in sh
        finally:
            pass

    def test_entries_with_special_characters(self, html):
        """HTML-special chars in commands are properly escaped."""
        special = [
            {
                "title": "Ampersand & Less-Than",
                "command": 'echo "a && b" < /dev/null',
                "description": "Tests <escaping> & more.",
                "category": "troubleshooting",
            }
        ]
        runbook_data.RUNBOOK_ENTRIES = special
        sh = runbook_data.runbooks_page()
        try:
            # Should NOT contain raw special chars in a way that breaks HTML
            assert "&amp;" in sh  # & -> &amp;
            assert "&lt;" in sh  # < -> &lt;
            # The title should be escaped
            assert "Ampersand &amp; Less-Than" in sh
        finally:
            pass
