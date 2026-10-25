"""Tests for item 1.2: handles everywhere. Packed text shows short #handles instead of 67-character ids (about 17 tokens saved per note on the real
store), every way of naming a note accepts a handle, and a clash is reported as ambiguous instead of being read as "missing". A hidden note must
never be revealed by a clash. Run: python3 -I tests/test_handles.py
"""
import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc import packer, render  # noqa: E402
from memsvc.client import ApiError, MemoryClient  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402


def h8(nid):
    return hashlib.sha1(nid.encode()).hexdigest()[:8]


class Base(ts.Base):
    def setUp(self):
        super().setUp()
        self.gpt = self.agent("gpt", "trusted")

    def put(self, agent, title, body, **kw):
        r = self.svc.add(agent, "note", title, body, project="p", **kw)
        self.assertIn(r["outcome"], ("add", "reference"), r)
        return r["id"]


class PackedText(Base):
    def test_recall_context_names_notes_by_handle_not_by_long_id(self):
        a = self.put(self.claude, "Quince jelly set", "quince jelly sets best with the skins left in during the long slow boil")
        r = self.svc.recall(self.claude, "quince jelly boil", budget_tokens=300)
        self.assertIn("#" + h8(a), r["context"])
        self.assertNotIn(a, r["context"])
        item = r["items"][0]
        self.assertEqual((item["id"], item["handle"]), (a, h8(a)))            # JSON keeps both

    def test_session_context_does_the_same(self):
        a = self.put(self.claude, "Medlar bletting", "medlars must blet for two weeks before they are soft enough to eat", tags="session-summary")
        ctx = self.svc.context(self.claude, project="p", budget_tokens=300)
        self.assertIn("#" + h8(a), ctx["context"])
        self.assertNotIn(a, ctx["context"])

    def test_more_notes_fit_in_the_same_budget(self):
        for i in range(30):
            self.put(self.claude, f"Orchard job {i} with a fairly long descriptive title", f"job number {i} " + " ".join(f"w{i}x{j}" for j in range(14)))
        got = self.svc.context(self.claude, project="p", budget_tokens=400)
        text = got["context"]
        self.assertGreater(len(got["items"]), 3)
        self.assertLessEqual(packer.est_tokens(text), 400)
        self.assertNotIn("note/", text)                                        # no long id survives in the text
        with_ids = text
        for it in got["items"]:
            with_ids = with_ids.replace("#" + it["handle"], it["id"])          # the very same block, written the old way
        self.assertGreater(packer.est_tokens(with_ids), 400)                   # it would not have fitted: handles buy room
        self.assertGreater(packer.est_tokens(with_ids) - packer.est_tokens(text), 8 * len(got["items"]))


class Resolving(Base):
    def test_get_accepts_a_handle_and_stays_scoped(self):
        a = self.put(self.claude, "Fig rule", "figs ripen unevenly and are picked a few at a time over three weeks")
        for ref in ("#" + h8(a), h8(a), "#" + h8(a).upper()):
            self.assertEqual(self.svc.get(self.claude, ref)["id"], a, ref)
        hidden = self.put(self.gpt, "Private fig", "a private scratch note about figs belonging only to gpt right now", scope="private")
        self.assertIsNone(self.svc.get(self.claude, "#" + h8(hidden)))
        self.assertIsNone(self.svc.get(self.claude, "deadbeef"))

    def test_a_clash_among_visible_notes_is_ambiguous_not_missing(self):
        a = self.put(self.claude, "Plum one", "plum trees thin their own fruit in a hot dry summer without help")
        b = self.put(self.claude, "Plum two", "plum stones crack open when the fruit is left to dry on the tree")
        self.svc.handle = lambda nid: "deadbeef" if nid in (a, b) else h8(nid)         # force the clash a 1% accident would cause
        res = self.svc.get_observations(self.claude, ["#deadbeef"])
        self.assertEqual((res["items"], res["missing"]), ([], []))
        self.assertEqual([x["ref"] for x in res["ambiguous"]], ["#deadbeef"])
        self.assertEqual(sorted(res["ambiguous"][0]["candidates"]), sorted([a, b]))
        tl = self.svc.timeline(self.claude, anchor="#deadbeef")
        self.assertEqual((tl["anchor"], tl["items"]), (None, []))
        self.assertEqual(sorted(tl["ambiguous"]), sorted([a, b]))
        with self.assertRaises(ValueError) as cm:
            self.svc.get(self.claude, "#deadbeef")
        self.assertIn("ambiguous", str(cm.exception))

    def test_a_hidden_note_in_a_clash_is_neither_revealed_nor_blocks_the_visible_one(self):
        a = self.put(self.claude, "Pear one", "pears ripen from the inside out so they are picked a week early")
        hidden = self.put(self.gpt, "Private pear", "a private scratch note about pears belonging only to gpt right now", scope="private")
        self.svc.handle = lambda nid: "cafe0001" if nid in (a, hidden) else h8(nid)
        res = self.svc.get_observations(self.claude, ["#cafe0001"])
        self.assertEqual([i["id"] for i in res["items"]], [a])
        self.assertEqual(res["ambiguous"], [])
        self.assertNotIn(hidden, str(res))

    def test_a_longer_prefix_resolves_a_clash(self):
        a = self.put(self.claude, "Apple one", "apples keep best when each one is wrapped in paper and stored cold")
        b = self.put(self.claude, "Apple two", "apples bruise easily so the pickers wear soft gloves all season")
        self.svc.handle = lambda nid: "feedface" if nid in (a, b) else h8(nid)
        long_a = hashlib.sha1(a.encode()).hexdigest()[:12]
        res = self.svc.get_observations(self.claude, ["#" + long_a])
        self.assertEqual([i["id"] for i in res["items"]], [a])
        self.assertEqual(res["ambiguous"], [])

    def test_a_full_id_is_never_ambiguous(self):
        a = self.put(self.claude, "Cherry rule", "cherries split when it rains just before they are ripe enough")
        self.assertEqual([i["id"] for i in self.svc.get_observations(self.claude, [a])["items"]], [a])


class Views(unittest.TestCase):
    def test_the_ambiguity_is_spelled_out(self):
        out = render.observations({"items": [], "missing": [], "skipped": [], "truncated": False, "total_read_tokens": 0,
                                   "ambiguous": [{"ref": "#deadbeef", "candidates": ["note/2026-10-22/a", "note/2026-10-22/b"]}]})
        self.assertIn("ambiguous", out.lower())
        self.assertIn("#deadbeef", out)
        self.assertIn("note/2026-10-22/a", out)
        self.assertIn("full id", out)

    def test_an_ambiguous_timeline_anchor_is_explained(self):
        out = render.timeline({"anchor": None, "items": [], "total_read_tokens": 0, "ambiguous": ["note/2026-10-22/a", "note/2026-10-22/b"]})
        self.assertIn("ambiguous", out.lower())
        self.assertIn("note/2026-10-22/b", out)


class Wire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.reg = Registry(d / "agents.json", d / "admin.token")
        cls.reg.ensure_admin()
        cls.claude = cls.reg.create_agent("claude", "trusted")
        svc = MemoryService(d / "graph.db", cls.reg)
        cls.nid = svc.add(cls.reg.authenticate(cls.claude), "note", "Lemon rule", "lemons need a sheltered sunny wall and regular light feeding", project="p")["id"]
        cls.httpd = make_server(svc, cls.reg, port=0)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def test_node_route_and_client_accept_handles(self):
        c = MemoryClient(self.url, self.claude)
        self.assertEqual(c.get("#" + h8(self.nid))["id"], self.nid)
        self.assertEqual(c.get(self.nid)["id"], self.nid)
        with self.assertRaises(ApiError) as e:
            c.get("#00000000")
        self.assertEqual(e.exception.status, 404)

    def test_the_search_to_get_loop_works_with_handles_only(self):
        c = MemoryClient(self.url, self.claude)
        hit = c.search("lemons sunny wall")["items"][0]
        self.assertEqual(c.get("#" + hit["handle"])["id"], self.nid)
        self.assertEqual([i["id"] for i in c.get_observations(["#" + hit["handle"]])["items"]], [self.nid])


if __name__ == "__main__":
    unittest.main(verbosity=1)
