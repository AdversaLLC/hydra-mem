#!/usr/bin/env python3
"""Run the floor/coverage sweep on a COPY of the real graph and vector store. The live store is never opened for writing.

  run_sweep.py [--floors 0,0.25,0.3,0.35,0.4,0.45,0.5] [--coverages 0,0.34,0.5,0.67] [--k 5]
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
SK = Path.home() / ".claude" / "skills" / "gsd-hydra-learn" / "scripts"
sys.path.insert(0, str(SK))
import evalkit  # noqa: E402
from eval_recall import corpus_active, file_sha16  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402
from memsvc.vector import ChromaDaemonIndex  # noqa: E402


def load(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floors", default="0,0.25,0.3,0.35,0.4,0.45,0.5")
    ap.add_argument("--coverages", default="0,0.34,0.5,0.67")
    ap.add_argument("--k", type=int, default=5)
    a = ap.parse_args()
    cfg = json.loads((Path.home() / ".hydra" / "config.json").read_text())
    tmp = Path(tempfile.mkdtemp(prefix="sweep-", dir="/tmp"))
    shutil.copy2(Path(cfg["graph_db"].replace("~", str(Path.home()))), tmp / "graph.db")
    shutil.copy2(Path.home() / ".hydra" / "graph.key", tmp / "graph.key")
    shutil.copytree(cfg["rag"]["chroma_path"], tmp / "chroma")
    reg = Registry(tmp / "agents.json", tmp / "admin")
    agent = reg.authenticate(reg.create_agent("evalbot", "trusted"))
    pos = load(SK / "gold.sample.jsonl") + load(SK / "gold.hard.jsonl")
    neg = load(SK / "gold.negative.jsonl")
    idx = ChromaDaemonIndex(cfg["rag"]["python"], str(tmp / "chroma"), timeout=180)
    try:
        print(f"corpus {corpus_active(tmp / 'graph.db')} active notes | positives {len(pos)} (easy {sha(SK / 'gold.sample.jsonl')}, hard {sha(SK / 'gold.hard.jsonl')}) | negatives {len(neg)} ({sha(SK / 'gold.negative.jsonl')})")
        rows = evalkit.sweep(lambda f, c: MemoryService(tmp / "graph.db", reg, index=idx, floor=f, fts_coverage=c), agent, pos, neg,
                             [float(x) for x in a.floors.split(",")], [float(x) for x in a.coverages.split(",")], a.k)
        print(evalkit.format_table(rows))
        print(f"\nscratch copy: {tmp} (not deleted)")
    finally:
        idx.close()


def sha(p):
    return file_sha16(p)


if __name__ == "__main__":
    main()
