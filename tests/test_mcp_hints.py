"""Tests for item 1.5: MCP behaviour hints. A client can skip its permission prompt for a tool marked read-only, so the mark is a promise: the read
tools must leave the notes, the links and the audit chain exactly as they were. Run: python3 -I tests/test_mcp_hints.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402
from memsvc.tools import ANNOTATIONS, TOOLS  # noqa: E402

READ = {"memory_recall", "memory_get", "memory_pending", "memory_workflow", "memory_search", "memory_timeline", "memory_get_observations", "memory_context"}
WRITE = {"memory_add", "memory_supersede"}
ALLOWED_KEYS = {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}


class Declared(unittest.TestCase):
    def test_every_tool_has_hints_and_no_stray_entries(self):
        names = {t["function"]["name"] for t in TOOLS}
        self.assertEqual(set(ANNOTATIONS), names)
        self.assertEqual(READ | WRITE, names)

    def test_read_tools_are_read_only_and_write_tools_are_not(self):
        for n in READ:
            self.assertIs(ANNOTATIONS[n]["readOnlyHint"], True, n)
        for n in WRITE:
            self.assertIs(ANNOTATIONS[n]["readOnlyHint"], False, n)
        self.assertIs(ANNOTATIONS["memory_supersede"]["destructiveHint"], True)         # it retires the old note
        self.assertIs(ANNOTATIONS["memory_add"]["destructiveHint"], False)

    def test_everything_is_a_closed_local_world_with_a_title_and_only_known_keys(self):
        for n, a in ANNOTATIONS.items():
            self.assertIs(a["openWorldHint"], False, n)
            self.assertTrue(a.get("title"), n)
            self.assertLessEqual(set(a), ALLOWED_KEYS, n)


class TheBridgeSendsThem(unittest.TestCase):
    def listing(self, env_extra=None):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": "http://127.0.0.1:9", "MEMSVC_TOKEN": "x" * 40}
        e.update(env_extra or {})
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                           capture_output=True, text=True, env=e, timeout=60)
        return json.loads(p.stdout.splitlines()[0])["result"]["tools"]

    def test_tools_list_carries_the_annotations(self):
        tools = {t["name"]: t for t in self.listing()}
        self.assertEqual(set(tools), READ | WRITE)
        for n, t in tools.items():
            self.assertEqual(t["annotations"], ANNOTATIONS[n], n)

    def test_a_narrowed_set_keeps_its_hints(self):
        tools = self.listing({"MEMSVC_TOOLS": "basic"})
        self.assertEqual(len(tools), 5)
        self.assertTrue(all("annotations" in t for t in tools))


class ThePromiseIsTrue(ts.Base):
    """Run every read operation and prove the store did not change."""

    def snapshot(self):
        g = self.svc._g()
        try:
            return (g.db.execute("SELECT count(*), max(updated), max(hash) FROM nodes").fetchone()[:], g.db.execute("SELECT count(*) FROM edges").fetchone()[0],
                    g.db.execute("SELECT count(*), max(seq) FROM audit").fetchone()[:], g._last_chain())
        finally:
            g.db.close()

    def test_no_read_operation_changes_notes_links_or_the_audit_chain(self):
        a = self.svc.add(self.claude, "note", "Hint rule", "read only tools must not change anything in the store at all", project="p", session="s1")["id"]
        self.svc.add(self.qwen, "note", "Pending one", "a pending note from a probation agent waiting for the owner here", project="p")
        before = self.snapshot()
        h = self.svc.handle(a)
        self.svc.recall(self.claude, "read only tools")
        self.svc.get(self.claude, a)
        self.svc.pending(self.qwen)
        self.svc.search(self.claude, "read only tools")
        self.svc.search(self.claude, "")
        self.svc.timeline(self.claude, anchor="#" + h)
        self.svc.get_observations(self.claude, ["#" + h])
        self.svc.context(self.claude, project="p")
        self.svc.sessions(self.claude)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=1)
