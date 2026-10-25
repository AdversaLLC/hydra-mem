"""Structural tests for the dashboard page (memsvc/ui.html). Note text is untrusted data, so the page must never turn it into
markup. Run: python3 -I tests/test_ui.py"""
import re
import sys
import tempfile
import threading
import unittest
import http.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from memsvc.registry import Registry  # noqa: E402
from memsvc.server import make_server  # noqa: E402
from memsvc.service import MemoryService  # noqa: E402

HTML = (ROOT / "memsvc" / "ui.html").read_text(encoding="utf-8") if (ROOT / "memsvc" / "ui.html").exists() else ""


class Page(unittest.TestCase):
    def test_the_page_exists_and_has_the_owner_workflows(self):
        for needle in ("Review", "Recall", "Agents", "Compaction", "Approve", "Reject", "/v1/review", "/v1/recall", "/v1/agents", "/v1/compact-report", "/v1/approve"):
            self.assertIn(needle, HTML, needle)

    def test_note_text_is_never_turned_into_markup(self):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, HTML, bad)

    def test_no_external_resources_and_no_remote_scripts(self):
        self.assertEqual(re.findall(r"(?:src|href)\s*=\s*[\"']https?://", HTML), [])
        self.assertEqual(re.findall(r"https?://(?!127\.0\.0\.1|localhost)", HTML.replace("http://www.w3.org", "")), [])

    def test_token_is_kept_in_session_storage_only(self):
        self.assertIn("sessionStorage", HTML)
        self.assertNotIn("localStorage", HTML)
        self.assertNotIn("document.cookie", HTML)

    def test_works_in_light_and_dark(self):
        self.assertIn("prefers-color-scheme", HTML)
        self.assertIn("color-scheme", HTML)


class Served(unittest.TestCase):
    def test_root_serves_the_page_with_a_strict_csp_and_needs_no_token(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            reg = Registry(d / "a.json", d / "adm")
            httpd = make_server(MemoryService(d / "g.db", reg), reg, port=0)
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
                r = c.getresponse()
                body = r.read().decode()
                self.assertEqual(r.status, 200)
                self.assertIn("text/html", r.getheader("Content-Type"))
                csp = r.getheader("Content-Security-Policy")
                self.assertIn("default-src 'none'", csp)
                self.assertIn("connect-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertIn("Review", body)
                c.close()
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
