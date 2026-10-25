"""Tests for memsvc.registry: agent tokens, lanes, scopes, rate limits. Run: python3 -I tests/test_registry.py"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memsvc.registry import Registry, RateLimiter, LANES  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "agents.json"
        self.admin_path = self.dir / "admin.token"
        self.reg = Registry(self.path, self.admin_path)

    def tearDown(self):
        self.tmp.cleanup()


class Agents(Base):
    def test_create_authenticate_and_scopes(self):
        tok = self.reg.create_agent("qwen-local", "probation", read=["*"], write=["adversa"])
        a = self.reg.authenticate(tok)
        self.assertEqual((a.name, a.lane), ("qwen-local", "probation"))
        self.assertTrue(a.can_read("anything"))
        self.assertTrue(a.can_write("adversa"))
        self.assertFalse(a.can_write("other"))
        self.assertTrue(a.can_read("adversa"))

    def test_token_is_never_stored_in_plaintext(self):
        tok = self.reg.create_agent("a1", "trusted")
        text = self.path.read_text()
        self.assertNotIn(tok, text)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertGreaterEqual(len(tok), 32)

    def test_bad_tokens_fail(self):
        self.reg.create_agent("a1", "trusted")
        for bad in ("", None, "x" * 10, "Bearer abc", 5):
            self.assertIsNone(self.reg.authenticate(bad))

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.reg.create_agent("Bad Name", "trusted")
        with self.assertRaises(ValueError):
            self.reg.create_agent("ok-name", "emperor")
        self.reg.create_agent("dupe", "trusted")
        with self.assertRaises(ValueError):
            self.reg.create_agent("dupe", "trusted")
        with self.assertRaises(ValueError):
            self.reg.create_agent("admin", "trusted")        # reserved

    def test_revoke_and_lane_change_persist(self):
        t1 = self.reg.create_agent("a1", "probation")
        t2 = self.reg.create_agent("a2", "probation")
        self.reg.set_lane("a1", "trusted")
        self.reg.revoke("a2")
        again = Registry(self.path, self.admin_path)
        self.assertEqual(again.authenticate(t1).lane, "trusted")
        self.assertIsNone(again.authenticate(t2))

    def test_list_never_exposes_tokens_or_hashes(self):
        self.reg.create_agent("a1", "trusted")
        listing = json.dumps(self.reg.list_agents())
        self.assertIn("a1", listing)
        self.assertNotIn("hash", listing)
        self.assertNotIn("token", listing)

    def test_agents_created_elsewhere_are_seen_without_a_restart(self):
        other = Registry(self.path, self.admin_path)               # e.g. the server, already running
        tok = self.reg.create_agent("late", "trusted")             # e.g. the command line
        self.assertEqual(other.authenticate(tok).name, "late")
        self.reg.revoke("late")
        self.assertIsNone(other.authenticate(tok))

    def test_lanes_are_the_known_set(self):
        self.assertEqual(set(LANES), {"trusted", "probation", "readonly"})


class Admin(Base):
    def test_admin_token_created_once(self):
        first = self.reg.ensure_admin()
        self.assertTrue(first)
        self.assertIsNone(self.reg.ensure_admin())
        a = self.reg.authenticate(first)
        self.assertEqual((a.name, a.lane), ("admin", "admin"))
        self.assertEqual(stat.S_IMODE(self.admin_path.stat().st_mode), 0o600)
        self.assertNotIn(first, self.admin_path.read_text())

    def test_agent_token_is_not_admin(self):
        self.reg.ensure_admin()
        tok = self.reg.create_agent("a1", "trusted")
        self.assertNotEqual(self.reg.authenticate(tok).lane, "admin")


class Limiter(unittest.TestCase):
    def test_window_slides(self):
        t = [0.0]
        rl = RateLimiter(limit=3, window=60, clock=lambda: t[0])
        self.assertEqual([rl.allow("a") for _ in range(4)], [True, True, True, False])
        self.assertTrue(rl.allow("b"))
        t[0] = 61
        self.assertTrue(rl.allow("a"))

    def test_denied_attempts_do_not_extend_the_block(self):
        t = [0.0]
        rl = RateLimiter(limit=1, window=10, clock=lambda: t[0])
        self.assertTrue(rl.allow("a"))
        for i in range(5):
            t[0] = 1 + i
            self.assertFalse(rl.allow("a"))
        t[0] = 10.5
        self.assertTrue(rl.allow("a"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
