"""Tests for item 1.7, the service side: what the owner is told about the vector tier. A note with no vector is found by keyword only, so recall
misses it by meaning without any error; vectors from two embedders mix without any error either. `vector_status` makes both visible in stats,
and `vector_gaps` is the same count for `memctl doctor`, which runs without the service. Run: python3 -I tests/test_vector_status.py
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc.service import MemoryService, vector_gaps  # noqa: E402
from memsvc.vector import VectorError  # noqa: E402


class Index(ts.FakeIndex):
    backend = "fake"
    fp = {"state": "match", "cosine": 1.0, "reason": None}
    boom = None

    def health(self):
        if self.boom:
            raise self.boom
        return {"backend": "fake", "running": True, "fingerprint": self.fp}


class Gaps(ts.Base):
    def service(self, index):
        return MemoryService(Path(self.tmp.name) / "graph.db", self.reg, index=index, clock=lambda: self.now[0])

    def fill(self, svc, n=3):
        return [svc.add(self.claude, "note", f"Note {i}", f"a distinct fact number {i} about topic{i} and nothing else", project="p")["id"] for i in range(n)]

    def test_no_index_means_every_active_note_is_keyword_only(self):
        svc = MemoryService(Path(self.tmp.name) / "graph.db", self.reg)
        self.fill(svc)
        v = svc.vector_status(self.admin)
        self.assertEqual((v["state"], v["active"], v["missing"]), ("off", 3, 3))
        self.assertIn("keyword", v["reason"])

    def test_a_healthy_index_with_every_note_embedded(self):
        svc = self.service(Index())
        self.fill(svc)
        v = svc.vector_status(self.admin)
        self.assertEqual((v["state"], v["active"], v["with_vector"], v["missing"], v["stale"]), ("ok", 3, 3, 0, 0))
        self.assertEqual(v["embedders"], {"fake": 3})
        self.assertEqual(v["warnings"], [])
        self.assertEqual(v["fingerprint"]["state"], "match")

    def test_a_daemon_that_has_not_started_is_idle_not_broken(self):
        idx = Index()
        idx.fp = None
        v = self.service(idx).vector_status(self.admin)
        self.assertEqual(v["state"], "idle")

    def test_a_fingerprint_mismatch_is_blocked_with_its_reason(self):
        idx = Index()
        idx.fp = {"state": "mismatch", "cosine": 0.4, "reason": "the embedder changed: the canary text now embeds at cosine 0.400"}
        v = self.service(idx).vector_status(self.admin)
        self.assertEqual(v["state"], "blocked")
        self.assertIn("embedder changed", v["reason"])
        idx.fp = {"state": "unreadable", "cosine": None, "reason": "cannot read memsvc-embedder.json"}
        self.assertEqual(self.service(idx).vector_status(self.admin)["state"], "blocked")

    def test_a_pinned_first_run_is_fine(self):
        idx = Index()
        idx.fp = {"state": "pinned", "cosine": None, "reason": None}
        self.assertEqual(self.service(idx).vector_status(self.admin)["state"], "ok")

    def test_an_unreachable_daemon_is_down_with_the_error(self):
        idx = Index()
        idx.boom = VectorError("vector daemon timed out")
        v = self.service(idx).vector_status(self.admin)
        self.assertEqual(v["state"], "down")
        self.assertIn("timed out", v["reason"])

    def test_a_note_without_a_vector_is_counted_and_named_for_the_owner_only(self):
        svc = self.service(Index())
        ids = self.fill(svc)
        g = svc._g()
        g.db.execute("DELETE FROM vectors WHERE node_id=?", (ids[1],))
        g.commit()
        g.db.close()
        v = svc.vector_status(self.admin)
        self.assertEqual((v["missing"], v["with_vector"], v["missing_ids"]), (1, 2, [ids[1]]))
        self.assertTrue(any("1 active note" in w and "keyword" in w for w in v["warnings"]), v["warnings"])
        self.assertNotIn("missing_ids", svc.vector_status(self.claude))
        self.assertEqual(svc.vector_status(self.claude)["missing"], 1)

    def test_a_vector_made_from_older_text_is_stale(self):
        svc = self.service(Index())
        ids = self.fill(svc)
        g = svc._g()
        g.update_body(ids[0], "the text changed after it was embedded", "test")
        g.commit()
        g.db.close()
        v = svc.vector_status(self.admin)
        self.assertEqual((v["stale"], v["missing"], v["stale_ids"]), (1, 0, [ids[0]]))
        self.assertTrue(any("out of date" in w for w in v["warnings"]))

    def test_vectors_from_two_embedders_are_a_warning(self):
        svc = self.service(Index())
        ids = self.fill(svc)
        g = svc._g()
        g.db.execute("UPDATE vectors SET backend='ollama' WHERE node_id=?", (ids[2],))
        g.commit()
        g.db.close()
        v = svc.vector_status(self.admin)
        self.assertEqual(v["embedders"], {"fake": 2, "ollama": 1})
        self.assertTrue(any("more than one embedder" in w and "ollama" in w for w in v["warnings"]), v["warnings"])

    def test_only_active_notes_count(self):
        svc = self.service(Index())
        ids = self.fill(svc)
        svc.reject(self.admin, ids[0])
        g = svc._g()
        g.db.execute("DELETE FROM vectors WHERE node_id=?", (ids[0],))                    # a rejected note having no vector is fine
        g.commit()
        g.db.close()
        v = svc.vector_status(self.admin)
        self.assertEqual((v["active"], v["missing"]), (2, 0))

    def test_missing_ids_are_capped(self):
        svc = self.service(Index())
        self.fill(svc, 25)
        g = svc._g()
        g.db.execute("DELETE FROM vectors")
        g.commit()
        g.db.close()
        v = svc.vector_status(self.admin)
        self.assertEqual(v["missing"], 25)
        self.assertEqual(len(v["missing_ids"]), 20)

    def test_stats_carries_the_vector_status_for_the_owner_only(self):
        svc = self.service(Index())
        self.fill(svc)
        s = svc.stats(self.admin)
        self.assertEqual((s["vectors"]["state"], s["vectors"]["active"]), ("ok", 3))
        for who in (self.claude, self.qwen):
            self.assertNotIn("vectors", svc.stats(who))                                  # how many notes lack a vector is a fact about the whole store

    def test_vector_gaps_works_on_a_bare_graph_for_doctor(self):
        svc = self.service(Index())
        ids = self.fill(svc)
        g = svc._g()
        g.db.execute("DELETE FROM vectors WHERE node_id=?", (ids[0],))
        g.commit()
        gaps = vector_gaps(g)
        g.db.close()
        self.assertEqual(gaps["active"], 3)
        self.assertEqual([m["id"] for m in gaps["missing"]], [ids[0]])
        self.assertEqual(gaps["missing"][0]["title"], "Note 0")
        self.assertEqual(gaps["embedders"], {"fake": 2})


if __name__ == "__main__":
    unittest.main(verbosity=1)
