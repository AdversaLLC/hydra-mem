"""Tests for tools/evalkit.py (the floor/coverage sweep). Run: python3 -I tests/test_evalkit.py"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "gsd-hydra-learn" / "scripts"))
from memsvc.registry import Registry  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402
import evalkit as K  # noqa: E402


class FakeIndex:
    def __init__(self):
        self.docs = {}

    def upsert(self, docs):
        for d in docs:
            self.docs[d["id"]] = set(re.findall(r"[a-z0-9]+", d["text"].lower()))

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def query(self, text, project, limit):
        q = set(re.findall(r"[a-z0-9]+", text.lower()))
        return sorted([(i, len(q & w) / max(len(q | w), 1)) for i, w in self.docs.items() if q & w], key=lambda s: -s[1])[:limit]


class Sweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        reg = Registry(self.d / "a.json", self.d / "adm")
        self.agent = reg.authenticate(reg.create_agent("evalbot", "trusted"))
        self.reg = reg
        self.index = FakeIndex()
        seed = MemoryService(self.d / "g.db", reg, index=self.index)
        self.ids = {}
        for k, (t, b) in {"fw": ("Firewall rule", "the firewall drops icmpv6 neighbor solicitation in the drop zone"),
                          "gpu": ("GPU thermals", "both compute cards climb to eighty two degrees within ninety seconds of load"),
                          "key": ("Audit key", "the audit key lives next to the database file with owner only permissions")}.items():
            self.ids[k] = seed.add(self.agent, "insight", t, b, project="p")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, floor, cov):
        return MemoryService(self.d / "g.db", self.reg, index=self.index, floor=floor, fts_coverage=cov)

    def gold(self):
        pos = [{"q": "icmpv6 neighbor solicitation drop zone firewall", "relevant": [self.ids["fw"]], "kind": "easy"},
               {"q": "why do the compute cards get so hot under load", "relevant": [self.ids["gpu"]], "kind": "paraphrase"},
               {"q": "where is the audit key stored permissions", "relevant": [self.ids["key"]], "kind": "easy"}]
        neg = [{"q": "recipe for vegetarian lasagna with spinach", "relevant": [], "kind": "negative"},
               {"q": "capital city of australia", "relevant": [], "kind": "negative"}]
        return pos, neg

    def test_rows_report_each_setting_with_positive_and_negative_metrics(self):
        pos, neg = self.gold()
        rows = K.sweep(self.make, self.agent, pos, neg, floors=[0.0, 0.5], coverages=[0.0, 0.5], k=5)
        self.assertEqual(len(rows), 4)
        for r in rows:
            for key in ("floor", "coverage", "hit1", "hit5", "mrr", "neg_answered", "neg_n", "n_pos"):
                self.assertIn(key, r)
        self.assertEqual(rows[0]["neg_n"], 2)
        self.assertEqual(rows[0]["n_pos"], 3)

    def test_stricter_settings_never_answer_more_negatives(self):
        pos, neg = self.gold()
        rows = {(r["floor"], r["coverage"]): r for r in K.sweep(self.make, self.agent, pos, neg, floors=[0.0, 0.3, 0.6], coverages=[0.0, 0.5], k=5)}
        for cov in (0.0, 0.5):
            self.assertGreaterEqual(rows[(0.0, cov)]["neg_answered"], rows[(0.3, cov)]["neg_answered"])
            self.assertGreaterEqual(rows[(0.3, cov)]["neg_answered"], rows[(0.6, cov)]["neg_answered"])
        for fl in (0.0, 0.3, 0.6):
            self.assertGreaterEqual(rows[(fl, 0.0)]["neg_answered"], rows[(fl, 0.5)]["neg_answered"])

    def test_positive_found_when_settings_are_loose(self):
        pos, neg = self.gold()
        r = K.sweep(self.make, self.agent, pos, neg, floors=[0.0], coverages=[0.0], k=5)[0]
        self.assertGreaterEqual(r["hit5"], 2 / 3)

    def test_format_table_mentions_every_setting(self):
        pos, neg = self.gold()
        rows = K.sweep(self.make, self.agent, pos, neg, floors=[0.0, 0.5], coverages=[0.0], k=5)
        text = K.format_table(rows)
        self.assertIn("floor", text)
        self.assertEqual(len(text.strip().splitlines()), 1 + len(rows))


if __name__ == "__main__":
    unittest.main(verbosity=1)
