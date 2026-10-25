"""Tests for the claude-mem-style functions added to memsvc.service: three-step search (search, timeline, get_observations),
short handles, <private> stripping at write, sessions, session-start context with token economics.
Uses the real hydra_graph on a temporary database. Run: python3 -I tests/test_progressive.py
"""
import hashlib
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_service as ts  # noqa: E402
from memsvc import packer  # noqa: E402
from memsvc.tools import TOOLS, tools_for  # noqa: E402


class Base(ts.Base):
    def setUp(self):
        super().setUp()
        self.gpt = self.agent("gpt", "trusted")

    def put(self, agent, title, body, kind="note", project="p", **kw):
        r = self.svc.add(agent, kind, title, body, project=project, **kw)
        self.assertIn(r["outcome"], ("add", "reference"), r)
        return r["id"]

    def set_ts(self, node_id, stamp):
        g = self.svc._g()
        g.db.execute("UPDATE nodes SET ts=? WHERE id=?", (stamp, node_id))
        g.commit()
        g.db.close()

    def series(self, n=7, project="p", agent=None, words=None):
        """n notes with distinct content and strictly increasing timestamps; returns ids oldest first."""
        agent = agent or self.claude
        words = words or ["apple", "birch", "cobalt", "zucchini", "ember", "fjord", "garnet", "harbor", "iris", "jasper"]
        ids = []
        for i in range(n):
            ids.append(self.put(agent, f"Series {words[i]} note", f"the {words[i]} item number {i} was logged during the garden audit", project=project))
            self.set_ts(ids[-1], f"2026-03-{i + 1:02d}T09:00:00Z")
        return ids

    def hid(self, node_id):
        return hashlib.sha1(node_id.encode()).hexdigest()[:8]


class Search(Base):
    def test_index_is_compact_and_has_no_bodies(self):
        a = self.put(self.claude, "Apricot rule", "apricot trees need pruning in late winter before buds open " * 3)
        res = self.svc.search(self.claude, "apricot pruning")
        it = res["items"][0]
        self.assertEqual(it["id"], a)
        self.assertNotIn("body", it)
        self.assertRegex(it["handle"], r"^[0-9a-f]{8}$")
        self.assertEqual(it["handle"], self.hid(a))
        self.assertLessEqual(len(it["snippet"]), 140)
        self.assertGreaterEqual(it["read_tokens"], 1)
        self.assertEqual(res["total_read_tokens"], sum(i["read_tokens"] for i in res["items"]))
        self.assertTrue(res["sufficient"])
        for key in ("ts", "kind", "conf", "trust", "project", "agent", "title", "tags"):
            self.assertIn(key, it)

    def test_search_sees_exactly_what_recall_sees(self):
        mine = self.put(self.claude, "Plumtree one", "plumtree grafting works best in early spring on dry days")
        pend = self.put(self.qwen, "Plumtree pending", "plumtree pending note from a probation agent waits for the owner")
        priv = self.put(self.gpt, "Plumtree private", "plumtree private scratch note belonging only to gpt today", scope="private")
        exp = self.put(self.claude, "Plumtree scratch", "plumtree scratch note that expires after one minute here", ttl_seconds=60)
        self.assertIn(exp, [i["id"] for i in self.svc.search(self.claude, "plumtree")["items"]])
        self.now[0] += 61
        ids = [i["id"] for i in self.svc.search(self.claude, "plumtree")["items"]]
        self.assertEqual(ids, [mine])
        self.assertEqual([i["id"] for i in self.svc.search(self.ro, "plumtree")["items"]], [mine])
        admin_ids = [i["id"] for i in self.svc.search(self.admin, "plumtree")["items"]]
        self.assertIn(priv, admin_ids)                                        # the owner sees private notes
        self.assertNotIn(pend, admin_ids)                                     # but the index never lists unapproved notes

    def test_project_scope_is_enforced_like_recall(self):
        scoped = self.agent("scoped", "trusted", read=("p1",), write=("p1",))
        self.put(self.claude, "Lemon in p1", "lemon seedlings like bright windows and light watering", project="p1")
        self.put(self.claude, "Lemon in p2", "lemon seedlings in the other project prefer a heated bench", project="p2")
        with self.assertRaises(PermissionError):
            self.svc.search(scoped, "lemon", project="p2")
        self.assertEqual([i["project"] for i in self.svc.search(scoped, "lemon")["items"]], ["p1"])

    def test_filters_kind_agent_and_dates(self):
        d = self.put(self.claude, "Cedar decision", "cedar posts last longer when set in gravel", kind="decision")
        n = self.put(self.claude, "Cedar note", "cedar boards warp when stored flat in the sun")
        x = self.put(self.gpt, "Cedar claim", "cedar smells strong when freshly cut", kind="claim")
        self.set_ts(d, "2026-01-05T10:00:00Z")
        self.set_ts(n, "2026-02-05T10:00:00Z")
        self.set_ts(x, "2026-03-05T10:00:00Z")
        ids = lambda **kw: [i["id"] for i in self.svc.search(self.claude, "cedar", **kw)["items"]]  # noqa: E731
        self.assertEqual(ids(kind="decision"), [d])
        self.assertEqual(ids(agent_name="gpt"), [x])
        self.assertEqual(ids(date_start="2026-02-01", date_end="2026-02-28"), [n])
        self.assertEqual(ids(date_end="2026-01-05"), [d])                    # a bare end date includes that whole day
        self.assertEqual(ids(date_start="2026-03-05T10:00:00Z"), [x])
        with self.assertRaises(ValueError):
            ids(kind="meta_param")
        with self.assertRaises(ValueError):
            ids(date_start="last tuesday")

    def test_empty_query_lists_newest_first_and_pages(self):
        ids = self.series(5)
        res = self.svc.search(self.claude, "", limit=2)
        self.assertEqual([i["id"] for i in res["items"]], [ids[4], ids[3]])
        self.assertEqual((res["total"], res["next_offset"]), (5, 2))
        res = self.svc.search(self.claude, "", limit=2, offset=2)
        self.assertEqual([i["id"] for i in res["items"]], [ids[2], ids[1]])
        res = self.svc.search(self.claude, "", limit=2, offset=4)
        self.assertEqual(([i["id"] for i in res["items"]], res["next_offset"]), ([ids[0]], None))
        asc = self.svc.search(self.claude, "", limit=2, order_by="date_asc")
        self.assertEqual([i["id"] for i in asc["items"]], [ids[0], ids[1]])

    def test_no_evidence_is_said_plainly(self):
        self.put(self.claude, "Fig note", "fig trees fruit twice in warm summers")
        res = self.svc.search(self.claude, "quantum zebra")
        self.assertEqual((res["items"], res["sufficient"], res["total_read_tokens"]), ([], False, 0))

    def test_superseded_notes_are_not_listed(self):
        old = self.put(self.claude, "Pear spray", "pear trees get sprayed with copper in november")
        new = self.svc.supersede(self.claude, old, "note", "Pear spray v2", "pear trees get sprayed with copper in december now")["id"]
        self.assertEqual([i["id"] for i in self.svc.search(self.claude, "pear spray copper")["items"]], [new])

    def crowd(self, n=60):
        """n distinct notes that all match 'marmalade' strongly, so they outrank a longer note that mentions it once."""
        for i in range(n):
            filler = " ".join(f"c{i}x{j}" for j in range(10))
            self.put(self.claude, f"Crowd {i}", f"marmalade {filler} marmalade jars marmalade lids", kind="note")

    def test_filters_are_applied_before_the_candidate_cut(self):
        self.crowd()
        long_tail = " ".join(f"tail{j}" for j in range(60))
        needle = self.put(self.claude, "Needle decision", f"we decided something about marmalade once among much other text {long_tail}", kind="decision")
        gpt_note = self.put(self.gpt, "Needle by gpt", f"gpt noted marmalade once among much other text {long_tail} again", kind="note")
        self.set_ts(needle, "2026-01-05T10:00:00Z")
        self.set_ts(gpt_note, "2026-02-05T10:00:00Z")
        ids = lambda **kw: [i["id"] for i in self.svc.search(self.claude, "marmalade", **kw)["items"]]  # noqa: E731
        self.assertEqual(ids(kind="decision"), [needle])
        self.assertEqual(ids(agent_name="gpt"), [gpt_note])
        self.assertEqual(ids(date_start="2026-02-01", date_end="2026-02-28"), [gpt_note])

    def test_the_owners_index_skips_retired_notes_even_when_a_stale_vector_remains(self):
        a = self.put(self.claude, "Stale vector", "stalevector quince note that the owner will retire shortly")
        g = self.svc._g()
        g.set_status(a, "superseded", actor="test", reason="retired", override=True)
        g.commit()
        g.db.close()
        self.assertIn(a, self.index.docs)                                     # the vector tier still holds it
        self.assertNotIn(a, [i["id"] for i in self.svc.search(self.admin, "stalevector quince")["items"]])
        self.assertEqual(self.svc.timeline(self.admin, anchor=a)["items"], [])

    def test_limit_is_clamped(self):
        self.series(4)
        self.assertEqual(len(self.svc.search(self.claude, "", limit=0)["items"]), 1)
        self.assertLessEqual(len(self.svc.search(self.claude, "", limit=10_000)["items"]), 100)


class Timeline(Base):
    def test_window_around_an_anchor_in_time_order(self):
        ids = self.series(7)
        tl = self.svc.timeline(self.claude, anchor=ids[3], depth_before=2, depth_after=2)
        self.assertEqual([i["id"] for i in tl["items"]], ids[1:6])
        self.assertEqual(tl["anchor"], ids[3])
        self.assertEqual([i["id"] for i in tl["items"] if i["is_anchor"]], [ids[3]])
        self.assertEqual([i["id"] for i in self.svc.timeline(self.claude, anchor=ids[3])["items"]], ids)        # defaults are 3 and 3
        self.assertEqual([i["id"] for i in self.svc.timeline(self.claude, anchor=ids[0], depth_before=2, depth_after=2)["items"]], ids[0:3])
        self.assertEqual(tl["total_read_tokens"], sum(i["read_tokens"] for i in tl["items"]))

    def test_the_anchor_can_be_found_by_query_or_handle(self):
        ids = self.series(7)
        self.assertEqual(self.svc.timeline(self.claude, query="zucchini", depth_before=1, depth_after=1)["anchor"], ids[3])
        h = self.svc.search(self.claude, "zucchini")["items"][0]["handle"]
        self.assertEqual(self.svc.timeline(self.claude, anchor=h)["anchor"], ids[3])
        self.assertEqual(self.svc.timeline(self.claude, anchor="#" + h)["anchor"], ids[3])

    def test_hidden_neighbours_never_appear_and_do_not_count(self):
        ids = self.series(5)
        h1 = self.put(self.qwen, "Hidden pending", "a pending note from a probation agent sits between two others")
        h2 = self.put(self.gpt, "Hidden private", "a private scratch note of another agent sits between two others", scope="private")
        self.set_ts(h1, "2026-03-02T12:00:00Z")
        self.set_ts(h2, "2026-03-03T12:00:00Z")
        tl = self.svc.timeline(self.claude, anchor=ids[2], depth_before=1, depth_after=1)
        self.assertEqual([i["id"] for i in tl["items"]], [ids[1], ids[2], ids[3]])
        self.assertNotIn(h1, [i["id"] for i in self.svc.timeline(self.claude, anchor=ids[1], depth_after=3)["items"]])

    def test_scope_defaults_to_the_anchors_project(self):
        a = self.series(3, project="p")
        b = self.put(self.claude, "Other project note", "an unrelated note that lives in the other project q", project="q")
        self.set_ts(b, "2026-03-02T10:00:00Z")
        tl = self.svc.timeline(self.claude, anchor=a[1])
        self.assertEqual({i["project"] for i in tl["items"]}, {"p"})
        self.assertIn(b, [i["id"] for i in self.svc.timeline(self.claude, anchor=a[1], project="*")["items"]])
        scoped = self.agent("scoped", "trusted", read=("p",), write=("p",))
        with self.assertRaises(PermissionError):
            self.svc.timeline(scoped, anchor=a[1], project="q")

    def test_unknown_and_hidden_anchors_look_identical(self):
        hidden = self.put(self.qwen, "Hidden anchor", "a pending note whose id must not be probed by other agents")
        self.series(2)
        r1 = self.svc.timeline(self.claude, anchor="note/2026-01-01/nope")
        r2 = self.svc.timeline(self.claude, anchor=hidden)
        self.assertEqual(r1, r2)
        self.assertEqual((r1["anchor"], r1["items"]), (None, []))
        self.assertEqual(self.svc.timeline(self.claude, query="quantum zebra")["items"], [])

    def test_same_second_notes_keep_insertion_order(self):
        ids = [self.put(self.claude, f"Quick {w}", f"quick burst note about {w} written in the same second") for w in ("alpha", "bravo", "charlie")]
        tl = self.svc.timeline(self.claude, anchor=ids[1], depth_before=1, depth_after=1)
        self.assertEqual([i["id"] for i in tl["items"]], ids)

    def test_a_session_timeline_lists_that_sessions_notes_in_order(self):
        a = self.put(self.claude, "Sess one a", "first fact of the first session about hazelnut grafting", session="s1")
        self.put(self.claude, "Other", "a note outside any named session about walnut husks", session="s2")
        b = self.put(self.claude, "Sess one b", "second fact of the first session about hazelnut pruning", session="s1")
        tl = self.svc.timeline(self.claude, session="s1")
        self.assertEqual([i["id"] for i in tl["items"]], [a, b])
        self.assertEqual([i["id"] for i in self.svc.timeline(self.gpt, session="claude:s1")["items"]], [a, b])


    def test_an_agents_default_group_has_its_own_session_form(self):
        loose = self.put(self.claude, "Loose note", "a note written without any session id about gooseberry canes")
        named = self.put(self.claude, "Named note", "a note written inside a session about gooseberry mulching", session="s1")
        self.assertEqual([i["id"] for i in self.svc.timeline(self.claude, session="claude:")["items"]], [loose])
        self.assertEqual([i["id"] for i in self.svc.timeline(self.claude, session="s1")["items"]], [named])
        labels = {r["session"] for r in self.svc.sessions(self.claude)}
        self.assertIn("claude", labels)                                       # the default group is listed under the bare agent name
        with self.assertRaises(ValueError):
            self.svc.timeline(self.claude, session="a:b:c")


class GetObservations(Base):
    def test_batch_returns_full_text_and_names_the_missing(self):
        a = self.put(self.claude, "Full one", "the full body of the first note is here for reading in detail", tags="x,y")
        b = self.put(self.claude, "Full two", "the full body of the second note is also here for reading")
        res = self.svc.get_observations(self.claude, [a, "note/2026-01-01/ghost", b])
        self.assertEqual([i["id"] for i in res["items"]], [a, b])
        self.assertIn("full body of the first note", res["items"][0]["body"])
        self.assertEqual(res["missing"], ["note/2026-01-01/ghost"])
        self.assertEqual(res["total_read_tokens"], sum(i["read_tokens"] for i in res["items"]))
        for key in ("handle", "kind", "conf", "trust", "project", "tags", "ts", "agent", "session", "work_tokens"):
            self.assertIn(key, res["items"][0])

    def test_hidden_notes_are_reported_exactly_like_absent_ones(self):
        pend = self.put(self.qwen, "Pending", "a pending note that only its author and the owner may read")
        res = self.svc.get_observations(self.claude, [pend])
        self.assertEqual((res["items"], res["missing"]), ([], [pend]))
        self.assertEqual([i["id"] for i in self.svc.get_observations(self.qwen, [pend])["items"]], [pend])     # authors see their own pending note
        self.assertEqual([i["id"] for i in self.svc.get_observations(self.admin, [pend])["items"]], [pend])

    def test_batches_are_capped(self):
        with self.assertRaises(ValueError):
            self.svc.get_observations(self.claude, [f"n{i}" for i in range(21)])
        with self.assertRaises(ValueError):
            self.svc.get_observations(self.claude, [])
        with self.assertRaises(ValueError):
            self.svc.get_observations(self.claude, "not-a-list")

    def test_a_token_budget_stops_the_batch_and_says_so(self):
        ids = [self.put(self.claude, f"Long {i}", ("long note about quince jelly step %d " % i) * 25) for i in range(4)]
        one = self.svc.get_observations(self.claude, ids[:1])["items"][0]["read_tokens"]
        res = self.svc.get_observations(self.claude, ids, max_tokens=one * 2 + 1)
        self.assertEqual(len(res["items"]), 2)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["skipped"], ids[2:])
        self.assertLessEqual(res["total_read_tokens"], one * 2 + 1)

    def test_handles_resolve_and_unknown_handles_are_missing(self):
        a = self.put(self.claude, "Handled", "this note is fetched by its short handle instead of its long id")
        res = self.svc.get_observations(self.claude, ["#" + self.hid(a), "deadbeef"])
        self.assertEqual([i["id"] for i in res["items"]], [a])
        self.assertEqual(res["missing"], ["deadbeef"])


class PrivateTags(Base):
    def test_private_text_never_reaches_storage_search_or_vectors(self):
        r = self.svc.add(self.claude, "note", "Rotation", "the gate code is <private>hunter2-secret</private> and rotates monthly", project="p")
        self.assertEqual(r["outcome"], "add")
        self.assertEqual(r["private_removed"], 1)
        got = self.svc.get(self.claude, r["id"])
        self.assertNotIn("hunter2", got["body"] + got["title"])
        self.assertIn("rotates monthly", got["body"])
        g = self.svc._g()
        try:
            self.assertEqual(list(g.fts("hunter2", None, 10)), [])
        finally:
            g.db.close()
        self.assertFalse(any("hunter2" in w for words in self.index.docs.values() for w in words))
        self.assertEqual(self.svc.search(self.claude, "hunter2")["items"], [])

    def test_title_and_tags_are_stripped_too(self):
        r = self.svc.add(self.claude, "note", "Plan <private>codename-zeta</private> notes", "the plan covers three phases of the rollout", tags="rollout,<private>zeta</private>", project="p")
        got = self.svc.get(self.claude, r["id"])
        self.assertNotIn("zeta", got["title"] + got["tags"] + got["body"])

    def test_a_note_that_is_all_private_is_rejected_without_echo(self):
        with self.assertRaises(ValueError) as cm:
            self.svc.add(self.claude, "note", "Hidden", "<private>nothing but the secret-xyz</private>", project="p")
        self.assertNotIn("secret-xyz", str(cm.exception))
        self.assertIn("private", str(cm.exception).lower())

    def test_an_unclosed_private_tag_fails_closed(self):
        r = self.svc.add(self.claude, "note", "Open tag", "safe fact about composting. <private>secret tail that never closes", project="p")
        self.assertTrue(r["private_unclosed"])
        got = self.svc.get(self.claude, r["id"])
        self.assertEqual(got["body"], "safe fact about composting.")

    def test_supersede_strips_as_well(self):
        old = self.put(self.claude, "Old rule", "the old rule about drip lines is simple and short")
        r = self.svc.supersede(self.claude, old, "note", "New rule", "the new rule is <private>gate-9999</private> to use timers on drip lines")
        self.assertNotIn("9999", self.svc.get(self.claude, r["id"])["body"])

    def test_plain_notes_report_no_private_removal(self):
        r = self.svc.add(self.claude, "note", "Plain", "an ordinary note with nothing hidden in it at all", project="p")
        self.assertNotIn("private_removed", r)


class Sessions(Base):
    def test_session_ids_group_notes_and_list(self):
        self.put(self.claude, "S1 first", "first note of session one about pomegranate seeds", session="s1")
        self.put(self.claude, "S1 second", "second note of session one about pomegranate juice", session="s1")
        self.put(self.claude, "S2 only", "single note of session two about persimmon ripening", session="s2")
        self.put(self.claude, "S1 summary", "session one summary: seeds and juice were both reviewed", tags="session-summary", session="s1")
        self.put(self.qwen, "Pending in s1", "a pending note in a session of the probation agent about kumquats", session="s1")
        rows = {r["session"]: r for r in self.svc.sessions(self.claude)}
        self.assertEqual((rows["claude:s1"]["count"], rows["claude:s2"]["count"]), (3, 1))
        self.assertNotIn("qwen:s1", rows)                                     # pending notes are not listed for other agents
        self.assertEqual(rows["claude:s1"]["agent"], "claude")
        self.assertEqual(rows["claude:s1"]["summary"], "S1 summary")
        self.assertLessEqual(rows["claude:s1"]["first_ts"], rows["claude:s1"]["last_ts"])

    def test_bad_session_ids_are_refused(self):
        for bad in ("bad id!", "x" * 65, "../etc", "a:b"):
            with self.assertRaises(ValueError, msg=bad):
                self.svc.add(self.claude, "note", "T", "a body long enough to be a proper note here", session=bad)

    def test_sessions_respect_project_scope(self):
        scoped = self.agent("scoped", "trusted", read=("p1",), write=("p1",))
        self.put(self.claude, "In p2", "a note in project two within a named session about chestnuts", project="p2", session="sx")
        self.assertEqual([r for r in self.svc.sessions(scoped) if r["session"] == "claude:sx"], [])


class Context(Base):
    def test_summary_first_then_corrections_then_the_rest_inside_the_budget(self):
        self.put(self.claude, "Mulberry note", "mulberry jam sets faster with lemon juice added late")
        self.put(self.claude, "Mulberry correction", "mulberry jam needs fruit pectin, lemon alone is not enough", kind="correction")
        self.put(self.claude, "Session 1 summary", "mulberry jam work: the recipe was fixed and one correction logged", tags="session-summary", session="s1")
        ctx = self.svc.context(self.claude, project="p", budget_tokens=400)
        text = ctx["context"]
        self.assertTrue(text.startswith(packer.HEADER))
        self.assertLess(text.index("Session 1 summary"), text.index("Mulberry correction"))
        self.assertLess(text.index("Mulberry correction"), text.index("Mulberry note"))
        self.assertLessEqual(ctx["used_tokens"], 400)
        self.assertEqual(ctx["used_tokens"], packer.est_tokens(text))
        self.assertTrue(ctx["sufficient"])
        tiny = self.svc.context(self.claude, project="p", budget_tokens=60)
        self.assertLessEqual(tiny["used_tokens"], 60)

    def test_project_chains_and_scope(self):
        self.put(self.claude, "In p1", "walnut trees drop husks in october onto the path", project="p1")
        self.put(self.claude, "In p2", "walnut shells go to the compost heap in the second yard", project="p2")
        self.put(self.claude, "In p3", "walnut leaves are collected for the third yard mulch", project="p3")
        text = self.svc.context(self.claude, project=["p1", "p2"], budget_tokens=500)["context"]
        self.assertIn("In p1", text)
        self.assertIn("In p2", text)
        self.assertNotIn("In p3", text)
        scoped = self.agent("scoped", "trusted", read=("p1",), write=("p1",))
        with self.assertRaises(PermissionError):
            self.svc.context(scoped, project=["p1", "p2"])

    def test_hidden_notes_never_enter_the_context(self):
        self.put(self.qwen, "Pending ctx", "a pending note that must stay out of every injected context block")
        self.put(self.gpt, "Private ctx", "a private note of another agent that must stay out of the block", scope="private")
        ctx = self.svc.context(self.claude, project="p", budget_tokens=500)
        self.assertEqual((ctx["context"], ctx["items"], ctx["sufficient"]), ("", [], False))

    def test_economics_use_agent_reported_work_and_say_so(self):
        self.put(self.claude, "Costly find", "the migration needed three failed attempts before the lock was found", work_tokens=2000)
        self.put(self.claude, "Cheap find", "the config key is named in the second paragraph of the guide", work_tokens=1000)
        self.put(self.claude, "Unmeasured", "a note whose original effort was never reported by its author")
        ctx = self.svc.context(self.claude, project="p", budget_tokens=600)
        e = ctx["economics"]
        self.assertEqual(e["work_tokens"], 3000)
        self.assertEqual(e["read_tokens"], ctx["used_tokens"])
        self.assertTrue(e["agent_reported"])
        self.assertEqual(e["notes"], len(ctx["items"]))
        self.assertTrue(0 <= e["saved_percent"] <= 100)
        self.assertEqual(e["saved_tokens"], max(0, 3000 - e["read_tokens"]))
        self.assertIn("economics", ctx["context"])
        self.assertLessEqual(ctx["used_tokens"], 600)

    def test_no_reported_work_means_no_savings_claim(self):
        self.put(self.claude, "Plain", "an ordinary note with no reported work attached to it at all")
        ctx = self.svc.context(self.claude, project="p", budget_tokens=300)
        self.assertEqual((ctx["economics"]["work_tokens"], ctx["economics"]["saved_percent"]), (0, 0))
        self.assertNotIn("economics", ctx["context"])

    def test_work_tokens_are_validated(self):
        for bad in (-5, "abc", True, 10 ** 9, 1.5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.svc.add(self.claude, "note", "T", "a body long enough to be a proper note here", work_tokens=bad)


class Handles(Base):
    def test_handle_is_stable_and_eight_hex_characters(self):
        a = self.put(self.claude, "Stable", "a note whose handle must be the same every time it is asked for")
        self.assertEqual(self.svc.handle(a), self.hid(a))
        self.assertRegex(self.svc.handle(a), r"^[0-9a-f]{8}$")


class Tools(Base):
    def test_progressive_tools_exist_with_short_descriptions(self):
        by = {t["function"]["name"]: t["function"] for t in TOOLS}
        for name in ("memory_workflow", "memory_search", "memory_timeline", "memory_get_observations", "memory_context"):
            self.assertIn(name, by)
            self.assertLessEqual(len(by[name]["description"]), 450, name)
        self.assertEqual(by["memory_get_observations"]["parameters"]["required"], ["ids"])
        self.assertIn("session", by["memory_add"]["parameters"]["properties"])
        self.assertIn("work_tokens", by["memory_add"]["parameters"]["properties"])
        self.assertIn("memory_timeline", by["memory_search"]["description"])

    def test_tool_sets_let_small_models_see_fewer_tools(self):
        names = lambda s: [t["function"]["name"] for t in tools_for(s)]  # noqa: E731
        self.assertEqual(names("basic"), ["memory_recall", "memory_add", "memory_supersede", "memory_get", "memory_pending"])
        prog = names("progressive")
        self.assertEqual(prog[:5], ["memory_workflow", "memory_search", "memory_timeline", "memory_get_observations", "memory_context"])
        self.assertIn("memory_add", prog)
        self.assertEqual(names("all"), [t["function"]["name"] for t in TOOLS])
        with self.assertRaises(ValueError):
            tools_for("nope")


if __name__ == "__main__":
    unittest.main(verbosity=1)
