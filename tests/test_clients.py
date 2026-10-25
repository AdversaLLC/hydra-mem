"""Tests for the Python client, the owner's CLI and the MCP bridge, against a real server. Run: python3 -I tests/test_clients.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
        cls.httpd = make_server(MemoryService(d / "graph.db", cls.reg), cls.reg, port=0)
        cls.port = cls.httpd.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()


class Client(Live):
    def test_round_trip_and_admin_flow(self):
        c, q, a = MemoryClient(self.url, self.claude), MemoryClient(self.url, self.qwen), MemoryClient(self.url, self.admin)
        r = c.add("note", "Raisin rule", "raisin soaking needs warm water for an hour", project="p")
        self.assertEqual(r["outcome"], "add")
        got = q.recall("raisin soaking warm water", budget_tokens=150)
        self.assertEqual([i["id"] for i in got["items"]], [r["id"]])
        self.assertTrue(got["context"])
        pend = q.add("note", "Date rule", "date palms need deep watering twice a month", project="p")
        self.assertEqual([x["id"] for x in q.pending()], [pend["id"]])
        self.assertEqual([x["id"] for x in a.review()], [pend["id"]])
        a.approve(pend["id"])
        self.assertIn("Date rule", [i["title"] for i in c.recall("date palms watering")["items"]])

    def test_errors_carry_status_and_message(self):
        c = MemoryClient(self.url, self.claude)
        with self.assertRaises(ApiError) as e:
            c.add("meta_param", "t", "body text")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ApiError) as e:
            MemoryClient(self.url, "x" * 40).stats()
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(ApiError) as e:
            c.review()
        self.assertEqual(e.exception.status, 403)

    def test_token_comes_from_the_environment_by_default(self):
        os.environ["MEMSVC_TOKEN"] = self.claude
        try:
            st = MemoryClient(self.url).stats()
            self.assertTrue(st["chain_ok"])
            self.assertNotIn("agents", st)                    # an agent's stats do not say who else is registered
        finally:
            del os.environ["MEMSVC_TOKEN"]


class Cli(Live):
    def run_cli(self, *args, env=None, home=None):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_HOME": str(home or Path(self.tmp.name) / "cli-home")}
        e.update(env or {})
        return subprocess.run([sys.executable, "-I", str(ROOT / "memctl"), *args], capture_output=True, text=True, env=e, timeout=60)

    def test_init_prints_the_admin_token_once(self):
        home = Path(self.tmp.name) / "fresh"
        first = self.run_cli("init", home=home)
        self.assertEqual(first.returncode, 0, first.stderr)
        tok = [w for w in first.stdout.split() if len(w) >= 40][0]
        second = self.run_cli("init", home=home)
        self.assertNotIn(tok, second.stdout)
        self.assertIn("already", second.stdout.lower())

    def test_agent_management_without_a_server(self):
        home = Path(self.tmp.name) / "mgmt"
        self.run_cli("init", home=home)
        out = self.run_cli("agent", "create", "local-qwen", "--lane", "probation", "--write", "p1", home=home)
        self.assertEqual(out.returncode, 0, out.stderr)
        tok = [w for w in out.stdout.split() if len(w) >= 40][0]
        lst = self.run_cli("agent", "list", home=home).stdout
        self.assertIn("local-qwen", lst)
        self.assertNotIn(tok, lst)
        bad = self.run_cli("agent", "create", "Bad Name", "--lane", "trusted", home=home)
        self.assertNotEqual(bad.returncode, 0)

    def test_review_and_recall_over_http(self):
        MemoryClient(self.url, self.qwen).add("note", "Quince CLI", "quince jam needs equal sugar by weight", project="p")
        env = {"MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.admin}
        out = self.run_cli("review", env=env)
        self.assertIn("Quince CLI", out.stdout)
        rid = [x["id"] for x in MemoryClient(self.url, self.admin).review() if x["title"] == "Quince CLI"][0]
        self.assertEqual(self.run_cli("approve", rid, env=env).returncode, 0)
        rec = self.run_cli("recall", "quince jam sugar", env={"MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude})
        self.assertIn("Quince CLI", rec.stdout)

    def test_serve_starts_on_loopback_and_answers_health(self):
        home = Path(self.tmp.name) / "serve"
        self.run_cli("init", home=home)
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_HOME": str(home)}
        p = subprocess.Popen([sys.executable, "-I", str(ROOT / "memctl"), "serve", "--port", "0", "--no-vectors", "--db", str(home / "g.db")],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=e)
        try:
            line = p.stdout.readline()
            url = [w for w in line.split() if w.startswith("http://127.0.0.1:")][0]
            self.assertEqual(MemoryClient(url, "x" * 40).health()["ok"], True)
        finally:
            p.terminate()
            p.wait(timeout=10)
            p.stdout.close()


class Mcp(Live):
    def session(self, messages, env=None):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude}
        e.update(env or {})
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input="\n".join(json.dumps(m) for m in messages) + "\n",
                           capture_output=True, text=True, env=e, timeout=60)
        return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]

    def test_handshake_tools_and_calls(self):
        out = self.session([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_add", "arguments": {"kind": "note", "title": "Mint MCP", "body": "mint tea steeps for four minutes at ninety degrees", "project": "p"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "memory_recall", "arguments": {"query": "mint tea steeps"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        ])
        by = {m["id"]: m for m in out if "id" in m}
        self.assertEqual(by[1]["result"]["serverInfo"]["name"], "memsvc")
        names = {t["name"] for t in by[2]["result"]["tools"]}
        self.assertTrue({"memory_recall", "memory_add", "memory_supersede", "memory_get", "memory_pending"} <= names)
        self.assertFalse(by[3]["result"].get("isError"))
        self.assertIn("Mint MCP", by[4]["result"]["content"][0]["text"])
        self.assertTrue(by[5]["result"]["isError"])
        self.assertEqual(len([m for m in out if "id" not in m]), 0)           # notifications get no reply

    def test_unreachable_server_is_an_error_result_not_a_crash(self):
        out = self.session([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_recall", "arguments": {"query": "x"}}}],
                           env={"MEMSVC_URL": "http://127.0.0.1:9"})
        self.assertTrue(out[0]["result"]["isError"])

    def test_garbage_lines_do_not_kill_the_bridge(self):
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "MEMSVC_URL": self.url, "MEMSVC_TOKEN": self.claude}
        p = subprocess.run([sys.executable, "-I", str(ROOT / "memsvc" / "mcp_bridge.py")], input='not json\n{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n',
                           capture_output=True, text=True, env=e, timeout=60)
        replies = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertTrue(any(r.get("id") == 9 for r in replies))


if __name__ == "__main__":
    unittest.main(verbosity=1)
