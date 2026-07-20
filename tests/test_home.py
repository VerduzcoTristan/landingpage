"""Regression tests for the briefing-first homepage composition."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import server


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 7, 20, 9, 0, 0)
        return value.replace(tzinfo=tz) if tz else value


def story(index):
    return {
        "title": f"Story {index}",
        "source_url": f"https://example.com/{index}",
        "summary": f"Summary {index}",
        "categories": "coding",
    }


class TodayArchive:
    def get_briefing(self, date):
        return {"date": date, "articles": [story(i) for i in range(7)]}


class RecentArchive:
    def get_briefing(self, date):
        if date == "2026-07-18":
            return {"date": date, "full_date": "Saturday, July 18, 2026",
                    "articles": [story(99)]}
        return None

    def get_briefings(self, limit=1):
        return [{"date": "2026-07-18"}]


class TestHomepageBriefingSelection(unittest.TestCase):
    def test_today_selection_is_preserved_and_preview_is_capped_at_five(self):
        with patch.object(server, "datetime", FrozenDateTime), patch.object(
            server, "_get_archive", return_value=TodayArchive()
        ), patch.object(server, "home_focus_projects", return_value="focus"):
            page = server.home_page()
        for index in range(5):
            self.assertIn(f"Story {index}", page)
        self.assertNotIn("Story 5", page)
        self.assertNotIn("Story 6", page)
        self.assertIn("Today's Briefing", page)

    def test_recent_database_fallback_is_preserved_when_today_is_missing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            server, "datetime", FrozenDateTime
        ), patch.object(server, "_get_archive", return_value=RecentArchive()), patch.object(
            server, "BRIEFING_DIR", Path(folder)
        ), patch.object(server, "home_focus_projects", return_value="focus"):
            page = server.home_page()
        self.assertIn("Story 99", page)
        self.assertIn("Saturday, July 18, 2026", page)

    def test_daily_layout_keeps_briefing_primary_with_monitoring_and_focus_rail(self):
        with patch.object(server, "datetime", FrozenDateTime), patch.object(
            server, "_get_archive", return_value=TodayArchive()
        ), patch.object(server, "home_focus_projects", return_value="FOCUS-CONTENT"):
            page = server.home_page()
        self.assertIn('class="home-dashboard"', page)
        self.assertIn('class="home-primary"', page)
        self.assertIn('class="home-rail"', page)
        self.assertIn("Monitoring", page)
        self.assertIn("FOCUS-CONTENT", page)


class TestHomepageFocus(unittest.TestCase):
    def test_focus_projects_are_limited_to_four_and_exclude_done(self):
        def entry(index, group="active"):
            return {
                "full_name": f"owner/repo-{index}", "name": f"repo-{index}",
                "whats_next": f"next-{index}", "goal": "", "description": "",
                "recency": group, "status_override": "", "attention_reasons": [],
            }

        merged = {"groups": {
            "active": [entry(i) for i in range(5)],
            "maintain": [entry(5, "maintain")],
            "stalled": [],
            "done": [entry(6, "done")],
        }}
        with patch.object(server, "_merge_hub_entries", return_value=merged):
            output = server.home_focus_projects()
        for index in range(4):
            self.assertIn(f"repo-{index}", output)
        self.assertNotIn("repo-4", output)
        self.assertNotIn("repo-6", output)

    def test_mobile_navigation_stays_in_one_row_with_touch_targets(self):
        mobile = server.NAV_CSS.split("@media(max-width:720px)", 1)[1]
        self.assertNotIn("flex-direction:column", mobile.split("}", 1)[0])
        self.assertIn("min-height:2.75rem", mobile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
