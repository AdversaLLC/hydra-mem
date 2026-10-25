"""Tests for item 1.3: each hit says which search found it (keyword, vector, both, or graph for a note pulled in through a link), so a reader can weigh
a keyword-only hit differently from one that both lanes agree on. Run: python3 -I tests/test_found_by.py
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc import render  # noqa: E402


class ScriptedIndex:
    """A vector tier that returns exactly the hits it is told to, so a note can be found by meaning alone."""
    backend = "onnx"

    def __init__(self):
        self.hits = []

    def upsert(self, docs): pass
    def delete(self, ids): pass
    def query(self, text, project, limit): return list(self.hits)


class FoundBy(ts.Base):
    def setUp(self):
        super().setUp()
        self.vec = ScriptedIndex()
        self.svc.index = self.vec

    def put(self, title, body, **kw):
        r = self.svc.add(self.claude, "note", title, body, project="p", **kw)
        self.assertEqual(r["outcome"], "add", r)
        return r["id"]

    def world(self):
        kw = self.put("Words only", "persimmon grafting is done in early spring with a whip and tongue cut")
        both = self.put("Both lanes", "persimmon trees need a long warm autumn to colour and sweeten the fruit")
        vec = self.put("Meaning only", "an astringent orange fruit loses its pucker once it has softened on the shelf")
        self.vec.hits = [(both, 0.9), (vec, 0.8)]
        return kw, both, vec

    def test_search_labels_each_hit_with_its_lane(self):
        kw, both, vec = self.world()
        got = {i["id"]: i["found_by"] for i in self.svc.search(self.claude, "persimmon")["items"]}
        self.assertEqual(got, {kw: "keyword", both: "both", vec: "vector"})

    def test_recall_labels_matches_and_marks_linked_notes_as_graph(self):
        kw, both, vec = self.world()
        linked = self.put("Linked rule", "a note about quince storage that shares no words with the question at all")
        self.svc.link(self.claude, both, linked, "supports")
        items = {i["id"]: i for i in self.svc.recall(self.claude, "persimmon", limit=10)["items"]}
        self.assertEqual(items[kw]["found_by"], "keyword")
        self.assertEqual(items[both]["found_by"], "both")
        self.assertEqual(items[vec]["found_by"], "vector")
        self.assertEqual(items[linked]["found_by"], "graph")

    def test_a_listing_with_no_query_has_no_lane_to_report(self):
        self.world()
        for i in self.svc.search(self.claude, "")["items"]:
            self.assertNotIn("found_by", i)

    def test_the_text_view_shows_a_short_marker(self):
        base = {"id": "note/x", "handle": "a1b2c3d4", "ts": "2026-10-22T16:15:17Z", "kind": "note", "conf": "U", "trust": "agent", "project": "p",
                "agent": "claude", "title": "T", "snippet": "s", "tags": "", "read_tokens": 10}
        res = lambda it: {"items": [it], "sufficient": True, "total": 1, "next_offset": None, "total_read_tokens": 10, "degraded": []}  # noqa: E731
        self.assertRegex(render.index(res(dict(base, found_by="keyword"))), r"· kw\b")
        self.assertRegex(render.index(res(dict(base, found_by="vector"))), r"· vec\b")
        self.assertRegex(render.index(res(dict(base, found_by="both"))), r"· both\b")
        self.assertNotIn("·", "\n".join(l for l in render.index(res(base)).splitlines() if l.startswith("  #")))


if __name__ == "__main__":
    unittest.main(verbosity=1)
