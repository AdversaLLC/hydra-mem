"""Tests for the owner's trash and restore (claude-mem's "Smart Trash" idea: nothing is lost, a mistake can be undone).
A rejected note is kept with its audit trail; the owner can list rejected notes and bring one back. Run: python3 -I tests/test_trash.py
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402


class Trash(ts.Base):
    def put(self, agent, title, body, kind="note", project="p", **kw):
        r = self.svc.add(agent, kind, title, body, project=project, **kw)
        self.assertIn(r["outcome"], ("add", "reference"), r)
        return r["id"]

    def test_a_rejected_note_is_listed_and_can_be_restored(self):
        a = self.put(self.claude, "Rejected by mistake", "a useful note about espalier pruning that the owner rejected by mistake")
        self.svc.reject(self.admin, a, "oops")
        self.assertEqual(self.svc.search(self.claude, "espalier pruning")["items"], [])
        listed = self.svc.trash(self.admin)
        self.assertEqual([i["id"] for i in listed], [a])
        self.assertEqual(listed[0]["title"], "Rejected by mistake")
        self.assertIn("rejected_at", listed[0])
        self.assertEqual(self.svc.restore(self.admin, a), {"id": a, "status": "active"})
        self.assertEqual([i["id"] for i in self.svc.search(self.claude, "espalier pruning")["items"]], [a])
        self.assertIn(a, self.index.docs)                                     # it is searchable by meaning again, not only by words
        self.assertEqual(self.svc.trash(self.admin), [])
        self.assertTrue(self.svc.stats(self.admin)["chain_ok"])              # the audit chain records both steps and is still sound

    def test_only_the_owner_can_list_or_restore(self):
        a = self.put(self.claude, "Owner only", "a note about trellis wire that only the owner may bring back")
        self.svc.reject(self.admin, a)
        for agent in (self.claude, self.qwen, self.ro):
            with self.assertRaises(PermissionError):
                self.svc.trash(agent)
            with self.assertRaises(PermissionError):
                self.svc.restore(agent, a)

    def test_only_rejected_notes_can_be_restored(self):
        live = self.put(self.claude, "Still live", "a note about espaliered pears that was never rejected at all")
        old = self.put(self.claude, "Will be replaced", "a note about the old pear spray schedule that gets replaced")
        self.svc.supersede(self.claude, old, "note", "Replacement", "a note about the new pear spray schedule that replaces the old one")
        with self.assertRaises(ValueError):
            self.svc.restore(self.admin, live)
        with self.assertRaises(ValueError):
            self.svc.restore(self.admin, old)                                 # superseded notes are replaced, not restored
        with self.assertRaises(KeyError):
            self.svc.restore(self.admin, "note/2026-01-01/none")

    def test_restoring_a_rejected_replacement_applies_the_replacement(self):
        old = self.put(self.claude, "Old rule", "the old rule about hedge trimming says trim twice a year in spring and autumn")
        new = self.svc.supersede(self.qwen, old, "note", "New rule", "the new rule about hedge trimming says trim once a year in late summer")["id"]
        self.svc.reject(self.admin, new)
        self.assertIn(old, [i["id"] for i in self.svc.search(self.claude, "hedge trimming")["items"]])
        self.svc.restore(self.admin, new)
        ids = [i["id"] for i in self.svc.search(self.claude, "hedge trimming")["items"]]
        self.assertEqual(ids, [new])                                          # the owner's restore does what approve would have done

    def test_newest_rejection_first_and_limited(self):
        ids = [self.put(self.claude, f"Reject {w}", f"a note about {w} that will be rejected in order one after another") for w in ("alder", "beech", "cherry")]
        for i in ids:
            self.svc.reject(self.admin, i)
        self.assertEqual([x["id"] for x in self.svc.trash(self.admin)], ids[::-1])
        self.assertEqual(len(self.svc.trash(self.admin, limit=2)), 2)

    def test_a_restored_note_keeps_its_author_and_session(self):
        a = self.put(self.claude, "Keeps author", "a note about loquat grafting that records who wrote it and when", session="s7")
        self.svc.reject(self.admin, a)
        self.svc.restore(self.admin, a)
        got = self.svc.get_observations(self.claude, [a])["items"][0]
        self.assertEqual((got["agent"], got["session"]), ("claude", "claude:s7"))

    def test_restoring_does_not_raise_anyones_trust(self):
        trusted = self.put(self.claude, "Trusted stays", "a note about kiwi vines from a trusted agent that is rejected and restored")
        self.svc.reject(self.admin, trusted)
        self.svc.restore(self.admin, trusted)
        got = self.svc.get(self.claude, trusted)
        self.assertEqual(got["trust"], "agent")
        self.assertTrue(got["source"].startswith("svc-trusted:"))
        pending = self.put(self.qwen, "Vetted by restore", "a note about passionfruit vines from a probation agent that the owner rejects then restores")
        self.svc.reject(self.admin, pending)
        self.svc.restore(self.admin, pending)
        self.assertEqual(self.svc.get(self.claude, pending)["trust"], "user")             # restoring is the owner vouching for it

    def test_a_note_approved_earlier_and_rejected_later_is_still_in_the_trash(self):
        a = self.put(self.qwen, "Approved then rejected", "a note about guava cuttings from a probation agent that is approved and later rejected")
        self.svc.approve(self.admin, a)
        self.svc.reject(self.admin, a)
        self.assertEqual([i["id"] for i in self.svc.trash(self.admin)], [a])
        self.svc.restore(self.admin, a)
        self.assertEqual(self.svc.get(self.claude, a)["status"], "active")


if __name__ == "__main__":
    unittest.main(verbosity=1)
