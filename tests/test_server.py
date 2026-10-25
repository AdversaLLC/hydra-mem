"""Tests for memsvc.server: the localhost HTTP API and its web-facing defences. Run: python3 -I tests/test_server.py"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.reg = Registry(d / "agents.json", d / "admin.token")
        cls.admin_tok = cls.reg.ensure_admin()
        cls.claude = cls.reg.create_agent("claude", "trusted")
        cls.qwen = cls.reg.create_agent("qwen", "probation")
        cls.viewer = cls.reg.create_agent("viewer", "readonly")
        cls.slow = cls.reg.create_agent("slow", "trusted", writes_per_hour=2)
        cls.log = d / "access.jsonl"
        svc = MemoryService(d / "graph.db", cls.reg)
        cls.httpd = make_server(svc, cls.reg, port=0, access_log=cls.log)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, token=None, headers=None, raw=None, ctype="application/json"):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = ctype
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        txt = r.read().decode("utf-8", "replace")
        try:
            obj = json.loads(txt)
        except ValueError:
            obj = txt
        out = (r.status, obj, dict((k.lower(), v) for k, v in r.getheaders()))
        c.close()
        return out


class Access(Base):
    def test_health_is_open_and_minimal(self):
        s, o, _ = self.req("GET", "/v1/health")
        self.assertEqual(s, 200)
        self.assertEqual(set(o), {"ok", "version"})

    def test_auth_required_everywhere_else(self):
        for method, path in (("GET", "/v1/stats"), ("POST", "/v1/recall"), ("POST", "/v1/add"), ("GET", "/v1/pending"), ("GET", "/v1/review")):
            self.assertEqual(self.req(method, path, {} if method == "POST" else None)[0], 401, path)
            self.assertEqual(self.req(method, path, {} if method == "POST" else None, token="x" * 40)[0], 401, path)
        self.assertEqual(self.req("GET", "/v1/stats", token=self.claude)[0], 200)

    def test_non_loopback_bind_is_refused(self):
        with self.assertRaises(ValueError):
            make_server(None, self.reg, host="0.0.0.0", port=0)

    def test_dns_rebinding_host_is_refused(self):
        for host in ("evil.example", "127.0.0.1.evil.example", f"evil.example:{self.port}", ""):
            s, _, _ = self.req("GET", "/v1/health", headers={"Host": host})
            self.assertEqual(s, 403, host)
        self.assertEqual(self.req("GET", "/v1/health", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_cross_origin_requests_are_refused(self):
        s, _, _ = self.req("POST", "/v1/recall", {"query": "x"}, token=self.claude, headers={"Origin": "https://evil.example"})
        self.assertEqual(s, 403)
        s, _, _ = self.req("POST", "/v1/recall", {"query": "x"}, token=self.claude, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(s, 200)

    def test_no_cors_headers_and_options_is_refused(self):
        s, _, h = self.req("GET", "/v1/health")
        self.assertFalse([k for k in h if k.startswith("access-control")])
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(self.req("OPTIONS", "/v1/recall")[0], 405)

    def test_post_needs_json_content_type_and_sane_body(self):
        self.assertEqual(self.req("POST", "/v1/recall", raw=b"query=x", token=self.claude, ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.req("POST", "/v1/recall", raw=b"{not json", token=self.claude)[0], 400)
        self.assertEqual(self.req("POST", "/v1/recall", raw=b"[1,2]", token=self.claude)[0], 400)
        self.assertEqual(self.req("POST", "/v1/add", raw=b"x" * 70000, token=self.claude)[0], 413)

    def test_unknown_route_and_wrong_method(self):
        self.assertEqual(self.req("GET", "/v1/nope", token=self.claude)[0], 404)
        self.assertEqual(self.req("GET", "/v1/recall", token=self.claude)[0], 405)


class Flow(Base):
    def test_lanes_end_to_end(self):
        s, o, _ = self.req("POST", "/v1/add", {"kind": "note", "title": "Cherry rule", "body": "cherry tree watering happens every second morning", "project": "p"}, token=self.claude)
        self.assertEqual((s, o["outcome"]), (200, "add"))
        s, o, _ = self.req("POST", "/v1/add", {"kind": "note", "title": "Plum rule", "body": "plum tree watering happens every third morning", "project": "p"}, token=self.qwen)
        self.assertEqual((s, o["outcome"]), (200, "reference"))
        plum = o["id"]
        s, got, _ = self.req("POST", "/v1/recall", {"query": "tree watering morning", "project": "*", "budget_tokens": 200}, token=self.qwen)
        self.assertEqual(s, 200)
        titles = [i["title"] for i in got["items"]]
        self.assertIn("Cherry rule", titles)
        self.assertNotIn("Plum rule", titles)
        self.assertTrue(got["context"].startswith("[memory"))
        s, p, _ = self.req("GET", "/v1/pending", token=self.qwen)
        self.assertEqual([x["id"] for x in p], [plum])
        self.assertEqual(self.req("GET", "/v1/review", token=self.claude)[0], 403)
        s, rv, _ = self.req("GET", "/v1/review", token=self.admin_tok)
        self.assertIn(plum, [x["id"] for x in rv])
        self.assertEqual(self.req("POST", "/v1/approve", {"id": plum}, token=self.admin_tok)[0], 200)
        s, got, _ = self.req("POST", "/v1/recall", {"query": "plum watering", "project": "*"}, token=self.claude)
        self.assertIn("Plum rule", [i["title"] for i in got["items"]])

    def test_error_mapping(self):
        self.assertEqual(self.req("POST", "/v1/add", {"kind": "note", "title": "t", "body": "body text"}, token=self.viewer)[0], 403)
        self.assertEqual(self.req("POST", "/v1/add", {"kind": "meta_param", "title": "t", "body": "body text"}, token=self.claude)[0], 400)
        self.assertEqual(self.req("GET", "/v1/node?id=note/2026-01-01/none", token=self.claude)[0], 404)
        self.assertEqual(self.req("POST", "/v1/recall", {}, token=self.claude)[0], 400)

    def test_write_rate_limit_returns_429_with_retry_after(self):
        for i in range(2):
            self.assertEqual(self.req("POST", "/v1/add", {"kind": "note", "title": f"Rate {i}", "body": f"rate limit probe number {i} about apricots"}, token=self.slow)[0], 200)
        s, o, h = self.req("POST", "/v1/add", {"kind": "note", "title": "Rate 3", "body": "rate limit probe number three about figs"}, token=self.slow)
        self.assertEqual(s, 429)
        self.assertIn("retry-after", h)


class Admin(Base):
    def test_agent_management_is_admin_only_and_token_shown_once(self):
        self.assertEqual(self.req("POST", "/v1/agents", {"name": "newbie", "lane": "probation"}, token=self.claude)[0], 403)
        s, o, _ = self.req("POST", "/v1/agents", {"name": "newbie", "lane": "probation"}, token=self.admin_tok)
        self.assertEqual(s, 200)
        tok = o["token"]
        self.assertEqual(self.req("GET", "/v1/stats", token=tok)[0], 200)
        s, lst, _ = self.req("GET", "/v1/agents", token=self.admin_tok)
        self.assertNotIn(tok, json.dumps(lst))
        self.assertEqual(self.req("POST", "/v1/agents/revoke", {"name": "newbie"}, token=self.admin_tok)[0], 200)
        self.assertEqual(self.req("GET", "/v1/stats", token=tok)[0], 401)
        self.assertEqual(self.req("POST", "/v1/agents", {"name": "Bad Name", "lane": "trusted"}, token=self.admin_tok)[0], 400)


class Compact(Base):
    def test_compact_report_is_admin_only_and_needs_vectors(self):
        self.assertEqual(self.req("GET", "/v1/compact-report", token=self.claude)[0], 403)
        self.assertEqual(self.req("GET", "/v1/compact-report", token=self.admin_tok)[0], 400)      # this server has no vector tier


class Tools(Base):
    def test_function_schemas_for_local_model_frameworks(self):
        s, o, _ = self.req("GET", "/v1/tools", token=self.claude)
        self.assertEqual(s, 200)
        names = {t["function"]["name"] for t in o}
        self.assertTrue({"memory_recall", "memory_add", "memory_supersede"} <= names)
        for t in o:
            self.assertEqual(t["type"], "function")
            self.assertIn("parameters", t["function"])


class Logging(Base):
    def test_access_log_is_private_to_the_owner(self):
        import stat
        self.req("GET", "/v1/health")
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode) & 0o077, 0)

    def test_access_log_has_no_secrets_or_bodies(self):
        marker = "unmistakable-body-marker-pomelo"
        self.req("POST", "/v1/recall", {"query": marker}, token=self.claude)
        text = self.log.read_text()
        self.assertIn("/v1/recall", text)
        self.assertNotIn(marker, text)
        self.assertNotIn(self.claude, text)
        self.assertIn("claude", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
