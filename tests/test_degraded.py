"""Tests for item 1.1: say when an answer is degraded. "Nothing relevant is stored", "the vector search is down" and "an error"
are three different answers; a keyword-only result must never pass for a full one, and an empty keyword result must not be
presented as proof that nothing is stored. Run: python3 -I tests/test_degraded.py
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
from memsvc import render  # noqa: E402
from memsvc.client import MemoryClient  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402


class FailingIndex(ts.FakeIndex):
    """A configured vector tier (it has a backend) that can be switched to fail, like a dead embedding daemon."""
    backend = "onnx"

    def __init__(self):
        super().__init__()
        self.fail = True

    def query(self, text, project, limit):
        if self.fail:
            raise RuntimeError("daemon down")
        return super().query(text, project, limit)


class Service(ts.Base):
    def setUp(self):
        super().setUp()
        self.vec = FailingIndex()
        self.svc.index = self.vec
        self.svc.add(self.claude, "note", "Orchard rule", "pruning the orchard happens in late winter before the buds open", project="p")

    def test_a_failing_vector_lane_is_reported_by_search_and_recall(self):
        s = self.svc.search(self.claude, "orchard pruning")
        self.assertEqual(s["degraded"], ["vector"])
        self.assertTrue(s["items"])                                           # the keyword lane still answers
        r = self.svc.recall(self.claude, "orchard pruning")
        self.assertEqual(r["degraded"], ["vector"])
        self.assertTrue(r["items"])

    def test_a_healthy_lane_is_not_degraded(self):
        self.vec.fail = False
        self.assertEqual(self.svc.search(self.claude, "orchard pruning")["degraded"], [])
        self.assertEqual(self.svc.recall(self.claude, "orchard pruning")["degraded"], [])

    def test_no_vector_tier_by_design_is_not_degraded(self):
        plain = MemoryService(Path(self.tmp.name) / "plain.db", self.reg, clock=lambda: self.now[0])         # NullIndex: vectors were never configured
        plain.add(self.claude, "note", "Orchard rule", "pruning the orchard happens in late winter before the buds open", project="p")
        self.assertEqual(plain.search(self.claude, "orchard pruning")["degraded"], [])
        self.assertEqual(plain.recall(self.claude, "orchard pruning")["degraded"], [])

    def test_empty_result_while_degraded_is_flagged_not_called_no_evidence(self):
        s = self.svc.search(self.claude, "quantum zebra")
        self.assertEqual((s["items"], s["sufficient"], s["degraded"]), ([], False, ["vector"]))

    def test_listing_without_a_query_never_touches_the_vector_lane(self):
        self.assertEqual(self.svc.search(self.claude, "")["degraded"], [])

    def test_a_degraded_recall_is_not_cached(self):
        self.svc.recall(self.claude, "orchard pruning")
        self.assertEqual(self.svc.cache_stats()["size"], 0)                    # served again after recovery, not frozen as keyword-only
        self.vec.fail = False
        self.assertEqual(self.svc.recall(self.claude, "orchard pruning")["degraded"], [])
        self.assertEqual(self.svc.cache_stats()["size"], 1)

    def test_timeline_by_query_reports_it_too(self):
        tl = self.svc.timeline(self.claude, query="orchard pruning")
        self.assertEqual(tl.get("degraded"), ["vector"])


class Views(unittest.TestCase):
    ITEM = {"id": "note/2026-10-22/a", "handle": "a1b2c3d4", "ts": "2026-10-22T16:15:17Z", "kind": "note", "conf": "U", "trust": "agent", "project": "p",
            "agent": "claude", "title": "Orchard rule", "snippet": "pruning in late winter", "tags": "", "read_tokens": 20}

    def res(self, items, **kw):
        out = {"items": items, "sufficient": bool(items), "total": len(items), "next_offset": None, "total_read_tokens": 20 * len(items), "degraded": []}
        out.update(kw)
        return out

    def test_results_say_they_are_keyword_only(self):
        out = render.index(self.res([self.ITEM], degraded=["vector"]))
        self.assertIn("keyword results only", out)
        self.assertIn("#a1b2c3d4", out)

    def test_an_empty_degraded_result_does_not_claim_nothing_is_stored(self):
        out = render.index(self.res([], degraded=["vector"]))
        self.assertIn("vector search is down", out)
        self.assertIn("does not show", out)
        self.assertNotIn("Do not guess; say you do not know", out)

    def test_a_healthy_empty_result_still_says_insufficient_evidence(self):
        self.assertIn("insufficient evidence", render.index(self.res([])).lower())

    def test_recall_text_has_the_same_three_states(self):
        ok = {"sufficient": True, "context": "[memory: x]\n- a", "degraded": []}
        self.assertEqual(render.recall(ok), "[memory: x]\n- a")
        self.assertIn("keyword results only", render.recall(dict(ok, degraded=["vector"])))
        self.assertIn("insufficient evidence", render.recall({"sufficient": False, "context": "", "degraded": []}).lower())
        self.assertIn("does not show", render.recall({"sufficient": False, "context": "", "degraded": ["vector"]}))


class Wire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.reg = Registry(d / "agents.json", d / "admin.token")
        cls.reg.ensure_admin()
        cls.claude = cls.reg.create_agent("claude", "trusted")
        svc = MemoryService(d / "graph.db", cls.reg, index=FailingIndex())
        svc.add(cls.reg.authenticate(cls.claude), "note", "Orchard rule", "pruning the orchard happens in late winter before the buds open", project="p")
        cls.httpd = make_server(svc, cls.reg, port=0)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def bridge(self, calls):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude}
        msgs = [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls)]
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, env=e, timeout=60)
        return {m["id"]: m["result"]["content"][0]["text"] for m in (json.loads(l) for l in p.stdout.splitlines() if l.strip())}

    def test_http_and_mcp_carry_the_flag(self):
        c = MemoryClient(self.url, self.claude)
        self.assertEqual(c.search("orchard pruning")["degraded"], ["vector"])
        self.assertEqual(c.recall("orchard pruning")["degraded"], ["vector"])
        out = self.bridge([("memory_search", {"query": "orchard pruning"}), ("memory_recall", {"query": "orchard pruning"}),
                           ("memory_search", {"query": "quantum zebra"}), ("memory_recall", {"query": "quantum zebra"})])
        self.assertIn("keyword results only", out[1])
        self.assertIn("keyword results only", out[2])
        self.assertIn("does not show", out[3])
        self.assertIn("does not show", out[4])


if __name__ == "__main__":
    unittest.main(verbosity=1)
