"""Tests for `memctl eval` (2.2 of the 0.3 plan): answerable, unanswerable and must-not-retrieve questions; recall at 5, MRR, an abstention
table, a paired bootstrap, and a 5-fold check of the floors. The command always works on a copy of the store. Run: python3 -I tests/test_eval.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc import evaluate as E  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402


def write_jsonl(path, rows, comment="# test questions"):
    Path(path).write_text(comment + "\n" + "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class Questions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_kinds_are_read_with_their_source_line(self):
        write_jsonl(self.d / "q.jsonl", [{"q": "where is the audit key", "relevant": ["n-1"]},
                                         {"q": "capital of australia", "relevant": []},
                                         {"q": "old watering advice", "relevant": [], "must_not": ["n-old"]}])
        qs = E.load_questions([self.d / "q.jsonl"])
        self.assertEqual([q["type"] for q in qs], ["answerable", "unanswerable", "must_not"])
        self.assertEqual(qs[0]["source"], "q.jsonl:2")
        self.assertEqual(qs[2]["must_not"], ["n-old"])

    def test_a_damaged_line_names_its_file_and_line(self):
        (self.d / "bad.jsonl").write_text('{"q": "fine", "relevant": []}\n{not json\n', encoding="utf-8")
        with self.assertRaises(ValueError) as e:
            E.load_questions([self.d / "bad.jsonl"])
        self.assertIn("bad.jsonl:2", str(e.exception))

    def test_a_question_needs_text(self):
        write_jsonl(self.d / "q.jsonl", [{"relevant": ["x"]}])
        with self.assertRaises(ValueError):
            E.load_questions([self.d / "q.jsonl"])
        write_jsonl(self.d / "q.jsonl", [{"q": "  ", "relevant": ["x"]}])
        with self.assertRaises(ValueError):
            E.load_questions([self.d / "q.jsonl"])


class Scoring(unittest.TestCase):
    def test_answerable_rank_metrics(self):
        s = E.score(["a", "r1", "b"], {"type": "answerable", "relevant": ["r1"], "must_not": []}, k=5)
        self.assertEqual((s["h1"], s["hk"], s["rr"], s["recall_k"]), (0, 1, 0.5, 1.0))
        s = E.score(["r1x", "b"], {"type": "answerable", "relevant": ["r1", "r2"], "must_not": []}, k=5)      # ids match by prefix
        self.assertEqual((s["h1"], s["recall_k"]), (1, 0.5))
        s = E.score(["a", "b", "c", "d", "e", "r1"], {"type": "answerable", "relevant": ["r1"], "must_not": []}, k=5)
        self.assertEqual((s["hk"], s["recall_k"], s["rr"]), (0, 0.0, 1 / 6))

    def test_unanswerable_is_scored_on_abstention(self):
        self.assertTrue(E.score(["junk"], {"type": "unanswerable", "relevant": [], "must_not": []}, k=5)["answered"])
        self.assertFalse(E.score([], {"type": "unanswerable", "relevant": [], "must_not": []}, k=5)["answered"])

    def test_must_not_counts_any_returned_position(self):
        q = {"type": "must_not", "relevant": [], "must_not": ["note/old"]}
        self.assertTrue(E.score(["a", "b", "c", "d", "e", "f", "note/old"], q, k=5)["violated"])
        self.assertFalse(E.score(["a", "b"], q, k=5)["violated"])

    def test_must_not_matches_whole_ids_only(self):
        q = {"type": "must_not", "relevant": [], "must_not": ["note/2026-10-24/lychee-watering"]}
        self.assertFalse(E.score(["note/2026-10-24/lychee-watering-bc4bfe"], q, k=5)["violated"])     # the replacement, not the hidden note

    def test_utility_puts_every_kind_on_one_scale(self):
        self.assertEqual(E.utility({"type": "answerable", "rr": 0.5}), 0.5)
        self.assertEqual(E.utility({"type": "unanswerable", "answered": False}), 1.0)
        self.assertEqual(E.utility({"type": "unanswerable", "answered": True}), 0.0)
        self.assertEqual(E.utility({"type": "must_not", "violated": True}), 0.0)

    def test_objective_rewards_rank_and_abstention_and_punishes_leaks(self):
        good = [{"type": "answerable", "rr": 1.0}, {"type": "unanswerable", "answered": False}, {"type": "must_not", "violated": False}]
        leaky = [{"type": "answerable", "rr": 1.0}, {"type": "unanswerable", "answered": False}, {"type": "must_not", "violated": True}]
        chatty = [{"type": "answerable", "rr": 1.0}, {"type": "unanswerable", "answered": True}, {"type": "must_not", "violated": False}]
        self.assertGreater(E.objective(good), E.objective(chatty))
        self.assertGreater(E.objective(chatty), E.objective(leaky))


class Statistics(unittest.TestCase):
    def test_bootstrap_interval_is_repeatable_and_brackets_the_mean(self):
        v = [1, 0, 1, 1, 0, 1, 1, 1, 0, 1]
        lo, hi = E.bootstrap_ci(v, seed=3)
        self.assertEqual((lo, hi), E.bootstrap_ci(v, seed=3))
        self.assertLessEqual(lo, 0.7)
        self.assertGreaterEqual(hi, 0.7)
        self.assertEqual(E.bootstrap_ci([]), (0.0, 0.0))

    def test_paired_bootstrap(self):
        same = E.paired_bootstrap([1, 0, 1], [1, 0, 1])
        self.assertEqual((same["diff"], same["ci"], same["a_better"], same["b_better"]), (0.0, (0.0, 0.0), 0, 0))
        apart = E.paired_bootstrap([1, 1, 1, 1], [0, 0, 0, 0])
        self.assertEqual((apart["diff"], apart["ci"], apart["a_better"]), (1.0, (1.0, 1.0), 4))
        with self.assertRaises(ValueError):
            E.paired_bootstrap([1, 0], [1])


class CrossValidation(unittest.TestCase):
    def qs(self, n_ans=10, n_un=5):
        return [{"type": "answerable"}] * n_ans + [{"type": "unanswerable"}] * n_un

    def test_folds_are_stratified_and_repeatable(self):
        qs = self.qs(20, 5)
        for seed in range(1, 9):                       # one lucky shuffle can look stratified; eight in a row cannot
            f = E.folds(qs, n=5, seed=seed)
            self.assertEqual(f, E.folds(qs, n=5, seed=seed))
            for k in range(5):
                self.assertEqual(sum(1 for i, q in enumerate(qs) if f[i] == k and q["type"] == "answerable"), 4, (seed, k))
                self.assertEqual(sum(1 for i, q in enumerate(qs) if f[i] == k and q["type"] == "unanswerable"), 1, (seed, k))
        self.assertNotEqual(E.folds(qs, n=5, seed=1), E.folds(qs, n=5, seed=2))

    def test_choice_is_made_without_the_held_out_fold(self):
        qs = self.qs(10, 0)
        f = E.folds(qs, n=5, seed=1)
        A, B = (0.25, 0.34), (0.5, 0.0)
        table = {A: [{"type": "answerable", "rr": 0.6} for _ in qs],
                 B: [{"type": "answerable", "rr": 1.0 if f[i] == 0 else 0.55} for i in range(len(qs))]}
        self.assertGreater(E.objective(table[B]), E.objective(table[A]))       # B wins overall only because of fold 0
        cv = E.cross_validate(table, qs, default=A, n=5, seed=1)
        self.assertEqual(cv["folds"][0]["chose"], A)                            # fold 0 must be chosen without fold 0's own answers
        self.assertEqual(cv["folds"][1]["chose"], B)

    def test_ties_go_to_the_default(self):
        qs = self.qs(5, 5)
        rows = [{"type": "answerable", "rr": 1.0}] * 5 + [{"type": "unanswerable", "answered": False}] * 5
        cv = E.cross_validate({(0.0, 0.0): rows, (0.25, 0.34): rows, (0.5, 0.5): rows}, qs, default=(0.25, 0.34), n=5)
        self.assertEqual(cv["default_chosen"], 5)
        self.assertEqual(cv["paired"]["diff"], 0.0)

    def test_held_out_scores_compare_the_choice_with_the_default(self):
        qs = self.qs(5, 5)
        good = [{"type": "answerable", "rr": 1.0}] * 5 + [{"type": "unanswerable", "answered": False}] * 5
        bad = [{"type": "answerable", "rr": 1.0}] * 5 + [{"type": "unanswerable", "answered": True}] * 5
        cv = E.cross_validate({(0.25, 0.34): bad, (0.4, 0.34): good}, qs, default=(0.25, 0.34), n=5)
        self.assertEqual(cv["default_chosen"], 0)
        self.assertEqual(cv["paired"]["diff"], 0.5)
        self.assertEqual(len(cv["held_out"]["chosen"]), 10)

    def test_more_folds_than_questions_is_refused(self):
        with self.assertRaises(ValueError):
            E.cross_validate({(0.25, 0.34): [{"type": "answerable", "rr": 1.0}] * 3}, self.qs(3, 0), default=(0.25, 0.34), n=5)


class OnAStore(ts.Base):
    def setUp(self):
        super().setUp()
        self.d = Path(self.tmp.name)
        self.ids = {}
        for k, (t, b) in {"fw": ("Firewall rule", "the firewall drops icmpv6 neighbor solicitation in the drop zone"),
                          "gpu": ("GPU thermals", "both compute cards climb to eighty two degrees within ninety seconds of load"),
                          "key": ("Audit key", "the audit key lives next to the database file with owner only permissions")}.items():
            self.ids[k] = self.svc.add(self.claude, "insight", t, b, project="p")["id"]
        self.old = self.svc.add(self.claude, "note", "Lychee watering", "lychee watering is heavy every day", project="p")["id"]
        self.svc.supersede(self.claude, self.old, "note", "Lychee watering", "lychee watering stays light in winter", project="p")
        self.bad = self.svc.add(self.qwen, "note", "Durian storage", "durian storage needs a sealed box", project="p")["id"]
        self.svc.reject(self.admin, self.bad, "wrong")
        self.questions = [
            {"q": "icmpv6 neighbor solicitation drop zone firewall", "relevant": [self.ids["fw"]], "must_not": [], "type": "answerable", "source": "t:1"},
            {"q": "where is the audit key stored permissions", "relevant": [self.ids["key"]], "must_not": [], "type": "answerable", "source": "t:2"},
            {"q": "recipe for vegetarian lasagna with spinach", "relevant": [], "must_not": [], "type": "unanswerable", "source": "t:3"}]

    def test_run_and_summarise(self):
        svc = MemoryService(self.d / "graph.db", self.reg, index=self.index, floor=0.25, fts_coverage=0.34)     # the served defaults
        rows = E.run(svc, self.claude, self.questions, k=5)
        self.assertEqual(len(rows), 3)
        s = E.summarize(rows, k=5)
        self.assertEqual((s["answerable"], s["recall_at_k"], s["mrr"]), (2, 1.0, 1.0))
        self.assertEqual((s["unanswerable"], s["answered"]), (1, 0))
        self.assertEqual(len(s["mrr_ci"]), 2)

    def test_auto_must_not_asks_for_hidden_notes_by_their_own_titles(self):
        auto = E.auto_must_not(self.d / "graph.db")
        by_id = {q["must_not"][0]: q for q in auto}
        self.assertIn(self.old, by_id)
        self.assertIn(self.bad, by_id)
        self.assertEqual(by_id[self.old]["q"], "Lychee watering")
        self.assertTrue(all(q["type"] == "must_not" and q["source"].startswith("auto:") for q in auto))
        s = E.summarize(E.run(self.svc, self.claude, auto, k=5), k=5)
        self.assertEqual((s["must_not"], s["violations"]), (len(auto), 0))

    def test_a_leak_is_reported(self):
        class Leaky:
            def recall(self, agent, q, project="*", limit=10):
                return {"items": [{"id": "bad-note"}]}
        rows = E.run(Leaky(), None, [{"q": "x", "relevant": [], "must_not": ["bad-note"], "type": "must_not", "source": "t:1"}], k=5)
        self.assertEqual(E.summarize(rows, k=5)["violations"], 1)

    def test_grid_evaluates_every_setting_on_every_question(self):
        configs = [(0.0, 0.0), (0.25, 0.34)]
        table = E.grid(lambda f, c: MemoryService(self.d / "graph.db", self.reg, index=self.index, floor=f, fts_coverage=c), self.claude,
                       self.questions, configs, k=5)
        self.assertEqual(sorted(table), configs)
        self.assertTrue(all(len(rows) == 3 for rows in table.values()))


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        reg = Registry(self.d / "agents.json", self.d / "admin.token")
        self.agent = reg.authenticate(reg.create_agent("writer", "trusted"))
        svc = MemoryService(self.d / "graph.db", reg)
        self.fw = svc.add(self.agent, "insight", "Firewall rule", "the firewall drops icmpv6 neighbor solicitation in the drop zone", project="p")["id"]
        self.gpu = svc.add(self.agent, "insight", "GPU thermals", "both compute cards climb to eighty two degrees within ninety seconds of load", project="p")["id"]
        old = svc.add(self.agent, "note", "Lychee watering", "lychee watering is heavy every day", project="p")["id"]
        svc.supersede(self.agent, old, "note", "Lychee watering", "lychee watering stays light in winter", project="p")
        rows = [{"q": "icmpv6 neighbor solicitation firewall", "relevant": [self.fw]}, {"q": "compute cards degrees load", "relevant": [self.gpu]},
                {"q": "firewall drop zone rule", "relevant": [self.fw]}, {"q": "eighty two degrees cards", "relevant": [self.gpu]},
                {"q": "neighbor solicitation icmpv6", "relevant": [self.fw]},
                {"q": "vegetarian lasagna spinach", "relevant": []}, {"q": "capital city of australia", "relevant": []},
                {"q": "best time to plant tulips", "relevant": []}, {"q": "how to renew a passport", "relevant": []},
                {"q": "chess opening for beginners", "relevant": []}]
        write_jsonl(self.d / "q.jsonl", rows)
        self.env = dict(os.environ, MEMSVC_HOME=str(self.d / "home"))

    def tearDown(self):
        self.tmp.cleanup()

    def digest(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.d.iterdir() if p.name.startswith("graph")}

    def run_eval(self, *extra):
        return subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), "eval", "--db", str(self.d / "graph.db"), "--no-vectors",
                               "--questions", str(self.d / "q.jsonl"), "--floors", "0,0.25", "--coverages", "0,0.34", *extra],
                              capture_output=True, text=True, env=self.env, timeout=300)

    def test_eval_reports_and_never_touches_the_store(self):
        before = self.digest()
        p = self.run_eval()
        self.assertEqual(p.returncode, 0, p.stderr)
        for want in ("recall@5", "MRR", "unanswerable", "must-not", "5-fold", "copy"):
            self.assertIn(want, p.stdout)
        self.assertEqual(self.digest(), before)

    def test_eval_never_opens_the_live_store(self):
        """SQLite must create its -wal and -shm files beside a database it opens. With the store's folder read-only, only an evaluation of a
        copy can run; one that opened the live store would fail here."""
        os.chmod(self.d, 0o555)
        try:
            p = self.run_eval("--json")
        finally:
            os.chmod(self.d, 0o755)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["default"]["summary"]["answerable"], 5)

    def test_json_output(self):
        p = self.run_eval("--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        j = json.loads(p.stdout)
        self.assertEqual(j["default"]["config"], [0.25, 0.34])
        self.assertEqual(j["default"]["summary"]["answerable"], 5)
        self.assertEqual(j["default"]["summary"]["unanswerable"], 5)
        self.assertGreaterEqual(j["default"]["summary"]["must_not"], 1)
        self.assertEqual(j["default"]["summary"]["violations"], 0)
        self.assertEqual(len(j["cross_validation"]["folds"]), 5)
        self.assertTrue(Path(j["copy"]).is_dir())

    def test_a_missing_questions_file_is_an_error(self):
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), "eval", "--db", str(self.d / "graph.db"), "--no-vectors",
                            "--questions", str(self.d / "missing.jsonl")], capture_output=True, text=True, env=self.env, timeout=120)
        self.assertEqual(p.returncode, 2)
        self.assertIn("missing.jsonl", p.stderr)


if __name__ == "__main__":
    unittest.main()
