"""A fingerprint of the embedder, so a model change cannot go unnoticed. Standard library only (the vector daemon imports it too).

Vectors made by two different embedders cannot be compared, and nothing fails loudly when they are mixed: recall quietly gets worse. So the
vector side embeds a fixed canary text at start and compares it with the copy pinned the first time. It also re-embeds a few stored notes and
compares them with their stored vectors, which catches a store that something else wrote (the canary alone only sees this process's embedder).
Any disagreement turns the vector side off with a reason; recall falls back to keyword results and says so. Nothing here re-pins by itself:
only the owner, after re-embedding the store, deletes the pinned file.
"""
import hashlib
import json
import math
import os
from pathlib import Path

CANARY = "The quick brown fox jumps over the lazy dog while the owner counts 12 blue umbrellas in the rain."
COSINE_MIN = 0.999            # the same model on the same text agrees to rounding; another model or pooling lands far below this
SAMPLE_COSINE_MIN = 0.98      # a stored vector against the same note embedded again now
SAMPLE = 3                    # stored notes re-embedded at start
FILE_NAME = "memsvc-embedder.json"
ROUND = 5


def canary_sha() -> str:
    return hashlib.sha256(CANARY.encode("utf-8")).hexdigest()[:16]


def cosine(a, b) -> float:
    if len(a) != len(b):
        return 0.0
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def record(vec, backend: str, created: str) -> dict:
    return {"version": 1, "backend": backend, "dim": len(vec), "canary_sha": canary_sha(), "created": created,
            "vector": [round(float(x), ROUND) for x in vec]}


def compare(rec: dict, vec, backend: str) -> dict:
    """Does this embedder still make what the pinned one made? Returns {"ok", "cosine", "reason"}."""
    if rec.get("canary_sha") != canary_sha():
        return {"ok": False, "cosine": None, "reason": "the pinned fingerprint was made from a different canary text, so it cannot vouch for this embedder"}
    if rec.get("backend") != backend:
        return {"ok": False, "cosine": None, "reason": f"the embedder changed: the store was pinned to backend '{rec.get('backend')}', this one is '{backend}'"}
    if rec.get("dim") != len(vec):
        return {"ok": False, "cosine": None, "reason": f"the embedder changed: it now makes {len(vec)}-dimension vectors, the pinned one made {rec.get('dim')}"}
    c = cosine(rec["vector"], vec)
    if c < COSINE_MIN:
        return {"ok": False, "cosine": round(c, 4), "reason": f"the embedder changed: the canary text now embeds at cosine {c:.3f} to the pinned copy (needs {COSINE_MIN})"}
    return {"ok": True, "cosine": round(c, 6), "reason": None}


def load(path):
    """The pinned record, or None when there is no file. Raises ValueError when the file is there but cannot be trusted."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {p.name} ({type(e).__name__})")
    if (not isinstance(rec, dict) or not isinstance(rec.get("vector"), list) or not rec["vector"] or not all(isinstance(x, (int, float)) for x in rec["vector"])
            or rec.get("dim") != len(rec["vector"]) or not isinstance(rec.get("backend"), str) or not isinstance(rec.get("canary_sha"), str)):
        raise ValueError(f"{p.name} is not a valid fingerprint")
    return rec


def save(path, rec: dict) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, p)


def inspect(path, vec, backend: str) -> dict:
    """Compare against the pinned file without writing anything. state is match, mismatch, unpinned or unreadable."""
    try:
        rec = load(path)
    except ValueError as e:
        return {"state": "unreadable", "cosine": None,
                "reason": f"{e}. The owner decides: once the store is known to be good, delete the file to pin again"}
    if rec is None:
        return {"state": "unpinned", "cosine": None, "reason": None}
    r = compare(rec, vec, backend)
    return {"state": "match" if r["ok"] else "mismatch", "cosine": r["cosine"], "reason": r["reason"]}


def spot_check(pairs) -> dict:
    """pairs: (stored vector, the same note embedded now). One disagreement is enough to distrust the store."""
    bad, worst, why = 0, 1.0, None
    for stored, fresh in pairs:
        if len(stored) != len(fresh):
            return {"ok": False, "reason": f"the store holds {len(stored)}-dimension vectors and this embedder makes {len(fresh)}-dimension ones: a different model wrote them"}
        c = cosine(stored, fresh)
        if c < SAMPLE_COSINE_MIN:
            bad += 1
            worst = min(worst, c)
    if bad:
        return {"ok": False, "reason": f"stored vectors disagree with this embedder: {bad} of {len(pairs)} sampled notes re-embed at cosine {worst:.2f} "
                                       f"(needs {SAMPLE_COSINE_MIN}); a different model wrote them"}
    return {"ok": True, "reason": None}
