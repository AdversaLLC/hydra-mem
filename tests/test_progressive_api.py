"""Tests for the claude-mem-style functions over HTTP, the Python client, the MCP bridge and the owner's CLI, against a real server.
Run: python3 -I tests/test_progressive_api.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from memsvc.client import ApiError, MemoryClient  # noqa: E402
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.reg = Registry(d / "agents.json", d / "admin.token")
        cls.admin = cls.reg.ensure_admin()
        cls.claude = cls.reg.create_agent("claude", "trusted")
        cls.qwen = cls.reg.create_agent("qwen", "probation")
        cls.viewer = cls.reg.create_agent("viewer", "readonly")
        cls.scoped = cls.reg.create_agent("scoped", "trusted", read=("other",), write=("other",))
        cls.svc = MemoryService(d / "graph.db", cls.reg)
        cls.httpd = make_server(cls.svc, cls.reg, port=0)
        cls.port = cls.httpd.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        c = MemoryClient(cls.url, cls.claude)
        cls.n1 = c.add("decision", "Lychee grafting", "lychee grafting is done by air layering in late spring", project="api", session="s1")["id"]
        cls.n2 = c.add("note", "Lychee watering", "lychee watering stays light in winter and heavy in fruit set", project="api", session="s1")["id"]
        cls.n3 = c.add("note", "Mango pruning", "mango pruning follows harvest and keeps the canopy open", project="api")["id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def c(self, tok=None):
        return MemoryClient(self.url, tok or self.claude)

    def handle(self, text="lychee grafting"):
        return self.c().search(text)["items"][0]["handle"]


class Http(Live):
    def test_search_round_trip_has_no_bodies(self):
        r = self.c().search("lychee")
        self.assertTrue(r["sufficient"])
        self.assertTrue({self.n1, self.n2} <= {i["id"] for i in r["items"]})
        self.assertTrue(all("body" not in i for i in r["items"]))

    def test_every_new_route_needs_a_token(self):
        bad = MemoryClient(self.url, "x" * 40)
        for fn in (lambda: bad.search("a"), lambda: bad.timeline(anchor="a"), lambda: bad.get_observations(["a"]), lambda: bad.context(), lambda: bad.sessions()):
            with self.assertRaises(ApiError) as e:
                fn()
            self.assertEqual(e.exception.status, 401)

    def test_a_readonly_agent_can_read_through_all_three_steps(self):
        v = self.c(self.viewer)
        h = v.search("lychee grafting")["items"][0]["handle"]
        self.assertTrue(v.timeline(anchor=h)["items"])
        self.assertTrue(v.get_observations([h])["items"])

    def test_claude_mem_style_argument_names_are_accepted(self):
        body = {"query": "lychee", "type": "decision", "platformSource": "claude", "dateStart": "2000-01-01", "dateEnd": "2999-01-01", "orderBy": "date_desc"}
        r = self.c()._req("POST", "/v1/search", body)
        self.assertEqual([i["id"] for i in r["items"]], [self.n1])
        t = self.c()._req("POST", "/v1/timeline", {"anchor": self.n1, "depthBefore": 0, "depthAfter": 1})
        self.assertEqual(len(t["items"]), 2)

    def test_a_web_form_may_send_empty_strings_for_unset_filters(self):
        r = self.c()._req("POST", "/v1/search", {"query": "", "project": "", "kind": "", "agent": "", "date_start": "", "date_end": "", "order_by": "", "limit": 5, "offset": 0})
        self.assertTrue(r["items"])

    def test_bad_input_is_a_400_and_never_a_500(self):
        c = self.c()
        for path, body in (("/v1/search", {"query": "a", "type": "meta_param"}), ("/v1/search", {"query": "a", "dateStart": "someday"}),
                           ("/v1/observations", {"ids": "x"}), ("/v1/observations", {"ids": [f"n{i}" for i in range(25)]}), ("/v1/observations", {}),
                           ("/v1/search", {"query": "a", "limit": "many"}), ("/v1/context", {"budget_tokens": "lots"})):
            with self.assertRaises(ApiError, msg=f"{path} {body}") as e:
                c._req("POST", path, body)
            self.assertEqual(e.exception.status, 400, f"{path} {body}")

    def test_scope_errors_are_403(self):
        with self.assertRaises(ApiError) as e:
            self.c(self.scoped).search("lychee", project="api")
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(ApiError) as e:
            self.c(self.scoped).context(project=["other", "api"])
        self.assertEqual(e.exception.status, 403)

    def test_timeline_observations_context_sessions(self):
        c = self.c()
        tl = c.timeline(anchor=self.n1, depth_before=1, depth_after=1)
        self.assertEqual(tl["anchor"], self.n1)
        obs = c.get_observations([self.n1, "note/none"])
        self.assertEqual([i["id"] for i in obs["items"]], [self.n1])
        self.assertEqual(obs["missing"], ["note/none"])
        ctx = c.context(project="api", budget_tokens=300)
        self.assertTrue(ctx["context"].startswith("[memory"))
        self.assertIn("economics", ctx)
        self.assertEqual({s["session"] for s in c.sessions(project="api") if s["session"] != "claude"} & {"claude:s1"}, {"claude:s1"})

    def test_tool_sets_over_http(self):
        c = self.c()
        self.assertEqual(len(c.tools("basic")), 5)
        self.assertEqual(c.tools("progressive")[0]["function"]["name"], "memory_workflow")
        self.assertGreaterEqual(len(c.tools()), 10)
        with self.assertRaises(ApiError) as e:
            c._req("GET", "/v1/tools?set=nope")
        self.assertEqual(e.exception.status, 400)

    def test_private_text_is_removed_over_http_and_reported(self):
        r = self.c().add("note", "Gate log", "the gate code is <private>8841-secret</private> and changes monthly", project="api")
        self.assertEqual(r["private_removed"], 1)
        self.assertEqual(self.c().search("8841")["items"], [])
        self.assertNotIn("8841", self.c().get_observations([r["id"]])["items"][0]["body"])

    def test_session_and_work_tokens_are_accepted_by_add(self):
        r = self.c().add("note", "Tracked work", "a measured note about hawthorn hedging and its costs", project="api", session="s9", work_tokens=1200)
        item = self.c().get_observations([r["id"]])["items"][0]
        self.assertEqual((item["session"], item["work_tokens"]), ("claude:s9", 1200))


class Owner(Live):
    def test_trash_and_restore_over_http_are_owner_only(self):
        a = self.c().add("note", "Trash me", "a note about rambutan seedlings that is rejected and then restored over http", project="api")["id"]
        adm = self.c(self.admin)
        adm.reject(a)
        for fn in (lambda: self.c().trash(), lambda: self.c().restore(a), lambda: self.c(self.viewer).trash()):
            with self.assertRaises(ApiError) as e:
                fn()
            self.assertEqual(e.exception.status, 403)
        self.assertIn(a, [i["id"] for i in adm.trash()])
        self.assertEqual(self.c().search("rambutan seedlings")["items"], [])
        self.assertEqual(adm.restore(a), {"id": a, "status": "active"})
        self.assertEqual([i["id"] for i in self.c().search("rambutan seedlings")["items"]], [a])

    def test_restore_errors_are_400_or_404(self):
        adm = self.c(self.admin)
        with self.assertRaises(ApiError) as e:
            adm.restore("note/2026-01-01/none")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ApiError) as e:
            adm.restore(self.n1)                                              # an active note
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ApiError) as e:
            adm._req("POST", "/v1/restore", {})
        self.assertEqual(e.exception.status, 400)


class McpProject(Live):
    """MEMSVC_PROJECT makes the bridge default to one project (claude-mem's per-project isolation), opt-in and always overridable."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other = MemoryClient(cls.url, cls.claude).add("note", "Marmalade rule", "marmalade needs seville oranges and a slow boil for the set", project="zzz")["id"]

    def run_bridge(self, calls, project="api"):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude}
        if project is not None:
            e["MEMSVC_PROJECT"] = project
        msgs = [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls)]
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, env=e, timeout=60)
        return {m["id"]: m["result"]["content"][0]["text"] for m in (json.loads(l) for l in p.stdout.splitlines() if l.strip())}

    def test_reads_default_to_the_project_and_star_widens_them(self):
        out = self.run_bridge([("memory_search", {"query": "marmalade"}), ("memory_search", {"query": "marmalade", "project": "*"}),
                               ("memory_recall", {"query": "marmalade seville"}), ("memory_recall", {"query": "marmalade seville", "project": "zzz"})])
        self.assertIn("insufficient evidence", out[1].lower())
        self.assertIn("Marmalade rule", out[2])
        self.assertIn("insufficient evidence", out[3].lower())
        self.assertIn("Marmalade rule", out[4])

    def test_context_and_add_default_to_the_project(self):
        out = self.run_bridge([("memory_context", {"budget_tokens": 400}),
                               ("memory_add", {"kind": "note", "title": "Defaulted", "body": "a note about kumquat jam that names no project at all and so takes the default"})])
        self.assertIn("Lychee", out[1])
        self.assertNotIn("Marmalade", out[1])
        nid = out[2].rsplit("(", 1)[1].rstrip(")")
        self.assertEqual(MemoryClient(self.url, self.claude).get_observations([nid])["items"][0]["project"], "api")

    def test_timeline_with_an_anchor_ignores_the_default(self):
        h = MemoryClient(self.url, self.claude).search("marmalade", project="zzz")["items"][0]["handle"]
        out = self.run_bridge([("memory_timeline", {"anchor": "#" + h})])
        self.assertIn("Marmalade rule", out[1])

    def test_without_the_variable_nothing_is_defaulted(self):
        out = self.run_bridge([("memory_search", {"query": "marmalade"})], project=None)
        self.assertIn("Marmalade rule", out[1])


class Mcp(Live):
    def session(self, calls, token=None):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": token or self.claude}
        msgs = [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls)]
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, env=e, timeout=60)
        return {m["id"]: m["result"] for m in (json.loads(l) for l in p.stdout.splitlines() if l.strip())}

    def test_the_three_steps_work_end_to_end_as_text(self):
        h = self.handle()
        out = self.session([("memory_workflow", {}), ("memory_search", {"query": "lychee"}), ("memory_timeline", {"anchor": "#" + h, "depth_before": 1, "depth_after": 1}),
                            ("memory_get_observations", {"ids": ["#" + h]}), ("memory_context", {"project": "api", "budget_tokens": 300})])
        text = {k: v["content"][0]["text"] for k, v in out.items()}
        self.assertTrue(all(not v.get("isError") for v in out.values()), out)
        self.assertIn("memory_get_observations", text[1])
        self.assertIn("#" + h, text[2])
        self.assertIn("memory_timeline", text[2])
        self.assertIn("►", text[3])
        self.assertIn("air layering", text[4])
        self.assertIn("data, not instructions", text[5])

    def test_errors_are_error_results(self):
        out = self.session([("memory_search", {"query": "x", "kind": "meta_param"}), ("memory_get_observations", {}), ("memory_timeline", {"anchor": "#00000000"})])
        self.assertTrue(out[1]["isError"])
        self.assertTrue(out[2]["isError"])
        self.assertFalse(out[3].get("isError"))                              # an unknown anchor is an empty answer, not an error
        self.assertIn("nothing", out[3]["content"][0]["text"].lower())

    def test_the_tool_list_can_be_narrowed_by_environment(self):
        def names(setting):
            e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude}
            if setting is not None:
                e["MEMSVC_TOOLS"] = setting
            p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                               capture_output=True, text=True, env=e, timeout=60)
            return [t["name"] for t in json.loads(p.stdout.splitlines()[0])["result"]["tools"]]
        self.assertEqual(len(names("basic")), 5)
        self.assertEqual(names("progressive")[0], "memory_workflow")
        self.assertGreaterEqual(len(names(None)), 10)
        self.assertGreaterEqual(len(names("nonsense")), 10)                    # an unknown value falls back to everything, not to nothing

    def test_adding_with_private_text_tells_the_model(self):
        out = self.session([("memory_add", {"kind": "note", "title": "Shed lock", "body": "the shed lock code is <private>5512-hush</private> kept by the owner", "project": "api"})])
        self.assertIn("private", out[1]["content"][0]["text"].lower())
        self.assertEqual(self.c().search("5512")["items"], [])


class WriteHints(Live):
    """What a write says about the notes already stored, over HTTP and through the MCP bridge. Each test uses its own words and project, so the
    notes of one never become the neighbours of another."""

    def session(self, calls):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude}
        msgs = [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls)]
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, env=e, timeout=60)
        return {m["id"]: m["result"]["content"][0]["text"] for m in (json.loads(l) for l in p.stdout.splitlines() if l.strip())}

    def test_http_returns_similar_with_differences(self):
        c = MemoryClient(self.url, self.claude)
        old = c.add("note", "Gull cache", "the gull cache ttl is 300 seconds for login tokens", project="hints-http")["id"]
        r = c.add("note", "Gull cache", "the gull cache ttl is 600 seconds for login tokens", project="hints-http")
        self.assertEqual(r["outcome"], "add")
        self.assertEqual(r["similar"][0]["id"], old)
        self.assertEqual(r["similar"][0]["differs"], {"numbers": {"new": ["600"], "old": ["300"]}})

    def test_the_bridge_prints_the_neighbour_and_the_way_to_replace_it(self):
        old = MemoryClient(self.url, self.claude).add("note", "Tern cache", "the tern cache ttl is 300 seconds for login tokens", project="hints-bridge")["id"]
        out = self.session([("memory_add", {"kind": "note", "title": "Tern cache", "body": "the tern cache ttl is 900 seconds for login tokens", "project": "hints-bridge"})])
        text = out[1]
        self.assertTrue(text.startswith("saved ("))
        self.assertIn("#" + MemoryService.handle(old), text)
        self.assertIn("900", text)
        self.assertIn("memory_supersede", text)

    def test_supersede_over_http_accepts_the_handle_and_a_hidden_handle_is_a_404(self):
        c = MemoryClient(self.url, self.claude)
        old = c.add("note", "Skua cache", "the skua cache ttl is 300 seconds for login tokens", project="hints-super")["id"]
        hidden = c.add("note", "Hidden", "a private note about walrus colonies and their winter ice platforms", project="hints-super", scope="private")["id"]
        with self.assertRaises(ApiError) as e:
            MemoryClient(self.url, self.qwen).supersede("#" + MemoryService.handle(hidden), "note", "Hidden", "walrus colonies use ice platforms in winter", project="hints-super")
        self.assertEqual(e.exception.status, 404)
        r = c.supersede("#" + MemoryService.handle(old), "correction", "Skua cache", "the skua cache ttl is 120 seconds for login tokens and nothing else", project="hints-super")
        self.assertEqual(r["outcome"], "add")


class Cli(Live):
    def run_cli(self, *args, token=None, home=None, env=None):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": token or self.claude,
             "MEMSVC_HOME": str(home or Path(self.tmp.name) / "cli-home")}
        e.update(env or {})
        return subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), *args], capture_output=True, text=True, env=e, timeout=60)

    def test_search_timeline_get_context_sessions(self):
        h = self.handle()
        s = self.run_cli("search", "lychee")
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertIn("#" + h, s.stdout)
        t = self.run_cli("timeline", "--anchor", h, "--before", "1", "--after", "1")
        self.assertIn("►", t.stdout)
        g = self.run_cli("get", h)
        self.assertIn("air layering", g.stdout)
        c = self.run_cli("context", "--project", "api", "--budget", "300")
        self.assertIn("[memory", c.stdout)
        ss = self.run_cli("sessions")
        self.assertIn("claude:s1", ss.stdout)

    def test_trash_and_restore(self):
        a = MemoryClient(self.url, self.claude).add("note", "CLI trash", "a note about longan pruning that is rejected and restored from the command line", project="api")["id"]
        MemoryClient(self.url, self.admin).reject(a)
        listed = self.run_cli("trash", token=self.admin)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(a, listed.stdout)
        back = self.run_cli("restore", a, token=self.admin)
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertIn("active", back.stdout)
        self.assertNotEqual(self.run_cli("trash", token=self.claude).returncode, 0)                     # owner only

    def test_workflow_needs_no_server(self):
        out = self.run_cli("workflow", env={"MEMSVC_URL": "http://127.0.0.1:9"})
        self.assertEqual(out.returncode, 0)
        self.assertIn("memory_search", out.stdout)

    def test_errors_exit_non_zero(self):
        self.assertNotEqual(self.run_cli("search", "x", "--kind", "meta_param").returncode, 0)

    def test_doctor_passes_on_a_healthy_home_and_never_prints_secrets(self):
        home = Path(self.tmp.name) / "doc-ok"
        init = self.run_cli("init", home=home)
        tok = [w for w in init.stdout.split() if len(w) >= 40][0]
        db = home / "graph.db"
        MemoryService(db, Registry(home / "agents.json", home / "admin.token"))
        out = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", home=home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("FAIL", out.stdout)
        self.assertNotIn(tok, out.stdout + out.stderr)
        self.assertIn("audit chain", out.stdout)
        self.assertIn("admin.token", out.stdout)

    def test_doctor_is_read_only(self):
        home = Path(self.tmp.name) / "doc-ro"
        self.run_cli("init", home=home)
        snap = lambda: sorted((p.name, p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode)) for p in home.iterdir())  # noqa: E731
        before = snap()
        out = self.run_cli("doctor", "--db", str(Path(self.tmp.name) / "absent" / "g.db"), "--url", "http://127.0.0.1:9", home=home)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual(snap(), before)
        self.assertFalse((Path(self.tmp.name) / "absent").exists())            # it did not create the db's folder either

    def test_doctor_reports_a_missing_audit_key_instead_of_creating_one(self):
        home = Path(self.tmp.name) / "doc-key"
        self.run_cli("init", home=home)
        db = home / "g.db"
        MemoryService(db, Registry(home / "agents.json", home / "admin.token"))
        (home / "g.key").rename(home / "g.key.moved")
        out = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", home=home)
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, r"FAIL.*audit key")
        self.assertFalse((home / "g.key").exists())

    def test_doctor_fails_on_loose_permissions(self):
        home = Path(self.tmp.name) / "doc-bad"
        self.run_cli("init", home=home)
        os.chmod(home / "admin.token", 0o644)
        out = self.run_cli("doctor", "--db", str(home / "none.db"), "--url", "http://127.0.0.1:9", home=home)
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, r"FAIL.*admin\.token")
        self.assertEqual(stat.S_IMODE((home / "admin.token").stat().st_mode), 0o644)       # doctor reports, it does not silently chmod

    # ---- item 1.7: doctor and the vector tier
    class _Idx:
        backend = "fake"

        def upsert(self, docs): pass
        def delete(self, ids): pass
        def query(self, text, project, limit): return []

    def doctor_home(self, name, index=None, notes=2):
        home = Path(self.tmp.name) / name
        self.run_cli("init", home=home)
        reg = Registry(home / "agents.json", home / "admin.token")
        db = home / "graph.db"
        svc = MemoryService(db, reg, index=index)
        w = reg.authenticate(reg.create_agent("writer", "trusted"))
        ids = [svc.add(w, "note", f"Doctor note {i}", f"a distinct fact number {i} about doctortopic{i} and nothing else", project="p")["id"] for i in range(notes)]
        return home, db, ids

    def test_doctor_lists_active_notes_that_have_no_vector(self):
        home, db, ids = self.doctor_home("doc-novec")
        out = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", "--chroma", str(Path(self.tmp.name) / "nochroma"), home=home)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertRegex(out.stdout, r"WARN\s+vectors: 2 of 2 active notes have no vector")
        for i in ids:
            self.assertIn(i, out.stdout)
        self.assertIn("Doctor note 0", out.stdout)
        self.assertIn("hydra_graph.py embed", out.stdout)

    def test_doctor_caps_the_list_of_notes_without_vectors(self):
        home, db, ids = self.doctor_home("doc-many", notes=14)
        out = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", "--chroma", str(Path(self.tmp.name) / "nochroma"), home=home)
        self.assertEqual(sum(1 for i in ids if i in out.stdout), 10)
        self.assertIn("and 4 more", out.stdout)

    def test_doctor_is_quiet_when_every_note_has_a_vector(self):
        home, db, ids = self.doctor_home("doc-allvec", index=self._Idx())
        out = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", "--chroma", str(Path(self.tmp.name) / "nochroma"), home=home)
        self.assertRegex(out.stdout, r"ok\s+vectors: every one of the 2 active notes has a vector")
        self.assertNotIn("WARN  vectors", out.stdout)

    def test_doctor_warns_when_vectors_come_from_two_embedders(self):
        home, db, ids = self.doctor_home("doc-mixed", index=self._Idx())
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("UPDATE vectors SET backend='ollama' WHERE node_id=?", (ids[0],))
        con.commit()
        con.close()
        out = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", "--chroma", str(Path(self.tmp.name) / "nochroma"), home=home)
        self.assertRegex(out.stdout, r"WARN\s+vectors: .*more than one embedder")
        self.assertIn("fake: 1", out.stdout)
        self.assertIn("ollama: 1", out.stdout)

    def test_doctor_reports_the_pinned_embedder_and_fails_on_a_broken_pin(self):
        home, db, ids = self.doctor_home("doc-pin", index=self._Idx())
        chroma = Path(self.tmp.name) / "doc-pin-chroma"
        chroma.mkdir()
        absent = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", "--chroma", str(chroma), home=home)
        self.assertRegex(absent.stdout, r"WARN\s+embedder fingerprint: not pinned yet")
        from memsvc import fingerprint
        fingerprint.save(chroma / fingerprint.FILE_NAME, fingerprint.record([0.1] * 384, "onnx", "2026-10-22T12:00:00Z"))
        good = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", "--chroma", str(chroma), home=home)
        self.assertRegex(good.stdout, r"ok\s+embedder fingerprint: pinned to onnx, 384 dimensions, on 2026-10-22")
        (chroma / fingerprint.FILE_NAME).write_text("garbage")
        bad = self.run_cli("doctor", "--db", str(db), "--url", "http://127.0.0.1:9", "--chroma", str(chroma), home=home)
        self.assertEqual(bad.returncode, 1)
        self.assertRegex(bad.stdout, r"FAIL\s+embedder fingerprint")
        self.assertEqual((chroma / fingerprint.FILE_NAME).read_text(), "garbage")          # doctor reports, it never repairs or re-pins


if __name__ == "__main__":
    unittest.main(verbosity=1)
