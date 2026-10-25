"""Time every read operation of the service on synthetic stores of growing size, so "fast enough" is a measurement.

Builds a throwaway database under the system temp folder (nothing real is touched), adds notes in batches, and after each size reports the
median and worst of several runs for: ranked search, newest-first listing, filtered search, timeline, fetch, session context, sessions list
and the older recall. Keyword retrieval only: the vector tier's cost is separate and depends on the embedder.

    python3 -I tools/bench_read_path.py [--sizes 1000 5000 20000] [--reps 7]
"""
import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memsvc.registry import Registry  # noqa: E402
from memsvc.service import ALLOWED_KINDS, MemoryService  # noqa: E402


def timed(fn, reps):
    runs = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - t0) * 1000)
    return statistics.median(runs), max(runs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000, 20000])
    ap.add_argument("--reps", type=int, default=7)
    a = ap.parse_args()
    d = Path(tempfile.mkdtemp(prefix="memsvc-bench-"))
    reg = Registry(d / "agents.json", d / "admin.token")
    reg.ensure_admin()
    agent = reg.authenticate(reg.create_agent("bench", "trusted"))
    svc = MemoryService(d / "graph.db", reg)
    rnd = random.Random(7)
    vocab = [f"w{i}" for i in range(3000)]
    weights = [1 / (i + 1) for i in range(3000)]                  # a few common words, a long tail, like real text
    ids, have = [], 0
    print(f"store under {d}")
    print(f"{'notes':>7} | " + " | ".join(f"{n:>13}" for n in ("search", "list newest", "search+kind", "timeline", "fetch x5", "context", "sessions", "recall")) + "   (median / worst ms)")
    for size in sorted(a.sizes):
        t_add = time.perf_counter()
        while have < size:
            body = " ".join(rnd.choices(vocab, weights, k=28))
            kind = rnd.choice(ALLOWED_KINDS)
            r = svc.add(agent, kind, f"Note {have} {rnd.choice(vocab)}", body, project=f"p{rnd.randrange(3)}", session=f"s{rnd.randrange(40)}" if rnd.random() < .5 else None)
            if r["outcome"] == "add":
                ids.append(r["id"])
                have += 1
        added_s = time.perf_counter() - t_add
        q = f"{rnd.choice(vocab[:60])} {rnd.choice(vocab[:200])}"
        anchor = ids[len(ids) // 2]
        five = rnd.sample(ids, 5)
        fresh = iter(f"{rnd.choice(vocab[:60])} {rnd.choice(vocab[:200])} {rnd.choice(vocab[:400])}" for _ in range(10_000))   # a new query each time: the repeat-query cache must not flatter recall
        row = [timed(lambda: svc.search(agent, q), a.reps), timed(lambda: svc.search(agent, ""), a.reps),
               timed(lambda: svc.search(agent, q, kind="decision"), a.reps), timed(lambda: svc.timeline(agent, anchor=anchor), a.reps),
               timed(lambda: svc.get_observations(agent, five), a.reps), timed(lambda: svc.context(agent, project="p1"), a.reps),
               timed(lambda: svc.sessions(agent), a.reps), timed(lambda: svc.recall(agent, next(fresh)), a.reps)]
        print(f"{size:>7} | " + " | ".join(f"{m:6.1f}/{w:6.1f}" for m, w in row) + f"   (adding took {added_s:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
