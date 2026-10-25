"""Tests for memsvc.geometry: leverage, ridge leverage, near-duplicate groups. Reference values come from the matrix-coherence
digest (verified there in numpy). Needs numpy, so run with the RAG virtualenv:
  ~/.hydra/venv-rag/bin/python -I tests/test_geometry.py        (skipped under a python without numpy)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import numpy as np
except ImportError:
    np = None
if np is not None:
    from memsvc import geometry as G          # numpy present: a missing module must FAIL, not skip

TOL = 1e-9


@unittest.skipIf(np is None, "numpy not available")
class Leverage(unittest.TestCase):
    def lev(self, rows):
        return G.leverage(np.array(rows, dtype=float))

    def test_t1_duplicate_and_orthogonal_rows(self):
        l = self.lev([[1, 0], [1, 0], [0, 1]])
        np.testing.assert_allclose(l, [0.5, 0.5, 1.0], atol=TOL)
        self.assertAlmostEqual(l.max(), 1.0, places=9)                       # coherence

    def test_t2_duplicate_pairs_plus_a_lone_direction(self):
        e = np.eye(3)
        l = G.leverage(np.array([e[0], e[0], e[1], e[1], e[2]]))
        np.testing.assert_allclose(l, [0.5, 0.5, 0.5, 0.5, 1.0], atol=TOL)
        self.assertAlmostEqual(l.sum(), 3.0, places=9)

    def test_t3_row_scale_matters_so_normalise_rows(self):
        np.testing.assert_allclose(self.lev([[2, 0, 0], [1, 0, 0], [0, 1, 0], [0, 1, 0], [0, 0, 1]]), [0.8, 0.2, 0.5, 0.5, 1.0], atol=TOL)

    def test_t4_m_copies_share_one_direction(self):
        m = 6
        l = self.lev([[1, 0, 0], [0, 1, 0]] + [[0, 0, 1]] * m)
        np.testing.assert_allclose(l[2:], [1 / m] * m, atol=TOL)
        self.assertAlmostEqual(l[2:].sum(), 1.0, places=9)

    def test_t5_row_owning_a_column_has_leverage_one(self):
        rng = np.random.default_rng(0)
        A = rng.standard_normal((2000, 20))
        A[1:, 0] = 0.0
        l = G.leverage(A)
        self.assertAlmostEqual(l[0], 1.0, places=9)
        self.assertAlmostEqual(l.sum(), 20.0, places=7)

    def test_t6_sum_mean_range_and_invariances(self):
        rng = np.random.default_rng(1)
        A = rng.standard_normal((2000, 20))
        l = G.leverage(A)
        self.assertAlmostEqual(l.sum(), 20.0, places=7)
        self.assertAlmostEqual(l.mean(), 0.01, places=9)
        self.assertTrue(((l > 0) & (l < 1)).all())
        M = rng.standard_normal((20, 20))
        np.testing.assert_allclose(G.leverage(A @ M), l, atol=1e-8)          # invariant to an invertible mixing of columns
        perm = rng.permutation(2000)
        np.testing.assert_allclose(G.leverage(A[perm]), l[perm], atol=1e-9)

    def test_t7_appending_a_row_never_raises_old_scores(self):
        rng = np.random.default_rng(2)
        A = rng.standard_normal((200, 8))
        before = G.leverage(A)
        after = G.leverage(np.vstack([A, rng.standard_normal((1, 8))]))
        self.assertTrue((after[:-1] <= before + 1e-12).all())
        self.assertAlmostEqual(after.sum(), 8.0, places=7)

    def test_t8_new_note_leverage_is_h_over_one_plus_h(self):
        rng = np.random.default_rng(3)
        A = rng.standard_normal((300, 6))
        x = rng.standard_normal(6)
        h = float(x @ np.linalg.inv(A.T @ A) @ x)
        new = G.leverage(np.vstack([A, x]))[-1]
        self.assertAlmostEqual(new, h / (1 + h), places=9)

    def test_t9_exact_leverage_is_degenerate_when_n_is_below_d_and_ridge_is_not(self):
        rng = np.random.default_rng(4)
        A = rng.standard_normal((300, 384))
        l = G.leverage(A)
        np.testing.assert_allclose(l, np.ones(300), atol=1e-8)
        lam = 0.5
        r = G.ridge_leverage(np.array([[1, 0], [1, 0], [0, 1]], dtype=float), lam)
        np.testing.assert_allclose(r, [1 / (2 + lam), 1 / (2 + lam), 1 / (1 + lam)], atol=TOL)
        rr = G.ridge_leverage(A, 1.0)
        self.assertTrue(rr.std() > 0 and (rr < 1).all())                    # informative where exact leverage is not


@unittest.skipIf(np is None, "numpy not available")
class Groups(unittest.TestCase):
    def test_near_duplicates_group_together_and_others_stay_single(self):
        a = np.array([1.0, 0.0, 0.0, 0.0])
        rows = np.array([a, a + 0.01 * np.array([0, 1, 0, 0]), [0, 1, 0, 0], [0, 0, 1, 0], [0.0, 0.0, 0.0, 1.0]])
        groups = G.near_duplicate_groups(rows, 0.95)
        self.assertEqual(groups, [[0, 1]])

    def test_chain_of_near_duplicates_forms_one_group(self):
        base = np.array([1.0, 0.0])
        rows = np.array([[np.cos(t), np.sin(t)] for t in (0.0, 0.1, 0.2, 0.3)] + [[0.0, 1.0]])
        self.assertEqual(G.near_duplicate_groups(rows, 0.98), [[0, 1, 2, 3]])

    def test_report_shape_and_ordering(self):
        rng = np.random.default_rng(5)
        A = rng.standard_normal((40, 16))
        A[1] = A[0] + 1e-3 * rng.standard_normal(16)                        # a planted near-duplicate
        ids = [f"n{i}" for i in range(40)]
        rep = G.report(ids, A, dup_threshold=0.99)
        self.assertEqual(rep["n"], 40)
        self.assertEqual(rep["d"], 16)
        self.assertIn(["n0", "n1"], rep["duplicate_groups"])
        scores = [x["leverage"] for x in rep["outliers"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertLessEqual(len(rep["outliers"]), 10)
        self.assertTrue(0.0 <= rep["coherence"] <= 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
