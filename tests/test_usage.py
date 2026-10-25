"""Tests for the usage log (2.1 of the 0.3 plan): one counts-only row per read, never text and never ids, owner-only, and never used for
ranking. The owner reads it with `memctl census` or GET /v1/usage. Run: python3 -I tests/test_usage.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc import usage  # noqa: E402
from memsvc.client import ApiError, MemoryClient  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402

DAY = 86_400
FIELDS = {"ts", "op", "agent", "notes", "served_tokens", "full_tokens", "asked", "abstained", "degraded", "ms", "age"}


class Broken(ts.FakeIndex):
    def query(self, text, project, limit):
        raise RuntimeError("vector tier down")


class Log(ts.Base):
    def setUp(self):
        super().setUp()
        self.d = Path(self.tmp.name)
        self.path = self.d / "usage.jsonl"
        self.svc.usage = usage.UsageLog(self.path, clock=lambda: self.now[0])
        self.a = self.svc.add(self.claude, "decision", "Quokka grafting", "quokka grafting uses zygomorphic splints in spring", project="orchard")["id"]
        self.b = self.svc.add(self.claude, "note", "Quokka watering", "quokka watering stays xerophytic in winter", project="orchard")["id"]

    def rows(self):
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()] if self.path.exists() else []

    def read_everything(self):
        self.svc.recall(self.claude, "quokka grafting", project="orchard")
        self.svc.search(self.claude, "quokka", project="orchard")
        self.svc.search(self.claude, "", project="orchard")
        self.svc.timeline(self.claude, anchor=self.a)
        self.svc.get_observations(self.claude, [self.a, self.b])
        self.svc.context(self.claude, project="orchard")
        self.svc.get(self.claude, self.a)

    def test_every_read_path_writes_one_row(self):
        self.read_everything()
        self.assertEqual([r["op"] for r in self.rows()], ["recall", "search", "search", "timeline", "observations", "context", "node"])
        self.assertTrue(all(r["agent"] == "claude" for r in self.rows()))

    def test_rows_hold_counts_only_never_text_or_ids(self):
        self.read_everything()
        raw = self.path.read_text(encoding="utf-8").lower()
        for leak in ("quokka", "grafting", "zygomorphic", "xerophytic", "orchard", self.a.lower(), self.b.lower(),
                     self.svc.handle(self.a), self.svc.handle(self.b)):
            self.assertNotIn(leak, raw)
        for r in self.rows():
            self.assertEqual(set(r), FIELDS)
            self.assertIn(r["op"], usage.OPS)
            for k in ("notes", "served_tokens", "full_tokens"):
                self.assertIsInstance(r[k], int)
            for k in ("asked", "abstained", "degraded"):
                self.assertIsInstance(r[k], bool)
            self.assertTrue(set(r["age"]) <= {b for b, _ in usage.AGE_BANDS})
            self.assertTrue(all(isinstance(v, int) for v in r["age"].values()))

    def test_index_reads_count_what_a_full_fetch_would_have_cost(self):
        res = self.svc.search(self.claude, "quokka", project="orchard")
        row = self.rows()[-1]
        self.assertEqual(row["notes"], 2)
        self.assertEqual(row["full_tokens"], sum(i["read_tokens"] for i in res["items"]))
        self.assertLess(row["served_tokens"], row["full_tokens"] + 1)
        self.assertGreater(row["served_tokens"], 0)

    def test_a_full_fetch_serves_what_it_costs(self):
        res = self.svc.get_observations(self.claude, [self.a, self.b])
        row = self.rows()[-1]
        self.assertEqual((row["notes"], row["served_tokens"], row["full_tokens"]), (2, res["total_read_tokens"], res["total_read_tokens"]))

    def test_a_budgeted_recall_counts_the_packed_block(self):
        res = self.svc.recall(self.claude, "quokka grafting", project="orchard", budget_tokens=60)
        row = self.rows()[-1]
        self.assertEqual(row["served_tokens"], res["used_tokens"])          # the block's header can cost more than tiny notes: no saving is claimed then
        self.assertGreater(row["full_tokens"], 0)

    def test_abstention_is_counted_only_for_reads_that_asked(self):
        self.svc.recall(self.claude, "vegetarian lasagna recipe", project="orchard")
        self.svc.search(self.claude, "", project="nowhere")
        asked, listing = self.rows()
        self.assertEqual((asked["asked"], asked["abstained"], asked["notes"]), (True, True, 0))
        self.assertEqual((listing["asked"], listing["abstained"]), (False, False))

    def test_a_hit_is_not_an_abstention(self):
        self.svc.recall(self.claude, "quokka grafting", project="orchard")
        self.assertEqual((self.rows()[-1]["asked"], self.rows()[-1]["abstained"]), (True, False))

    def test_a_degraded_read_is_flagged(self):
        svc = MemoryService(self.d / "g2.db", self.reg, index=Broken(), clock=lambda: self.now[0], usage=usage.UsageLog(self.d / "u2.jsonl"))
        svc.add(self.claude, "note", "Quokka pruning", "quokka pruning keeps the canopy open", project="orchard")
        svc.recall(self.claude, "quokka pruning", project="orchard")
        row = json.loads((self.d / "u2.jsonl").read_text().splitlines()[-1])
        self.assertTrue(row["degraded"])

    def test_age_of_what_was_read_is_banded(self):
        self.now[0] = time.time() + 10 * DAY
        self.svc.search(self.claude, "quokka", project="orchard")
        self.assertEqual(self.rows()[-1]["age"], {"month": 2})

    def test_age_band_edges(self):
        for age, band in ((-5, "day"), (0, "day"), (DAY - 1, "day"), (DAY, "week"), (7 * DAY - 1, "week"), (7 * DAY, "month"),
                          (30 * DAY, "quarter"), (91 * DAY, "year"), (365 * DAY, "older"), (5000 * DAY, "older")):
            self.assertEqual(usage.age_band(age), band, age)

    def test_a_cached_recall_is_still_a_read(self):
        self.svc.recall(self.claude, "quokka grafting", project="orchard")
        self.svc.recall(self.claude, "quokka grafting", project="orchard")
        self.assertEqual(self.svc.cache_stats()["hits"], 1)
        self.assertEqual([r["op"] for r in self.rows()], ["recall", "recall"])

    def test_writes_and_refused_reads_write_no_row(self):
        self.svc.add(self.claude, "note", "Quokka mulch", "quokka mulch is bark chips", project="orchard")
        self.svc.supersede(self.claude, self.b, "note", "Quokka watering", "quokka watering stays light in winter", project="orchard")
        scoped = self.agent("scoped", "trusted", read=("other",), write=("other",))
        with self.assertRaises(PermissionError):
            self.svc.recall(scoped, "quokka", project="orchard")
        with self.assertRaises(ValueError):
            self.svc.get_observations(self.claude, [])
        self.assertEqual(self.rows(), [])

    def test_the_file_is_private_to_the_owner(self):
        self.svc.search(self.claude, "quokka", project="orchard")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_log_that_cannot_be_written_never_fails_a_read(self):
        self.svc.usage = usage.UsageLog(self.d)                           # a directory: every append fails
        res = self.svc.recall(self.claude, "quokka grafting", project="orchard")
        self.assertEqual(res["items"][0]["id"], self.a)

    def test_only_the_owner_gets_the_summary(self):
        with self.assertRaises(PermissionError):
            self.svc.usage_summary(self.claude)
        self.svc.search(self.claude, "quokka", project="orchard")
        self.assertEqual(self.svc.usage_summary(self.admin)["by_op"], {"search": 1})
        self.assertEqual(self.svc.usage_summary(self.admin, days=1)["reads"], 1)

    def test_without_a_log_nothing_is_written(self):
        self.svc.usage = None
        self.read_everything()
        self.assertFalse(self.path.exists())

    def test_ranking_never_reads_the_log(self):
        before = self.ids(MemoryService(self.d / "graph.db", self.reg, index=self.index, clock=lambda: self.now[0]).recall(self.claude, "quokka", project="orchard"))
        noisy = usage.UsageLog(self.path, clock=lambda: self.now[0])
        for _ in range(300):
            noisy.record("recall", "qwen", notes=9, served_tokens=1, full_tokens=900, asked=True, abstained=False, degraded=False, ms=1.0, ages={"older": 9})
        after = self.ids(MemoryService(self.d / "graph.db", self.reg, index=self.index, clock=lambda: self.now[0], usage=noisy).recall(self.claude, "quokka", project="orchard"))
        self.assertEqual(before, after)


class Summary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.jsonl"
        self.now = [5_000_000.0]
        self.log = usage.UsageLog(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, op="search", agent="claude", notes=2, served=20, full=100, asked=True, abstained=False, degraded=False, ms=10.0, ages=None):
        self.log.record(op, agent, notes=notes, served_tokens=served, full_tokens=full, asked=asked, abstained=abstained, degraded=degraded, ms=ms,
                        ages=ages if ages is not None else {"day": notes})

    def test_summary_counts_by_operation_agent_and_flag(self):
        self.rec("search", "claude", ms=10)
        self.rec("recall", "claude", notes=0, served=0, full=0, abstained=True, ms=30, ages={})
        self.rec("recall", "qwen", degraded=True, ms=20)
        self.rec("context", "qwen", asked=False, ms=40, ages={"week": 1, "older": 1})
        s = self.log.summary()
        self.assertEqual(s["reads"], 4)
        self.assertEqual(s["by_op"], {"search": 1, "recall": 2, "context": 1})
        self.assertEqual(s["by_agent"], {"claude": 2, "qwen": 2})
        self.assertEqual((s["asked"], s["abstained"], s["degraded"]), (3, 1, 1))
        self.assertEqual(s["notes"], 6)
        self.assertEqual(s["age"], {"day": 4, "week": 1, "older": 1})
        self.assertEqual(s["ms"]["median"], 25.0)
        self.assertEqual(s["ms"]["p95"], 40.0)
        self.assertIn("never", s["note"])

    def test_savings_card_names_its_baseline_and_stays_below_100_percent(self):
        card = usage.savings_card(0, 1000)
        self.assertEqual((card["saved_percent"], card["capped"]), (usage.SAVINGS_CAP, True))
        self.assertLess(usage.SAVINGS_CAP, 100)
        self.assertIn("full text", card["baseline"])
        self.assertEqual(usage.savings_card(250, 1000)["saved_percent"], 75)
        self.assertEqual(usage.savings_card(250, 1000)["capped"], False)
        self.assertEqual(usage.savings_card(0, 0)["saved_percent"], 0)
        self.assertEqual(usage.savings_card(1200, 1000)["saved_percent"], 0)
        self.assertEqual(usage.savings_card(1200, 1000)["saved_tokens"], 0)

    def test_summary_carries_the_savings_card(self):
        self.rec(served=20, full=100)
        self.rec(served=30, full=100)
        card = self.log.summary()["savings"]
        self.assertEqual((card["served_tokens"], card["baseline_tokens"], card["saved_percent"]), (50, 200, 75))

    def test_damaged_lines_are_skipped_and_counted(self):
        self.rec()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("not json\n{\"op\": \"recall\"}\n")
        s = self.log.summary()
        self.assertEqual((s["reads"], s["skipped"]), (1, 2))

    def test_since_keeps_only_recent_rows(self):
        self.rec(op="search")
        self.now[0] += 3 * DAY
        self.rec(op="recall")
        s = self.log.summary(since=self.now[0] - DAY)
        self.assertEqual(s["by_op"], {"recall": 1})

    def test_an_empty_log_summarises_to_zero(self):
        s = self.log.summary()
        self.assertEqual((s["reads"], s["savings"]["saved_percent"]), (0, 0))

    def test_an_unknown_operation_is_refused(self):
        with self.assertRaises(ValueError):
            self.rec(op="add")


class Route(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.reg = Registry(d / "agents.json", d / "admin.token")
        cls.admin = cls.reg.ensure_admin()
        cls.claude = cls.reg.create_agent("claude", "trusted")
        cls.svc = MemoryService(d / "graph.db", cls.reg, usage=usage.UsageLog(d / "usage.jsonl"))
        cls.httpd = make_server(cls.svc, cls.reg, port=0)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        c = MemoryClient(cls.url, cls.claude)
        c.add("note", "Quokka grafting", "quokka grafting uses splints", project="orchard")
        c.search("quokka", project="orchard")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def test_only_the_owner_reads_the_usage_summary(self):
        with self.assertRaises(ApiError) as e:
            MemoryClient(self.url, self.claude).usage()
        self.assertEqual(e.exception.status, 403)
        s = MemoryClient(self.url, self.admin).usage()
        self.assertTrue(s["enabled"])
        self.assertEqual(s["by_op"], {"search": 1})

    def test_reading_the_summary_is_not_itself_a_read_of_notes(self):
        c = MemoryClient(self.url, self.admin)
        n = c.usage()["reads"]
        self.assertEqual(c.usage()["reads"], n)


class Census(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "memsvc"
        self.home.mkdir()
        self.env = dict(os.environ, MEMSVC_HOME=str(self.home))

    def tearDown(self):
        self.tmp.cleanup()

    def census(self, *args):
        return subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), "census", *args], capture_output=True, text=True, env=self.env, timeout=60)

    def test_census_without_a_log_says_so(self):
        p = self.census()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no reads logged yet", p.stdout)

    def test_census_prints_counts_and_the_savings_card(self):
        log = usage.UsageLog(self.home / "usage.jsonl")
        log.record("search", "claude", notes=3, served_tokens=30, full_tokens=120, asked=True, abstained=False, degraded=False, ms=12.0, ages={"day": 3})
        log.record("recall", "qwen", notes=0, served_tokens=0, full_tokens=0, asked=True, abstained=True, degraded=False, ms=40.0, ages={})
        p = self.census()
        self.assertEqual(p.returncode, 0, p.stderr)
        for want in ("2 reads", "search 1", "recall 1", "claude 1", "qwen 1", "1 of 2", "75%", "baseline", "never"):
            self.assertIn(want, p.stdout)
        j = json.loads(self.census("--json").stdout)
        self.assertEqual(j["reads"], 2)


if __name__ == "__main__":
    unittest.main()
