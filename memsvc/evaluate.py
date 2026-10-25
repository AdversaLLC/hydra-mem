"""memctl eval (2.2 of the 0.3 plan): does recall find the right notes, say nothing when nothing is stored, and never return what it must not?
Standard library only; no model is called.

Questions are JSON lines ('#' lines are comments), three kinds:
  answerable     {"q": "...", "relevant": ["note id or id prefix", ...]}   recall@k, hit@1 and MRR
  unanswerable   {"q": "...", "relevant": []}                                the right answer is nothing; anything returned counts against it
  must-not       {"q": "...", "must_not": ["full id", ...]}                   a leak if one of those ids comes back, at any position
Must-not questions are also made from the store itself: rejected, superseded, quarantined and pending notes, each asked for by its own title.

The served floors (vector 0.25, keyword coverage 0.34) were chosen on the same 50 questions they were measured on. cross_validate checks that
honestly: each fold's setting is picked on the other folds only, scored on its own held-out questions, and the held-out scores are compared
with the defaults' by a paired bootstrap.
"""
import json
import random
import sqlite3
from pathlib import Path

K = 5
DEFAULT = (0.25, 0.34)
HIDDEN = ("rolled_back", "superseded", "quarantined", "reference")
TYPES = ("answerable", "unanswerable", "must_not")
OBJECTIVE = "MRR on answerable + share of unanswerable left unanswered - 2 x share of must-not questions that leaked"
GOLD_DIR = Path.home() / ".claude" / "skills" / "gsd-hydra-learn" / "scripts"


def default_question_files() -> list:
    return [GOLD_DIR / n for n in ("gold.sample.jsonl", "gold.hard.jsonl", "gold.negative.jsonl")]


def _ids(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def load_questions(paths) -> list:
    out = []
    for p in map(Path, paths):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise ValueError(f"{p}: cannot read it ({e.strerror or type(e).__name__})")
        for n, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            where = f"{p.name}:{n}"
            try:
                d = json.loads(s)
            except ValueError:
                raise ValueError(f"{where}: not valid JSON")
            if not isinstance(d, dict) or not isinstance(d.get("q"), str) or not d["q"].strip():
                raise ValueError(f'{where}: a question needs a non-empty "q"')
            rel, bad = d.get("relevant", []), d.get("must_not", [])
            if not _ids(rel) or not _ids(bad):
                raise ValueError(f'{where}: "relevant" and "must_not" must be lists of note ids')
            out.append({"q": d["q"].strip(), "relevant": rel, "must_not": bad, "source": where,
                        "type": "must_not" if bad else ("answerable" if rel else "unanswerable")})
    return out


def auto_must_not(db_path, per_status: int = 10) -> list:
    """Each hidden note asked for by its own title: the newest `per_status` of every hidden status. Run it on the evaluation copy."""
    con = sqlite3.connect(str(db_path))
    try:
        out = []
        for st in HIDDEN:
            for nid, title in con.execute("SELECT id, title FROM nodes WHERE status=? AND trim(coalesce(title, '')) <> '' "
                                          "ORDER BY ts DESC, rowid DESC LIMIT ?", (st, per_status)):
                out.append({"q": title.strip(), "relevant": [], "must_not": [nid], "type": "must_not", "source": f"auto:{st}"})
        return out
    finally:
        con.close()


def _hit(rid: str, prefixes) -> bool:
    return any(rid.startswith(p) for p in prefixes)


def score(ids: list, q: dict, k: int = K) -> dict:
    if q["type"] == "answerable":
        ranks = [n for n, rid in enumerate(ids, 1) if _hit(rid, q["relevant"])]
        first = ranks[0] if ranks else None
        got = {p for p in q["relevant"] if any(rid.startswith(p) for rid in ids[:k])}
        return {"rr": 1 / first if first else 0.0, "h1": 1 if first == 1 else 0, "hk": 1 if first and first <= k else 0,
                "recall_k": len(got) / len(q["relevant"])}
    if q["type"] == "unanswerable":
        return {"answered": bool(ids)}
    banned = set(q["must_not"])            # whole ids: a replacement note's id can start with the id of the note it hid (a -hash suffix)
    return {"violated": any(rid in banned for rid in ids)}


def utility(row: dict) -> float:
    """Every kind of question on one 0-1 scale, so held-out results can be compared question by question."""
    if row["type"] == "answerable":
        return float(row["rr"])
    if row["type"] == "unanswerable":
        return 0.0 if row["answered"] else 1.0
    return 0.0 if row["violated"] else 1.0


def _mean(v) -> float:
    return round(sum(v) / len(v), 3) if v else 0.0


def objective(rows) -> float:
    by = {t: [r for r in rows if r["type"] == t] for t in TYPES}
    mrr = sum(r["rr"] for r in by["answerable"]) / len(by["answerable"]) if by["answerable"] else 0.0
    left = sum(not r["answered"] for r in by["unanswerable"]) / len(by["unanswerable"]) if by["unanswerable"] else 0.0
    leaks = sum(r["violated"] for r in by["must_not"]) / len(by["must_not"]) if by["must_not"] else 0.0
    return mrr + left - 2 * leaks


def run(svc, agent, questions, k: int = K, project: str = "*") -> list:
    rows = []
    for q in questions:
        res = svc.recall(agent, q["q"], project=project, limit=max(k, 10))
        ids = [i["id"] for i in res["items"]]
        rows.append({"type": q["type"], "source": q["source"], "ids": ids, "degraded": bool(res.get("degraded")), **score(ids, q, k)})
    return rows


def bootstrap_ci(values, n_boot: int = 2000, seed: int = 1, alpha: float = 0.05) -> tuple:
    if not values:
        return (0.0, 0.0)
    rng, n = random.Random(seed), len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (round(means[int(alpha / 2 * n_boot)], 3), round(means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))], 3))


def paired_bootstrap(a, b, n_boot: int = 2000, seed: int = 1) -> dict:
    """Mean of a - b over paired questions, with a bootstrap interval; and how many questions each side won outright."""
    if len(a) != len(b):
        raise ValueError("paired samples must be the same length")
    d = [x - y for x, y in zip(a, b)]
    return {"n": len(d), "diff": _mean(d) + 0.0, "ci": bootstrap_ci(d, n_boot, seed), "a_better": sum(1 for x in d if x > 0),
            "b_better": sum(1 for x in d if x < 0)}


def summarize(rows, k: int = K, n_boot: int = 2000, seed: int = 1) -> dict:
    ans, un, mn = ([r for r in rows if r["type"] == t] for t in TYPES)
    rk, rr, h1 = [r["recall_k"] for r in ans], [r["rr"] for r in ans], [r["h1"] for r in ans]
    return {"k": k, "answerable": len(ans), "recall_at_k": _mean(rk), "recall_at_k_ci": bootstrap_ci(rk, n_boot, seed),
            "mrr": _mean(rr), "mrr_ci": bootstrap_ci(rr, n_boot, seed), "hit1": _mean(h1),
            "unanswerable": len(un), "answered": sum(r["answered"] for r in un),
            "must_not": len(mn), "violations": sum(r["violated"] for r in mn), "leaks": [r["source"] for r in mn if r["violated"]],
            "degraded": sum(r.get("degraded", False) for r in rows), "objective": round(objective(rows), 3)}


def folds(questions, n: int = 5, seed: int = 1) -> list:
    """The fold of every question, stratified by kind so each fold gets its share of every kind; the same seed gives the same split."""
    fold, rng = [0] * len(questions), random.Random(seed)
    for t in TYPES:
        idx = [i for i, q in enumerate(questions) if q["type"] == t]
        rng.shuffle(idx)
        start = rng.randrange(n)
        for pos, i in enumerate(idx):
            fold[i] = (start + pos) % n
    return fold


def cross_validate(table: dict, questions, default=DEFAULT, n: int = 5, seed: int = 1, n_boot: int = 2000) -> dict:
    """table: {(floor, coverage): rows aligned with questions}. Each fold picks the setting with the best objective on the OTHER folds
    (ties go to the default) and is scored on its own questions; the pooled held-out scores are compared with the defaults'."""
    if n < 2 or n > len(questions):
        raise ValueError(f"{n} folds need at least {n} questions (have {len(questions)})")
    if default not in table:
        raise ValueError("the default setting must be one of the settings tried")
    f = folds(questions, n, seed)
    out, chosen_u, default_u = [], [], []
    for k in range(n):
        train = [i for i in range(len(questions)) if f[i] != k]
        test = [i for i in range(len(questions)) if f[i] == k]

        def fit(cfg):
            return round(objective([table[cfg][i] for i in train]), 9)

        best = max(sorted(table), key=lambda c: (fit(c), c == default, c))
        cu = [utility(table[best][i]) for i in test]
        du = [utility(table[default][i]) for i in test]
        chosen_u += cu
        default_u += du
        out.append({"fold": k + 1, "chose": best, "train_n": len(train), "test_n": len(test), "train_objective": round(fit(best), 3),
                    "held_out_utility": _mean(cu), "default_held_out_utility": _mean(du)})
    return {"folds": out, "default": default, "default_chosen": sum(1 for o in out if o["chose"] == default), "objective": OBJECTIVE,
            "held_out": {"chosen": chosen_u, "default": default_u}, "paired": paired_bootstrap(chosen_u, default_u, n_boot, seed)}


def grid(make_svc, agent, questions, configs, k: int = K, project: str = "*") -> dict:
    return {tuple(cfg): run(make_svc(*cfg), agent, questions, k, project) for cfg in configs}


def evaluate(table: dict, questions, k: int = K, n_folds: int = 5, seed: int = 1, default=DEFAULT) -> dict:
    out = {"k": k, "default": {"config": default, "summary": summarize(table[default], k)},
           "grid": [{"config": cfg, **{x: s[x] for x in ("objective", "recall_at_k", "mrr", "answered", "violations")}}
                    for cfg, s in ((c, summarize(r, k)) for c, r in sorted(table.items()))]}
    try:
        out["cross_validation"] = cross_validate(table, questions, default, n_folds, seed)
    except ValueError as e:
        out["cross_validation"] = {"skipped": str(e)}
    return out


def _cfg(c) -> str:
    return f"floor {c[0]:.2f} / coverage {c[1]:.2f}"


def report(r: dict) -> str:
    s, k, q = r["default"]["summary"], r["k"], r["questions"]
    lines = [f"evaluated on a copy of the store: {r['copy']} (kept; the live store was not opened)",
             f"corpus: {r['corpus']} active notes; vectors {'on' if r['vectors'] else 'off (keyword search only)'}",
             f"questions: {q['answerable']} answerable, {q['unanswerable']} unanswerable, {q['must_not']} must-not "
             f"({q['auto_must_not']} made from hidden notes)",
             f"served defaults ({_cfg(r['default']['config'])}), k={k}:",
             f"  recall@{k} {s['recall_at_k']:.2f} [{s['recall_at_k_ci'][0]:.2f}-{s['recall_at_k_ci'][1]:.2f}]   "
             f"MRR {s['mrr']:.2f} [{s['mrr_ci'][0]:.2f}-{s['mrr_ci'][1]:.2f}]   hit@1 {s['hit1']:.2f}",
             f"  unanswerable answered: {s['answered']} of {s['unanswerable']} (the right answer was nothing)",
             f"  must-not leaks: {s['violations']} of {s['must_not']}" + (f"  LEAKED: {', '.join(s['leaks'][:10])}" if s["leaks"] else "")]
    if s["degraded"]:
        lines.append(f"  warning: {s['degraded']} question(s) ran with the vector lane down")
    best = max(r["grid"], key=lambda g: (g["objective"], tuple(g["config"]) == tuple(r["default"]["config"])))
    lines += [f"settings tried: {len(r['grid'])} (objective: {OBJECTIVE})",
              f"  best on ALL questions: {_cfg(best['config'])}, objective {best['objective']:.2f}; defaults {s['objective']:.2f} "
              "(picked and scored on the same questions, the flaw the 5-fold check below removes)"]
    cv = r["cross_validation"]
    if "skipped" in cv:
        lines.append(f"5-fold check skipped: {cv['skipped']}")
        return "\n".join(lines)
    lines.append(f"{len(cv['folds'])}-fold check of the defaults (each fold picks a setting on the other folds, then is scored on its own questions):")
    for f in cv["folds"]:
        lines.append(f"  fold {f['fold']}: chose {_cfg(f['chose'])} on {f['train_n']} questions; held-out utility {f['held_out_utility']:.2f} "
                     f"vs defaults {f['default_held_out_utility']:.2f} ({f['test_n']} questions)")
    p = cv["paired"]
    lo, hi = p["ci"]
    lines.append(f"  defaults chosen in {cv['default_chosen']} of {len(cv['folds'])} folds; held-out utility, choice minus defaults: "
                 f"{p['diff']:+.3f} [{lo:+.3f}, {hi:+.3f}] (paired bootstrap, {p['n']} questions)")
    lines.append("  reading: " + ("the held-out choice beat the defaults; add questions and re-run before changing them" if lo > 0 else
                                  "the defaults beat the held-out choice" if hi < 0 else
                                  "the interval includes 0: no evidence the defaults should change"))
    return "\n".join(lines)
