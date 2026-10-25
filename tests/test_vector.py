"""Tests for memsvc.vector (persistent embedding daemon). Needs the RAG virtualenv and its cached model; skipped otherwise.
Run: python3 -I tests/test_vector.py
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memsvc.vector import ChromaDaemonIndex, VectorError  # noqa: E402

VENV = Path.home() / ".hydra" / "venv-rag" / "bin" / "python"
MODEL = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2" / "onnx"


@unittest.skipUnless(VENV.exists() and MODEL.exists(), "needs the RAG virtualenv and its cached ONNX model")
class Daemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.idx = ChromaDaemonIndex(python=str(VENV), chroma_path=str(Path(cls.tmp.name) / "chroma"), timeout=120)
        cls.idx.upsert([
            {"id": "a", "text": "The router blocks unknown programs until the owner clicks allow", "project": "p1", "meta": {"kind": "note"}},
            {"id": "b", "text": "Bake sourdough bread with a long cold fermentation overnight", "project": "p2", "meta": {"kind": "note"}},
            {"id": "c", "text": "Rotate the encryption key every ninety days and store it offline", "project": None, "meta": {"kind": "note"}},
        ])

    @classmethod
    def tearDownClass(cls):
        cls.idx.close()
        cls.tmp.cleanup()

    def test_paraphrase_finds_the_right_note_first(self):
        hits = self.idx.query("how often should I change my secret key", None, 3)
        self.assertEqual(hits[0][0], "c")
        self.assertTrue(0.0 <= hits[0][1] <= 1.0)

    def test_scores_are_ordered_best_first(self):
        hits = self.idx.query("baking bread", None, 3)
        self.assertEqual(hits[0][0], "b")
        self.assertEqual([s for _, s in hits], sorted([s for _, s in hits], reverse=True))

    def test_delete_removes_from_results(self):
        self.idx.upsert([{"id": "tmp1", "text": "temporary zebra crossing note about stripes", "project": "p1", "meta": {"kind": "note"}}])
        self.assertIn("tmp1", [i for i, _ in self.idx.query("zebra stripes", None, 5)])
        self.idx.delete(["tmp1"])
        self.assertNotIn("tmp1", [i for i, _ in self.idx.query("zebra stripes", None, 5)])

    def test_upsert_is_idempotent(self):
        doc = {"id": "idem", "text": "idempotent upsert check about walnuts", "project": "p1", "meta": {"kind": "note"}}
        self.idx.upsert([doc])
        self.idx.upsert([doc])
        self.assertEqual([i for i, _ in self.idx.query("walnuts upsert", None, 10)].count("idem"), 1)

    def test_bad_request_does_not_kill_the_daemon(self):
        with self.assertRaises(VectorError):
            self.idx._call({"op": "nonsense"})
        self.assertTrue(self.idx.query("router allow", None, 1))

    def test_restarts_after_the_process_dies(self):
        os.kill(self.idx._proc.pid, signal.SIGKILL)
        time.sleep(0.3)
        self.assertEqual(self.idx.query("how often should I change my secret key", None, 1)[0][0], "c")

    def test_geometry_reports_near_duplicates_and_outliers(self):
        self.idx.upsert([
            {"id": "g1", "text": "Back up the database nightly and keep seven copies offline", "project": "p1", "meta": {"kind": "note"}},
            {"id": "g2", "text": "Back up the database nightly and keep seven copies offline please", "project": "p1", "meta": {"kind": "note"}},
        ])
        rep = self.idx.geometry(dup=0.9, top=5)
        self.assertGreaterEqual(rep["n"], 5)
        self.assertEqual(rep["d"], 384)
        self.assertTrue(any({"g1", "g2"} <= set(g) for g in rep["duplicate_groups"]))
        self.assertLessEqual(len(rep["outliers"]), 5)
        self.assertTrue(0 < rep["coherence"] <= 1)

    def test_repeated_queries_hit_the_embedding_cache_and_are_fast(self):
        before = self.idx._call({"op": "info"})["query_cache_hits"]
        t = time.time()
        a = self.idx.query("unique cache probe about pomegranates in winter", None, 3)
        first = time.time() - t
        t = time.time()
        b = self.idx.query("unique cache probe about pomegranates in winter", None, 3)
        second = time.time() - t
        self.assertEqual(self.idx._call({"op": "info"})["query_cache_hits"], before + 1)
        self.assertEqual(a, b)
        self.assertLess(second, first)
        self.assertLess(second, 0.1)

    def test_telemetry_is_off_and_second_query_is_fast(self):
        self.assertFalse(self.idx._call({"op": "info"})["telemetry"])
        self.idx.query("warm up", None, 1)
        t = time.time()
        self.idx.query("router blocks programs", None, 3)
        self.assertLess(time.time() - t, 1.0)


DOCS = [{"id": f"d{i}", "text": t, "project": "p", "meta": {"kind": "note"}} for i, t in enumerate((
    "The router blocks unknown programs until the owner clicks allow",
    "Bake sourdough bread with a long cold fermentation overnight",
    "Rotate the encryption key every ninety days and store it offline"))]


@unittest.skipUnless(VENV.exists() and MODEL.exists(), "needs the RAG virtualenv and its cached ONNX model")
class Fingerprinted(unittest.TestCase):
    """Item 1.7: the daemon pins what its embedder makes of a canary text, and turns itself off, with a reason, when that or the stored vectors change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chroma = Path(self.tmp.name) / "chroma"
        self.pin = self.chroma / "memsvc-embedder.json"
        self.opened = []

    def tearDown(self):
        for i in self.opened:
            i.close()
        self.tmp.cleanup()

    def index(self):
        i = ChromaDaemonIndex(python=str(VENV), chroma_path=str(self.chroma), timeout=120)
        self.opened.append(i)
        return i

    def seeded_then_closed(self):
        a = self.index()
        a.upsert(DOCS)
        a.close()

    def test_first_start_pins_and_a_restart_matches(self):
        a = self.index()
        a.upsert(DOCS)
        self.assertEqual(a.health()["fingerprint"]["state"], "pinned")
        self.assertTrue(self.pin.exists())
        a.close()
        b = self.index()
        b.query("router", None, 1)
        self.assertEqual(b.health()["fingerprint"]["state"], "match")
        self.assertGreater(b.health()["fingerprint"]["cosine"], 0.999)

    def test_asking_for_health_does_not_start_the_daemon(self):
        a = self.index()
        h = a.health()
        self.assertEqual((h["running"], h["fingerprint"], h["backend"]), (False, None, "onnx"))
        self.assertIsNone(a._proc)

    def test_a_changed_embedder_turns_the_vector_side_off_with_a_reason(self):
        self.seeded_then_closed()
        rec = json.loads(self.pin.read_text())
        rec["vector"] = list(reversed(rec["vector"]))                       # as if another model had been pinned
        self.pin.write_text(json.dumps(rec))
        b = self.index()
        with self.assertRaises(VectorError) as e:
            b.query("router", None, 1)
        self.assertIn("embedder changed", str(e.exception))
        with self.assertRaises(VectorError):
            b.upsert([{"id": "late", "text": "a note written after the embedder changed", "project": "p", "meta": {"kind": "note"}}])
        with self.assertRaises(VectorError):
            b.geometry()
        self.assertEqual(b.health()["fingerprint"]["state"], "mismatch")
        self.assertEqual(b._call({"op": "count"})["count"], len(DOCS))      # reading the count is still allowed
        self.assertEqual(json.loads(self.pin.read_text()), rec)             # and nothing re-pinned itself

    def test_a_store_written_by_something_else_is_refused_and_not_pinned(self):
        self.seeded_then_closed()
        self.pin.unlink()
        snippet = ("import chromadb, random, sys\n"
                   "col = chromadb.PersistentClient(path=sys.argv[1]).get_collection('hg_global')\n"
                   "got = col.get(include=['embeddings'])\nrandom.seed(3)\n"
                   "col.update(ids=got['ids'], embeddings=[[random.uniform(-1, 1) for _ in range(len(got['embeddings'][0]))] for _ in got['ids']])\n")
        subprocess.run([str(VENV), "-I", "-c", snippet, str(self.chroma)], check=True, timeout=180,
                       env={"HOME": os.environ["HOME"], "PATH": os.environ["PATH"], "ANONYMIZED_TELEMETRY": "False"})
        b = self.index()
        with self.assertRaises(VectorError) as e:
            b.query("router", None, 1)
        self.assertIn("disagree", str(e.exception))
        self.assertEqual(b.health()["fingerprint"]["state"], "mismatch")
        self.assertFalse(self.pin.exists())

    def test_an_unreadable_pin_fails_closed_and_is_left_alone(self):
        self.seeded_then_closed()
        self.pin.write_text("garbage")
        b = self.index()
        with self.assertRaises(VectorError) as e:
            b.query("router", None, 1)
        self.assertIn("cannot read", str(e.exception))
        self.assertEqual(b.health()["fingerprint"]["state"], "unreadable")
        self.assertEqual(self.pin.read_text(), "garbage")

    def test_an_empty_store_pins_on_first_use(self):
        a = self.index()
        self.assertEqual(a.query("anything", None, 1), [])
        self.assertEqual(a.health()["fingerprint"]["state"], "pinned")


if __name__ == "__main__":
    unittest.main(verbosity=1)
