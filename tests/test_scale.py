"""Tests for item 1.8: what happens when the store outgrows the listing scan. Listings, timelines, session lists and the start-of-session context read
the newest READ_CAP active notes, newest first, and filter them for the caller. Past the cap the oldest notes silently dropped out of those views
(and, for the timeline, the NEWEST ones did). These tests use a tiny cap so the cut can be seen. Run: python3 -I tests/test_scale.py
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc import render, service  # noqa: E402
from memsvc.service import MemoryService, scale_status  # noqa: E402


class Scan(ts.Base):
    def fill(self, n, agent=None, project="p", session=None, scope="shared", start=0):
        ids = []
        for i in range(start, start + n):
            self.now[0] += 60
            ids.append(self.svc.add(agent or self.claude, "note", f"Scale note {i:03d}", f"fact number {i} about subject{i} and nothing else", project=project,
                                    session=session, scope=scope)["id"])
        return ids

    def test_a_listing_under_the_cap_is_not_flagged(self):
        self.fill(10)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.search(self.claude, "")
        self.assertEqual(r["total"], 10)
        self.assertNotIn("scan_capped", r)

    def test_a_listing_over_the_cap_says_so_and_drops_the_oldest(self):
        self.fill(12)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.search(self.claude, "", limit=100)
        self.assertIs(r["scan_capped"], True)
        titles = [i["title"] for i in r["items"]]
        self.assertEqual(r["total"], 10)
        self.assertEqual(titles[0], "Scale note 011")                                   # newest first
        self.assertNotIn("Scale note 000", titles)
        self.assertNotIn("Scale note 001", titles)

    def test_oldest_first_listing_still_keeps_the_newest_notes(self):
        self.fill(12)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.search(self.claude, "", limit=100, order_by="date_asc")
        titles = [i["title"] for i in r["items"]]
        self.assertEqual((titles[0], titles[-1]), ("Scale note 002", "Scale note 011"))  # the cut takes the oldest, whichever way it is sorted
        self.assertIs(r["scan_capped"], True)

    def test_a_search_with_words_is_not_limited_by_the_listing_cap(self):
        self.fill(12)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.search(self.claude, "subject0 fact number 0")
        self.assertIn("Scale note 000", [i["title"] for i in r["items"]])
        self.assertNotIn("scan_capped", r)

    def test_the_timeline_keeps_the_newest_notes_when_the_scan_is_capped(self):
        ids = self.fill(12)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.timeline(self.claude, depth_before=2, depth_after=0, project="p")
        self.assertEqual(r["items"][-1]["id"], ids[-1])                                  # before the fix the NEWEST notes were the ones dropped
        self.assertIs(r["scan_capped"], True)

    def test_an_anchor_older_than_the_scan_says_the_scan_was_capped(self):
        ids = self.fill(12)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.timeline(self.claude, anchor="#" + self.svc.handle(ids[0]))
        self.assertFalse(r["sufficient"])
        self.assertIs(r["scan_capped"], True)

    def test_a_timeline_under_the_cap_is_not_flagged(self):
        ids = self.fill(5)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.timeline(self.claude, anchor="#" + self.svc.handle(ids[2]))
        self.assertTrue(r["sufficient"])
        self.assertNotIn("scan_capped", r)

    def test_sessions_over_the_cap_are_flagged(self):
        self.fill(12, session="run1")
        with mock.patch.object(service, "READ_CAP", 10):
            rows = self.svc.sessions(self.claude)
        self.assertEqual(rows[0]["count"], 10)
        self.assertIs(rows[0]["scan_capped"], True)
        with mock.patch.object(service, "READ_CAP", 20):
            self.assertNotIn("scan_capped", self.svc.sessions(self.claude)[0])

    def test_context_over_the_cap_is_flagged(self):
        self.fill(12)
        with mock.patch.object(service, "READ_CAP", 10):
            r = self.svc.context(self.claude, project="p")
        self.assertIs(r["scan_capped"], True)
        with mock.patch.object(service, "READ_CAP", 20):
            self.assertNotIn("scan_capped", self.svc.context(self.claude, project="p"))


class ContextPool(ts.Base):
    def mine(self):
        ids = []
        for i in range(2):
            self.now[0] += 60
            ids.append(self.svc.add(self.claude, "decision", f"Mine {i}", f"a decision of mine number {i} about subject{i} that matters", project="p")["id"])
        return set(ids)

    def test_hidden_newer_notes_cannot_push_visible_ones_out_of_the_context(self):
        """The context used to take the newest 3000 notes and only then drop the ones the caller may not read, so a project's notes could vanish
        from its own context behind newer notes of someone else."""
        other = self.agent("other", "trusted")
        mine = self.mine()
        for i in range(8):
            self.now[0] += 60
            self.svc.add(other, "decision", f"Theirs {i}", f"a private decision number {i} about topic{i}", project="p", scope="private")
        with mock.patch.object(service, "CONTEXT_POOL", 5):
            r = self.svc.context(self.claude, project="p")
        self.assertEqual({i["id"] for i in r["items"]}, mine)

    def test_notes_of_other_projects_cannot_push_a_project_out_either(self):
        mine = self.mine()
        for i in range(8):
            self.now[0] += 60
            self.svc.add(self.claude, "decision", f"Elsewhere {i}", f"a decision number {i} about topic{i}", project="q")
        with mock.patch.object(service, "CONTEXT_POOL", 5):
            r = self.svc.context(self.claude, project="p")
        self.assertEqual({i["id"] for i in r["items"]}, mine)

    def test_the_pool_still_limits_how_many_visible_notes_are_sorted(self):
        self.mine()
        for i in range(8):
            self.now[0] += 60
            self.svc.add(self.claude, "decision", f"More {i}", f"another decision number {i} about topic{i}", project="p")
        with mock.patch.object(service, "CONTEXT_POOL", 5):
            r = self.svc.context(self.claude, project="p", budget_tokens=5000)
        self.assertEqual(len(r["items"]), 5)


class Levels(unittest.TestCase):
    def test_levels_at_the_real_thresholds(self):
        self.assertEqual(service.WARN_AT, 8000)
        self.assertEqual(service.READ_CAP, 20000)
        self.assertEqual(scale_status(7999)["level"], "ok")
        self.assertIsNone(scale_status(7999)["message"])
        s = scale_status(8000)
        self.assertEqual(s["level"], "slowing")
        self.assertIn("slower", s["message"])
        self.assertIn("20,000", s["message"])
        self.assertEqual(scale_status(19999)["level"], "slowing")
        self.assertEqual(scale_status(20000)["level"], "full")
        self.assertIn("newest 20,000", scale_status(20000)["message"])

    def test_the_message_counts_what_is_already_left_out(self):
        self.assertIn("1,500 older", scale_status(21500)["message"])
        self.assertIn("recall", scale_status(21500)["message"])                            # a search with words still reaches them
        self.assertIn("no older note is left out yet", scale_status(20000)["message"])

    def test_thresholds_follow_the_module_constants(self):
        with mock.patch.object(service, "WARN_AT", 6), mock.patch.object(service, "READ_CAP", 10):
            self.assertEqual([scale_status(n)["level"] for n in (5, 6, 9, 10, 11)], ["ok", "slowing", "slowing", "full", "full"])


class InStats(ts.Base):
    def test_stats_carries_the_scale_for_the_owner_only(self):
        for i in range(7):
            self.now[0] += 60
            self.svc.add(self.claude, "note", f"S {i}", f"fact {i} about subject{i} and nothing else", project="p")
        with mock.patch.object(service, "WARN_AT", 6), mock.patch.object(service, "READ_CAP", 10):
            s = self.svc.stats(self.admin)["scale"]
            self.assertEqual((s["level"], s["active"], s["warn_at"], s["cap"]), ("slowing", 7, 6, 10))
            self.assertIn("slower", s["message"])
            for who in (self.claude, self.qwen):
                self.assertNotIn("scale", self.svc.stats(who))


class Rendering(unittest.TestCase):
    NOTE = "older notes are not listed here"

    def test_an_index_that_was_capped_says_what_it_did_not_scan(self):
        t = render.index({"items": [{"id": "n/1", "handle": "aaaa1111", "kind": "note", "conf": "U", "trust": "agent", "title": "T", "snippet": "s",
                                     "ts": "2026-01-01T00:00:00Z", "read_tokens": 5}], "total": 1, "total_read_tokens": 5, "sufficient": True, "scan_capped": True})
        self.assertIn(self.NOTE, t)
        self.assertIn("query", t)

    def test_an_index_that_was_not_capped_says_nothing_of_the_kind(self):
        t = render.index({"items": [{"id": "n/1", "handle": "aaaa1111", "kind": "note", "conf": "U", "trust": "agent", "title": "T", "snippet": "s",
                                     "ts": "2026-01-01T00:00:00Z", "read_tokens": 5}], "total": 1, "total_read_tokens": 5, "sufficient": True})
        self.assertNotIn(self.NOTE, t)

    def test_a_timeline_that_was_capped_says_so_even_when_it_found_nothing(self):
        t = render.timeline({"anchor": None, "items": [], "total_read_tokens": 0, "sufficient": False, "scan_capped": True})
        self.assertIn(self.NOTE, t)


class DoctorWarns(unittest.TestCase):
    """memctl doctor runs without the service, so it counts active notes straight from the database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name) / "home"
        env = {"PATH": __import__("os").environ["PATH"], "HOME": __import__("os").environ["HOME"], "MEMSVC_HOME": str(cls.home)}
        subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), "init"], capture_output=True, text=True, env=env, timeout=60, check=True)
        cls.env = env
        cls.db = cls.home / "graph.db"
        from memsvc.registry import Registry
        MemoryService(cls.db, Registry(cls.home / "agents.json", cls.home / "admin.token"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def grow(self, upto):
        con = sqlite3.connect(self.db)
        have = con.execute("SELECT count(*) FROM nodes WHERE status='active'").fetchone()[0]
        rows = [(f"bulk/{i}", "note", f"bulk {i}", f"bulk body {i}", "p", "U", "svc-trusted:bulk", "agent", "", "active", None,
                 f"2026-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}Z", f"2026-01-01T00:00:00Z", f"h{i}", "{}") for i in range(have, upto)]
        con.executemany("INSERT INTO nodes(id,kind,title,body,project,conf,source,trust,tags,status,session,ts,updated,hash,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def doctor(self):
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), "doctor", "--db", str(self.db), "--url", "http://127.0.0.1:9",
                            "--chroma", str(Path(self.tmp.name) / "none")], capture_output=True, text=True, env=self.env, timeout=120)
        return p.stdout

    def test_doctor_is_quiet_below_the_first_threshold_then_warns_twice(self):
        self.grow(7999)
        self.assertNotIn("scale:", self.doctor())
        self.grow(8000)
        out = self.doctor()
        self.assertRegex(out, r"WARN\s+scale: 8,000 active notes.*slower")
        self.grow(20000)
        out = self.doctor()
        self.assertRegex(out, r"WARN\s+scale: 20,000 active notes.*newest 20,000")
        self.assertNotRegex(out, r"scale:.*slower")


if __name__ == "__main__":
    unittest.main(verbosity=1)
