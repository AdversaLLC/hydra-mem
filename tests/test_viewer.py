"""Structural tests for the viewer features of memsvc/ui.html (feed, search, timeline, sessions, context preview).
Behaviour is checked in a real browser, see docs/CLAUDE-MEM-PARITY.md; these tests pin the contract the page must keep.
Run: python3 -I tests/test_viewer.py
"""
import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "memsvc" / "ui.html").read_text(encoding="utf-8")


class Viewer(unittest.TestCase):
    def test_new_tabs_exist_and_feed_is_the_landing_tab(self):
        for tab in ("Feed", "Search", "Sessions", "Context"):
            self.assertRegex(HTML, r"\b%s\s*:\s*function" % tab, tab)
        self.assertRegex(HTML, r'select\(\s*tok\.value\s*\?\s*"Feed"')

    def test_it_uses_the_three_step_endpoints(self):
        for path in ("/v1/search", "/v1/timeline", "/v1/observations", "/v1/context", "/v1/sessions"):
            self.assertIn(path, HTML, path)

    def test_the_owner_can_see_and_restore_rejected_notes(self):
        self.assertRegex(HTML, r"\bTrash\s*:\s*function")
        for needle in ("/v1/trash", "/v1/restore", "Restore"):
            self.assertIn(needle, HTML, needle)
        self.assertIn("kept", HTML.lower())                                   # the page tells the owner a rejection is not a deletion

    def test_the_page_says_when_vector_search_is_down(self):
        self.assertIn("degraded", HTML)
        self.assertIn("vector search is down", HTML.lower())
        self.assertIn("does not show", HTML)                                   # an empty degraded result is not proof that nothing is stored

    def test_the_header_shows_the_vector_tier_state_and_notes_without_vectors(self):
        self.assertIn("s.vectors", HTML)
        for needle in ("vector search off", "without a vector", "v.reason", "v.warnings"):
            self.assertIn(needle, HTML, needle)

    def test_the_page_warns_when_the_store_outgrows_the_listing_scan(self):
        for needle in ("s.scale", "store at its listing limit", "reads slowing", "r.scan_capped", "older notes are not listed"):
            self.assertIn(needle, HTML, needle)

    def test_the_feed_polls_only_while_visible_and_can_be_stopped(self):
        self.assertIn("visibilityState", HTML)
        self.assertIn("setInterval", HTML)
        self.assertIn("clearInterval", HTML)

    def test_cards_show_the_claude_mem_style_fields(self):
        for needle in ("handle", "read_tokens", "type-", "snippet", "load more", "economics"):
            self.assertIn(needle, HTML.lower() if needle == "load more" else HTML, needle)

    def test_theme_toggle_uses_session_storage_not_local_storage(self):
        self.assertIn("data-theme", HTML)
        self.assertNotIn("localStorage", HTML)

    def test_still_never_builds_markup_from_data_and_loads_nothing_remote(self):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, HTML, bad)
        self.assertEqual(re.findall(r"(?:src|href)\s*=\s*[\"']https?://", HTML), [])

    def test_the_layout_works_on_a_phone(self):
        self.assertIn('name="viewport"', HTML)
        self.assertRegex(HTML, r"@media\s*\(max-width")


if __name__ == "__main__":
    unittest.main(verbosity=1)
