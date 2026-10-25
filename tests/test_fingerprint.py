"""Tests for item 1.7, the pure part: the embedder fingerprint. Vectors from two different embedders cannot be compared, and nothing fails loudly when
they are mixed; recall just gets worse. So the vector side pins what its embedder makes of a fixed canary text, and refuses to run when that changes.
Run: python3 -I tests/test_fingerprint.py
"""
import json
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memsvc import fingerprint as fp  # noqa: E402

rng = random.Random(7)
VEC = [rng.uniform(-1, 1) for _ in range(384)]
NOW = "2026-10-22T12:00:00Z"


def noisy(v, eps):
    return [x + rng.uniform(-eps, eps) for x in v]


class Cosine(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(fp.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(fp.cosine([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(fp.cosine([1, 1], [-1, -1]), -1.0)

    def test_a_zero_vector_is_not_similar_to_anything(self):
        self.assertEqual(fp.cosine([0, 0], [1, 1]), 0.0)

    def test_different_lengths_are_not_comparable(self):
        self.assertEqual(fp.cosine([1, 2], [1, 2, 3]), 0.0)


class Compare(unittest.TestCase):
    def setUp(self):
        self.rec = fp.record(VEC, "onnx", NOW)

    def test_the_record_carries_what_a_later_run_needs(self):
        self.assertEqual((self.rec["backend"], self.rec["dim"], self.rec["created"]), ("onnx", 384, NOW))
        self.assertEqual(len(self.rec["vector"]), 384)
        self.assertEqual(self.rec["canary_sha"], fp.canary_sha())

    def test_the_same_embedder_matches_even_with_rounding_noise(self):
        r = fp.compare(self.rec, noisy(VEC, 1e-5), "onnx")
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["cosine"], fp.COSINE_MIN)

    def test_a_different_model_of_the_same_size_does_not(self):
        other = [rng.uniform(-1, 1) for _ in range(384)]
        r = fp.compare(self.rec, other, "onnx")
        self.assertFalse(r["ok"])
        self.assertLess(r["cosine"], 0.5)
        self.assertIn("embedder changed", r["reason"])

    def test_a_different_size_is_named_as_such(self):
        r = fp.compare(self.rec, VEC[:100], "onnx")
        self.assertFalse(r["ok"])
        self.assertIn("384", r["reason"])
        self.assertIn("100", r["reason"])

    def test_a_different_backend_label_does_not_match(self):
        r = fp.compare(self.rec, VEC, "ollama")
        self.assertFalse(r["ok"])
        self.assertIn("onnx", r["reason"])
        self.assertIn("ollama", r["reason"])

    def test_a_record_made_from_another_canary_cannot_vouch(self):
        r = fp.compare(dict(self.rec, canary_sha="0" * 16), VEC, "onnx")
        self.assertFalse(r["ok"])
        self.assertIn("canary", r["reason"])


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memsvc-embedder.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_pinned_yet(self):
        self.assertEqual(fp.inspect(self.path, VEC, "onnx")["state"], "unpinned")

    def test_save_then_match(self):
        fp.save(self.path, fp.record(VEC, "onnx", NOW))
        r = fp.inspect(self.path, noisy(VEC, 1e-5), "onnx")
        self.assertEqual(r["state"], "match")
        self.assertEqual(json.loads(self.path.read_text())["dim"], 384)

    def test_a_changed_embedder_is_a_mismatch_with_a_reason(self):
        fp.save(self.path, fp.record(VEC, "onnx", NOW))
        r = fp.inspect(self.path, [rng.uniform(-1, 1) for _ in range(384)], "onnx")
        self.assertEqual(r["state"], "mismatch")
        self.assertTrue(r["reason"])

    def test_an_unreadable_file_fails_closed_and_is_never_overwritten(self):
        for bad in ("not json at all", "[]", json.dumps({"dim": 3}), json.dumps({"vector": "x", "dim": 3, "backend": "onnx", "canary_sha": fp.canary_sha()}),
                    json.dumps({"vector": [0.1, 0.2, 0.3], "dim": 5, "backend": "onnx", "canary_sha": fp.canary_sha()})):
            self.path.write_text(bad)
            r = fp.inspect(self.path, VEC, "onnx")
            self.assertEqual(r["state"], "unreadable", bad)
            self.assertTrue(r["reason"])
            self.assertEqual(self.path.read_text(), bad)                         # the owner decides; the service never re-pins on its own

    def test_save_does_not_leave_a_temporary_file_or_replace_atomically(self):
        fp.save(self.path, fp.record(VEC, "onnx", NOW))
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["memsvc-embedder.json"])


class SpotCheck(unittest.TestCase):
    def test_no_stored_vectors_means_nothing_to_disagree_with(self):
        self.assertTrue(fp.spot_check([])["ok"])

    def test_vectors_that_re_embed_the_same_agree(self):
        pairs = [(VEC, noisy(VEC, 1e-5)) for _ in range(3)]
        self.assertTrue(fp.spot_check(pairs)["ok"])

    def test_one_disagreeing_note_is_enough_to_refuse(self):
        other = [rng.uniform(-1, 1) for _ in range(384)]
        r = fp.spot_check([(VEC, VEC), (VEC, other), (VEC, VEC)])
        self.assertFalse(r["ok"])
        self.assertIn("1 of 3", r["reason"])
        self.assertIn("different model", r["reason"])

    def test_stored_vectors_of_another_size_are_named(self):
        r = fp.spot_check([(VEC[:100], VEC)])
        self.assertFalse(r["ok"])
        self.assertIn("100", r["reason"])
        self.assertIn("384", r["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
