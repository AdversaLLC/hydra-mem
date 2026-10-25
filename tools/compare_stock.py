#!/usr/bin/env python3
"""Regression check: the service's recall versus stock hydra-graph hybrid recall on the same gold questions (paired).

  compare_stock.py [--floor 0.25] [--coverage 0.34] [--scratch DIR]
The service runs on a scratch COPY of the graph and vector store; stock recall runs read-only against the live ones.
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SK = Path.home() / ".claude" / "skills" / "gsd-hydra-learn" / "scripts"
sys.path.insert(0, str(SK))
from eval_recall import bootstrap_ci, paired, recall as stock_recall, score_row, sign_test_p  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402
from memsvc.vector import ChromaDaemonIndex  # noqa: E402


def load(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=float, default=0.25)
    ap.add_argument("--coverage", type=float, default=0.34)
    a = ap.parse_args()
    cfg = json.loads((Path.home() / ".hydra" / "config.json").read_text())
    tmp = Path(tempfile.mkdtemp(prefix="cmp-", dir="/tmp"))
    shutil.copy2(Path(cfg["graph_db"].replace("~", str(Path.home()))), tmp / "graph.db")
    shutil.copy2(Path.home() / ".hydra" / "graph.key", tmp / "graph.key")
    shutil.copytree(cfg["rag"]["chroma_path"], tmp / "chroma")
    reg = Registry(tmp / "agents.json", tmp / "admin")
    agent = reg.authenticate(reg.create_agent("evalbot", "trusted"))
    pos = [g for g in load(SK / "gold.sample.jsonl") + load(SK / "gold.hard.jsonl") if g["relevant"]]
    idx = ChromaDaemonIndex(cfg["rag"]["python"], str(tmp / "chroma"), timeout=180)
    svc = MemoryService(tmp / "graph.db", reg, index=idx, floor=a.floor, fts_coverage=a.coverage)
    s_rows, k_rows = [], []
    try:
        for g in pos:
            s_rows.append(score_row([i["id"] for i in svc.recall(agent, g["q"], project="*", limit=10)["items"]], g["relevant"], 5))
            ids, _tier, _ms = stock_recall(g["q"], "hybrid", 10, "*")
            k_rows.append(score_row(ids, g["relevant"], 5))
    finally:
        idx.close()
    n = len(pos)
    print(f"{n} positive questions; service floor {a.floor} coverage {a.coverage} vs stock hybrid recall")
    for name, rows in (("stock", k_rows), ("service", s_rows)):
        h1, rr = [r["h1"] for r in rows], [r["rr"] for r in rows]
        ci1, cir = bootstrap_ci(h1), bootstrap_ci(rr)
        print(f"{name:8} hit@1 {sum(h1) / n:.2f} [{ci1[0]:.2f}-{ci1[1]:.2f}]  hit@5 {sum(r['hk'] for r in rows) / n:.2f}  MRR {sum(rr) / n:.2f} [{cir[0]:.2f}-{cir[1]:.2f}]")
    for label, key in (("hit@1", "h1"), ("hit@5", "hk")):
        s_only, k_only = paired([r[key] for r in s_rows], [r[key] for r in k_rows])
        print(f"paired {label}: service only {s_only}, stock only {k_only}, exact sign test p = {sign_test_p(s_only, k_only):.3f}")


if __name__ == "__main__":
    main()
