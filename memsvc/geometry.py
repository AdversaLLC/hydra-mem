"""Embedding geometry for memory compaction and outlier review. Needs numpy (it runs inside the vector daemon's virtualenv).

A is the n x d matrix of note embeddings, one row per note.
  leverage        squared length of each row of an orthonormal basis of the column space. In [0,1], sums to the rank.
                  A note alone in a direction scores 1; m copies of a note share one direction and score 1/m each.
  ridge leverage  the same idea with shrinkage: l_i = a_i (A^T A + lam I)^-1 a_i^T. Stays informative when there are fewer
                  notes than dimensions (n < d), where exact leverage is 1 for every note and says nothing.
  near-duplicates groups of notes whose cosine similarity is at least a threshold (chains are merged).
Leverage is geometry, not importance to the user: use it to REVIEW (redundant vs unique, outliers), never to delete on its own.
Scores shift as notes are added, so recompute rather than cache. The paper's fast sketches are not needed at our sizes.
"""
import numpy as np


def leverage(A: np.ndarray) -> np.ndarray:
    """Exact statistical leverage scores (thin SVD)."""
    U, S, _ = np.linalg.svd(np.asarray(A, dtype=float), full_matrices=False)
    if S.size == 0:
        return np.zeros(len(A))
    r = int((S > S[0] * max(A.shape) * np.finfo(float).eps).sum())
    return (U[:, :r] ** 2).sum(axis=1)


def ridge_leverage(A: np.ndarray, lam: float) -> np.ndarray:
    """Ridge leverage via the SVD: sum_j U_ij^2 * s_j^2 / (s_j^2 + lam). Works for any shape."""
    U, S, _ = np.linalg.svd(np.asarray(A, dtype=float), full_matrices=False)
    return ((U ** 2) * (S ** 2 / (S ** 2 + lam))).sum(axis=1)


def _unit_rows(A: np.ndarray) -> np.ndarray:
    A = np.asarray(A, dtype=float)
    n = np.linalg.norm(A, axis=1, keepdims=True)
    return A / np.where(n == 0, 1.0, n)


def near_duplicate_groups(A: np.ndarray, threshold: float, block: int = 1024) -> list:
    """Groups (lists of row indexes, size >= 2) whose cosine similarity chains above `threshold`."""
    X = _unit_rows(A)
    n = len(X)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for lo in range(0, n, block):
        sims = X[lo:lo + block] @ X.T
        rows, cols = np.nonzero(sims >= threshold)
        for r, c in zip(rows, cols):
            i, j = lo + int(r), int(c)
            if i < j:
                parent[find(j)] = find(i)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted([g for g in groups.values() if len(g) > 1], key=lambda g: g[0])


def nearest_cosine(A: np.ndarray, block: int = 1024) -> np.ndarray:
    """For each note, the highest cosine similarity to any OTHER note."""
    X = _unit_rows(A)
    out = np.full(len(X), -1.0)
    for lo in range(0, len(X), block):
        sims = X[lo:lo + block] @ X.T
        for k in range(sims.shape[0]):
            sims[k, lo + k] = -1.0
        out[lo:lo + block] = sims.max(axis=1) if len(X) > 1 else -1.0
    return out


def report(ids: list, A: np.ndarray, lam_rel: float = 0.05, dup_threshold: float = 0.95, top: int = 10) -> dict:
    X = _unit_rows(A)
    n, d = X.shape
    lam = lam_rel * (n / max(d, 1))                       # relative to the mean squared singular value of unit rows
    lev = ridge_leverage(X, lam)
    near = nearest_cosine(X)
    order = np.argsort(-lev)[:top]
    mean = float(lev.mean()) if n else 0.0
    return {"n": int(n), "d": int(d), "ridge_lambda": float(lam), "coherence": float(lev.max()) if n else 0.0, "mean_leverage": mean,
            "outliers": [{"id": ids[i], "leverage": float(lev[i]), "relative": float(lev[i] / mean) if mean else 0.0, "nearest_cos": float(near[i])} for i in order],
            "duplicate_groups": [[ids[i] for i in g] for g in near_duplicate_groups(X, dup_threshold)]}
