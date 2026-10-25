"""Tests for one-click sign-in to the viewer (`memctl ui`): the owner's token is kept in ui.token (mode 600), a stale or missing one is
replaced with a fresh owner token while the old admin.token is moved aside (never deleted), and the token is never printed.
Run: python3 -I tests/test_signin.py
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from memsvc import cli  # noqa: E402
from memsvc.registry import Registry  # noqa: E402

HTML = (ROOT / "memsvc" / "ui.html").read_text(encoding="utf-8")


class Token(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def reg(self):
        return Registry(self.h / "agents.json", self.h / "admin.token")

    def test_first_use_makes_an_owner_token_kept_private(self):
        tok = cli.ui_token(self.h)
        self.assertEqual(self.reg().authenticate(tok).lane, "admin")
        self.assertEqual((self.h / "ui.token").read_text(encoding="utf-8").strip(), tok)
        self.assertEqual(stat.S_IMODE((self.h / "ui.token").stat().st_mode), 0o600)

    def test_the_same_token_is_reused_while_it_works(self):
        first = cli.ui_token(self.h)
        before = (self.h / "admin.token").read_text(encoding="utf-8")
        self.assertEqual(cli.ui_token(self.h), first)
        self.assertEqual((self.h / "admin.token").read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(p.name for p in self.h.iterdir()), ["admin.token", "ui.token"])

    def test_a_lost_owner_token_is_replaced_and_the_old_file_is_kept(self):
        lost = self.reg().ensure_admin()                       # made at `memctl init`, shown once, never saved
        old = (self.h / "admin.token").read_text(encoding="utf-8")
        tok = cli.ui_token(self.h)
        self.assertNotEqual(tok, lost)
        self.assertIsNone(self.reg().authenticate(lost))
        self.assertEqual(self.reg().authenticate(tok).lane, "admin")
        kept = [p for p in self.h.iterdir() if p.name.startswith("admin.token.replaced-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding="utf-8"), old)

    def test_a_stale_saved_token_is_replaced(self):
        cli.ui_token(self.h)
        (self.h / "ui.token").write_text("x" * 43 + "\n", encoding="utf-8")       # no longer matches admin.token
        tok = cli.ui_token(self.h)
        self.assertEqual(self.reg().authenticate(tok).lane, "admin")
        self.assertEqual(stat.S_IMODE((self.h / "ui.token").stat().st_mode), 0o600)

    def test_an_agent_token_in_the_file_is_not_treated_as_the_owner(self):
        agent_tok = self.reg().create_agent("viewer", "readonly")
        (self.h / "ui.token").write_text(agent_tok, encoding="utf-8")
        tok = cli.ui_token(self.h)
        self.assertNotEqual(tok, agent_tok)
        self.assertEqual(self.reg().authenticate(tok).lane, "admin")


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Path(self.tmp.name) / "memsvc"
        self.env = dict(os.environ, MEMSVC_HOME=str(self.h))

    def tearDown(self):
        self.tmp.cleanup()

    def test_memctl_ui_never_prints_the_token(self):
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), "ui", "--no-open", "--port", "1"], capture_output=True, text=True,
                           env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        tok = (self.h / "ui.token").read_text(encoding="utf-8").strip()
        self.assertNotIn(tok, p.stdout + p.stderr)
        self.assertIn("signed in", p.stdout)
        self.assertIn("not running", p.stdout + p.stderr)            # nothing listens on port 1: say how to start it


class Page(unittest.TestCase):
    def test_the_page_takes_a_sign_in_link_once_and_wipes_it(self):
        self.assertIn("location.hash", HTML)
        self.assertIn("history.replaceState", HTML)
        start = HTML.index("location.hash")
        self.assertLess(start, HTML.index('tok.value = store.get("memtok")'))    # read before the token box is filled
        self.assertRegex(HTML[start:start + 600], r'store\.set\("memtok"')

    def test_the_link_token_never_reaches_the_server(self):
        self.assertIsNone(re.search(r"[?&]token=", HTML))                       # only the fragment (#token=), which browsers never send


if __name__ == "__main__":
    unittest.main()
