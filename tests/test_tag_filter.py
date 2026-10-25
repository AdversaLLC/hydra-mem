"""Tests for item 1.4: search by tag. A tag is a concept, a file name or session-summary. The filter matches whole tags only, case-insensitively,
and (like every filter) applies while candidates are collected, before the top-N cut. Run: python3 -I tests/test_tag_filter.py
"""
import json
import os
import subprocess
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
from memsvc.client import ApiError, MemoryClient  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402
from memsvc.tools import TOOLS  # noqa: E402


class Service(ts.Base):
    def put(self, title, body, tags="", kind="note"):
        r = self.svc.add(self.claude, kind, title, body, project="p", tags=tags)
        self.assertEqual(r["outcome"], "add", r)
        return r["id"]

    def ids(self, query="", **kw):
        return [i["id"] for i in self.svc.search(self.claude, query, **kw)["items"]]

    def test_matches_whole_tags_only(self):
        a = self.put("Wrap-up one", "the first session wrapped up with a short list of what was decided", tags="session-summary,infra")
        b = self.put("Plain note", "an ordinary note about trellis wire that carries no special tag at all", tags="infra")
        c = self.put("Odd tag", "a note whose tag only contains the word as part of a longer tag name", tags="summary-of-things")
        self.assertEqual(self.ids(tag="session-summary"), [a])
        self.assertEqual(sorted(self.ids(tag="infra")), sorted([a, b]))
        self.assertEqual(self.ids(tag="summary"), [])                          # not a substring match
        self.assertEqual(self.ids(tag="sum"), [])
        self.assertEqual(self.ids(tag="summary-of-things"), [c])

    def test_case_and_spacing_do_not_matter(self):
        a = self.put("Spaced tags", "a note whose stored tag string has spaces and capitals in it", tags=" Infra , File:memsvc/service.py ")
        self.assertEqual(self.ids(tag="infra"), [a])
        self.assertEqual(self.ids(tag="FILE:MEMSVC/SERVICE.PY"), [a])

    def test_it_works_with_a_query_and_with_a_kind_and_in_a_listing(self):
        a = self.put("Tagged decision", "we decided to keep the cedar posts in gravel for longer life", tags="cedar", kind="decision")
        b = self.put("Tagged note", "cedar boards warp when stored flat in the sun over a summer", tags="cedar")
        self.put("Untagged", "cedar smells strong when freshly cut and keeps insects away")
        self.assertEqual(sorted(self.ids("cedar", tag="cedar")), sorted([a, b]))
        self.assertEqual(self.ids("cedar", tag="cedar", kind="decision"), [a])
        self.assertEqual(sorted(self.ids("", tag="cedar")), sorted([a, b]))

    def test_the_tag_filter_applies_before_the_candidate_cut(self):
        for i in range(60):
            filler = " ".join(f"c{i}x{j}" for j in range(10))
            self.put(f"Crowd {i}", f"marmalade {filler} marmalade jars marmalade lids")
        tail = " ".join(f"tail{j}" for j in range(60))
        needle = self.put("Needle", f"one note that mentions marmalade once among much other text {tail}", tags="needle-tag")
        self.assertEqual(self.ids("marmalade", tag="needle-tag"), [needle])

    def test_bad_tags_are_refused(self):
        for bad in ("a,b", "x" * 65, "line\nbreak", 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.svc.search(self.claude, "", tag=bad)
        self.assertEqual(self.svc.search(self.claude, "", tag="")["items"], [])        # an empty tag means no filter, and the store is empty here


class Surfaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.reg = Registry(d / "agents.json", d / "admin.token")
        cls.reg.ensure_admin()
        cls.claude = cls.reg.create_agent("claude", "trusted")
        svc = MemoryService(d / "graph.db", cls.reg)
        me = cls.reg.authenticate(cls.claude)
        cls.tagged = svc.add(me, "note", "Tagged over http", "a note carrying a tag so the route and the command line can find it", project="p", tags="route-tag")["id"]
        svc.add(me, "note", "Untagged over http", "a note without that tag that must not appear in the filtered results", project="p")
        cls.httpd = make_server(svc, cls.reg, port=0)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def test_route_and_client(self):
        c = MemoryClient(self.url, self.claude)
        self.assertEqual([i["id"] for i in c.search("", tag="route-tag")["items"]], [self.tagged])
        with self.assertRaises(ApiError) as e:
            c.search("", tag="a,b")
        self.assertEqual(e.exception.status, 400)

    def test_command_line(self):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude, "MEMSVC_HOME": self.tmp.name}
        out = subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), "search", "--tag", "route-tag"], capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Tagged over http", out.stdout)
        self.assertNotIn("Untagged over http", out.stdout)

    def test_the_tool_schema_offers_it(self):
        by = {t["function"]["name"]: t["function"] for t in TOOLS}
        self.assertIn("tag", by["memory_search"]["parameters"]["properties"])

    def test_the_viewer_search_tab_has_a_tag_box(self):
        html = (ROOT / "memsvc" / "ui.html").read_text(encoding="utf-8")
        self.assertIn("b.tag", html)


if __name__ == "__main__":
    unittest.main(verbosity=1)
