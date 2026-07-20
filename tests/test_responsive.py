"""Responsive layout contracts for short landscape screens."""

import unittest

import server


SHORT_LANDSCAPE = "@media(orientation:landscape) and (max-height:600px)"
WIDE_SHORT_LANDSCAPE = (
    "@media(orientation:landscape) and (max-height:600px) and (min-width:700px)"
)


class TestShortLandscapeDensity(unittest.TestCase):
    def test_density_is_scoped_to_short_landscape(self):
        self.assertIn(SHORT_LANDSCAPE, server.NAV_CSS)
        self.assertIn(SHORT_LANDSCAPE, server.BASE_CSS)
        self.assertIn("body{font-size:15px;line-height:1.5}", server.BASE_CSS)
        self.assertIn(".container{padding-top:1rem}", server.BASE_CSS)
        self.assertIn(".briefing-home-row{gap:.55rem;padding:.65rem .8rem}", server.BASE_CSS)

    def test_landscape_homepage_restores_columns_only_at_viable_width(self):
        self.assertIn(WIDE_SHORT_LANDSCAPE, server.BASE_CSS)
        landscape_override = server.BASE_CSS.split(WIDE_SHORT_LANDSCAPE, 1)[1]
        self.assertIn(
            ".home-dashboard{grid-template-columns:minmax(0,2fr) minmax(15rem,.95fr)}",
            landscape_override,
        )
        self.assertIn(".home-rail{grid-template-columns:1fr}", landscape_override)
        self.assertIn(".home-rail,.home-rail-block{min-width:0}", landscape_override)
        self.assertIn(".home-rail .section-head{flex-wrap:wrap}", landscape_override)

    def test_portrait_breakpoints_and_touch_targets_remain(self):
        self.assertIn("@media(max-width:820px)", server.BASE_CSS)
        self.assertIn("@media(max-width:640px)", server.BASE_CSS)
        self.assertIn("@media(max-width:720px)", server.NAV_CSS)
        self.assertIn("min-height:2.75rem", server.NAV_CSS)
        self.assertIn(".hub-filter{", server.BASE_CSS)
        self.assertIn(".hub-filter{display:flex", server.BASE_CSS)
        hub_filter = server.BASE_CSS.split(".hub-filter{display:flex", 1)[1].split("}", 1)[0]
        self.assertIn("min-height:2.75rem", hub_filter)

    def test_density_rules_do_not_truncate_content(self):
        compact_css = server.BASE_CSS.split(SHORT_LANDSCAPE, 1)[1]
        compact_css = compact_css.split("@media(prefers-reduced-motion", 1)[0]
        for forbidden in (
            "line-clamp",
            "text-overflow",
            "white-space:nowrap",
            "overflow:hidden",
            "display:none",
        ):
            self.assertNotIn(forbidden, compact_css)
        self.assertIn(".briefing-archive-card{min-height:0", compact_css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
