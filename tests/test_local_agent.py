"""Tests for examples/local_agent.py: action parsing and tool dispatch (no model needed). Run: python3 -I tests/test_local_agent.py"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))
import local_agent as L  # noqa: E402


class FakeClient:
    def __init__(self, sufficient=True):
        self.calls, self.sufficient = [], sufficient

    def recall(self, query, project="*", limit=8, budget_tokens=None):
        self.calls.append(("recall", query, budget_tokens))
        return {"sufficient": self.sufficient, "context": "[memory: ...]\n- [n/1] (note, conf U, user) Vault: it lives on the big drive"}

    def add(self, kind, title, body, **kw):
        self.calls.append(("add", kind, title, body))
        return {"id": "note/2026-01-01/x", "outcome": "reference"}


class Parse(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(L.parse_action('{"tool": "memory_recall", "args": {"query": "x"}}'), {"tool": "memory_recall", "args": {"query": "x"}})

    def test_fenced_and_chatty_json(self):
        self.assertEqual(L.parse_action('Sure!\n```json\n{"answer": "hi"}\n```\nthanks'), {"answer": "hi"})

    def test_garbage_and_non_objects_are_none(self):
        for s in ("", "no json here", "[1,2]", '{"unclosed": ', "null"):
            self.assertIsNone(L.parse_action(s), s)

    def test_an_action_needs_a_tool_or_an_answer(self):
        self.assertIsNone(L.parse_action('{"hello": "world"}'))


class Dispatch(unittest.TestCase):
    def test_recall_returns_the_packed_context(self):
        c = FakeClient()
        out = L.run_tool(c, "memory_recall", {"query": "where is the vault"})
        self.assertIn("big drive", out)
        self.assertEqual(c.calls[0][2], L.RECALL_BUDGET)

    def test_insufficient_evidence_is_spelled_out_for_the_model(self):
        out = L.run_tool(FakeClient(sufficient=False), "memory_recall", {"query": "capital of australia"})
        self.assertIn("NO RELEVANT NOTES", out)

    def test_add_reports_waiting_for_approval(self):
        out = L.run_tool(FakeClient(), "memory_add", {"kind": "note", "title": "Backup time", "body": "nightly at 03:00"})
        self.assertIn("waiting", out.lower())

    def test_a_refused_duplicate_that_differs_is_reported_without_a_tool_the_agent_does_not_have(self):
        class Differs(FakeClient):
            def add(self, *a, **k):
                return {"id": "note/2026-01-01/x", "handle": "abcd1234", "outcome": "duplicate", "differs": {"numbers": {"new": ["5"], "old": ["3"]}}}
        out = L.run_tool(Differs(), "memory_add", {"kind": "note", "title": "Retries", "body": "it retries 5 times"})
        self.assertIn("differs", out)
        self.assertIn("numbers", out)
        self.assertNotIn("memory_supersede", out)                            # this agent has only recall and add
        self.assertIn("do not add it again", out)

    def test_unknown_tool_and_missing_args_are_errors_the_model_can_read(self):
        self.assertIn("unknown tool", L.run_tool(FakeClient(), "rm_rf", {}))
        self.assertIn("missing", L.run_tool(FakeClient(), "memory_recall", {}).lower())


if __name__ == "__main__":
    unittest.main(verbosity=1)
