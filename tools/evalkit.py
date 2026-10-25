"""Sweep the service's vector floor and keyword coverage against gold questions and out-of-corpus negatives.

Positives measure whether the right note comes back; negatives measure whether the service abstains when nothing relevant
is stored. A floor that is too low answers everything (small models then treat nearest junk as fact); one that is too high
hides real answers. This puts both numbers in one table so the default is chosen by measurement, not feel.
Reuses the scoring helpers from the gsd-hydra-learn skill (intervals, paired test).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "gsd-hydra-learn" / "scripts"))
from eval_recall import bootstrap_ci, score_row  # noqa: E402


def sweep(make_svc, agent, positives, negatives, floors, coverages, k=5, project="*"):
    """make_svc(floor, coverage) -> MemoryService. Returns one dict per (floor, coverage) pair."""
    out = []
    for floor in floors:
        for cov in coverages:
            svc = make_svc(floor, cov)
            rows = []
            for g in positives:
                res = svc.recall(agent, g["q"], project=project, limit=max(k, 10))
                s = score_row([i["id"] for i in res["items"]], g["relevant"], k)
                if s is not None:
                    rows.append(s)
            answered = sum(1 for g in negatives if svc.recall(agent, g["q"], project=project, limit=k)["items"])
            n = max(len(rows), 1)
            h1 = [r["h1"] for r in rows]
            out.append({"floor": floor, "coverage": cov, "n_pos": len(rows), "hit1": sum(h1) / n, "hit1_ci": bootstrap_ci(h1),
                        "hit5": sum(r["hk"] for r in rows) / n, "mrr": sum(r["rr"] for r in rows) / n,
                        "neg_answered": answered, "neg_n": len(negatives)})
    return out


def format_table(rows) -> str:
    lines = [f"{'floor':>6} {'cover':>6} {'hit@1':>6} {'hit@5':>6} {'MRR':>6} {'negatives answered':>20}"]
    for r in rows:
        lines.append(f"{r['floor']:6.2f} {r['coverage']:6.2f} {r['hit1']:6.2f} {r['hit5']:6.2f} {r['mrr']:6.2f} {r['neg_answered']:>12}/{r['neg_n']}")
    return "\n".join(lines)
