"""Localhost HTTP API for the memory service. Standard library only.

Defences, because a local web service is reachable from every web page the owner opens:
  * binds loopback only (refuses anything else unless explicitly allowed)
  * Host header must be 127.0.0.1, localhost or [::1] with this port (DNS-rebinding defence)
  * a request with an Origin header is refused unless it is this server's own origin; no CORS headers are ever sent
  * every route except /v1/health and the page itself needs a bearer token; POSTs must be application/json
  * request bodies are capped; errors never include internals; the access log holds no bodies and no tokens
  * per-agent rate limits (writes per hour, reads per minute)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .registry import RateLimiter
from .tools import tools_for

MAX_BODY = 65536
LOOPBACK = ("127.0.0.1", "::1", "localhost")
UI_PATH = Path(__file__).with_name("ui.html")
CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def make_server(service, registry, host="127.0.0.1", port=8765, access_log=None, allow_non_loopback=False, reads_per_minute=240):
    if host not in LOOPBACK and not allow_non_loopback:
        raise ValueError("refusing to bind a non-loopback address; the memory service is local only")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.svc, httpd.reg = service, registry
    httpd.limit_w = RateLimiter(60, 3600)
    httpd.limit_r = RateLimiter(reads_per_minute, 60)
    httpd.log_path = Path(access_log) if access_log else None
    httpd.log_lock = threading.Lock()
    if httpd.log_path:                                      # the log names agents and paths: keep it private to the owner
        httpd.log_path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(httpd.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))
        os.chmod(httpd.log_path, 0o600)
    return httpd


def _need(body: dict, *keys):
    for k in keys:
        if k not in body or body[k] in (None, ""):
            raise ValueError(f"missing field: {k}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "memsvc"
    sys_version = ""

    def log_message(self, *a):
        pass

    # ---- plumbing
    def _drain(self):
        """Consume (up to a cap) any request body we have not read, so it cannot be parsed as the next request."""
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        left = min(n, 1_000_000)
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)
        if n > 1_000_000:
            self.close_connection = True

    def _send(self, code, obj=None, extra=None, raw=None, ctype="application/json; charset=utf-8"):
        if self.command in ("POST", "PUT", "DELETE") and not getattr(self, "_body_read", False):
            self._drain()
        body = raw if raw is not None else json.dumps(obj).encode("utf-8")
        self._status = code
        self._audit()                                       # log before the client can see the response
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _audit(self):
        if getattr(self, "_audited", False):
            return
        self._audited = True
        p = self.server.log_path
        if not p:
            return
        line = json.dumps({"ts": round(time.time(), 3), "agent": getattr(self, "_agent_name", "-"), "method": self.command, "path": urlsplit(self.path).path,
                           "status": getattr(self, "_status", 0), "ms": round((time.time() - self._t0) * 1000, 1)})
        with self.server.log_lock:
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _guard(self) -> bool:
        port = self.server.server_address[1]
        ok_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if (self.headers.get("Host") or "").lower() not in ok_hosts:
            self._send(403, {"error": "bad host"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self._send(403, {"error": "cross-origin requests are refused"})
            return False
        return True

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return self.headers.get("X-Memory-Token")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self._drain()
            self.close_connection = True
            self._send(413, {"error": "request too large"})
            return None
        self._body_read = True
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw = self.rfile.read(n) if n else b""
        if ctype != "application/json":
            self._send(415, {"error": "send application/json"})
            return None
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "invalid JSON"})
            return None
        if not isinstance(obj, dict):
            self._send(400, {"error": "JSON body must be an object"})
            return None
        return obj

    # ---- routing
    ROUTES = {}

    def _route(self, method):
        self._t0 = time.time()
        self._body_read = False
        self._audited = False
        self._agent_name = "-"
        try:
            if not self._guard():
                return
            path = urlsplit(self.path).path
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if method == "GET" and path == "/v1/health":
                return self._send(200, {"ok": True, "version": __version__})
            if method == "GET" and path == "/":
                html = UI_PATH.read_bytes() if UI_PATH.exists() else b"<!doctype html><title>memory</title><p>memory service</p>"
                return self._send(200, raw=html, ctype="text/html; charset=utf-8", extra={"Content-Security-Policy": CSP})
            entry = self.ROUTES.get(path)
            if entry is None:
                return self._send(404, {"error": "not found"})
            if method not in entry:
                return self._send(405, {"error": "method not allowed"})
            fn, admin, write = entry[method]
            agent = self.server.reg.authenticate(self._token())
            if agent is None:
                return self._send(401, {"error": "unauthorized"})
            self._agent_name = agent.name
            if admin and agent.lane != "admin":
                return self._send(403, {"error": "admin only"})
            limiter, cap, retry = (self.server.limit_w, agent.writes_per_hour, "3600") if write else (self.server.limit_r, None, "60")
            if not limiter.allow(agent.name, cap):
                return self._send(429, {"error": "rate limit"}, extra={"Retry-After": retry})
            body = self._body() if method == "POST" else None
            if method == "POST" and body is None:
                return
            try:
                code, obj = fn(self.server.svc, self.server.reg, agent, body or {}, q)
            except PermissionError as e:
                code, obj = 403, {"error": str(e) or "forbidden"}
            except KeyError:
                code, obj = 404, {"error": "not found"}
            except ValueError as e:
                code, obj = 400, {"error": str(e)}
            except Exception:
                code, obj = 500, {"error": "internal error"}
            self._send(code, obj)
        finally:
            self._audit()

    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_OPTIONS(self): self._route("OPTIONS")
    def do_PUT(self): self._route("PUT")
    def do_DELETE(self): self._route("DELETE")


# ---------------------------------------------------------------- endpoint functions: (svc, reg, agent, body, query) -> (code, obj)
def _alias(b: dict, **names) -> dict:
    """Accept claude-mem's argument spellings (dateStart, platformSource, type, ...) next to ours, so a prompt written for either works."""
    out = dict(b)
    for canon, olds in names.items():
        for old in olds:
            if old in out and canon not in out:
                out[canon] = out[old]
    return out


def _add(svc, reg, agent, b, q):
    _need(b, "kind", "title", "body")
    return 200, svc.add(agent, b["kind"], b["title"], b["body"], project=b.get("project"), tags=b.get("tags", ""), ttl_seconds=b.get("ttl_seconds"),
                        scope=b.get("scope", "shared"), force=bool(b.get("force")), session=b.get("session"), work_tokens=b.get("work_tokens"))


def _search(svc, reg, agent, b, q):
    b = _alias(b, kind=("type", "obs_type"), agent=("platformSource", "agent_name"), date_start=("dateStart",), date_end=("dateEnd",), order_by=("orderBy",))
    return 200, svc.search(agent, b.get("query", ""), project=b.get("project") or "*", kind=b.get("kind") or None, agent_name=b.get("agent") or None,
                           date_start=b.get("date_start") or None, date_end=b.get("date_end") or None, limit=b.get("limit", 20),
                           offset=b.get("offset", 0), order_by=b.get("order_by") or None, tag=b.get("tag") or None)   # a web form sends "" for an unset field


def _timeline(svc, reg, agent, b, q):
    b = _alias(b, depth_before=("depthBefore",), depth_after=("depthAfter",))
    return 200, svc.timeline(agent, anchor=b.get("anchor"), query=b.get("query"), depth_before=b.get("depth_before", 3),
                             depth_after=b.get("depth_after", 3), project=b.get("project"), session=b.get("session"))


def _observations(svc, reg, agent, b, q):
    _need(b, "ids")
    b = _alias(b, max_tokens=("maxTokens",))
    return 200, svc.get_observations(agent, b["ids"], max_tokens=b.get("max_tokens"))


def _restore(svc, reg, agent, b, q):
    _need(b, "id")
    return 200, svc.restore(agent, b["id"])


def _context(svc, reg, agent, b, q):
    return 200, svc.context(agent, project=b.get("project", b.get("projects", "*")), budget_tokens=b.get("budget_tokens", 1500), full=bool(b.get("full")))


def _supersede(svc, reg, agent, b, q):
    _need(b, "old_id", "kind", "title", "body")
    return 200, svc.supersede(agent, b["old_id"], b["kind"], b["title"], b["body"], project=b.get("project"))


def _link(svc, reg, agent, b, q):
    _need(b, "src", "dst", "kind")
    return 200, {"linked": svc.link(agent, b["src"], b["dst"], b["kind"])}


def _recall(svc, reg, agent, b, q):
    _need(b, "query")
    return 200, svc.recall(agent, b["query"], project=b.get("project", "*"), limit=b.get("limit", 8), budget_tokens=b.get("budget_tokens"))


def _node(svc, reg, agent, b, q):
    node = svc.get(agent, q.get("id", ""))
    if node is None:
        raise KeyError("note")
    return 200, node


def _focus_get(svc, reg, agent, b, q):
    return 200, {"focus": svc.focus_get(agent, project=q.get("project") or None)}


def _focus_set(svc, reg, agent, b, q):
    _need(b, "text")
    return 200, svc.focus_set(agent, b["text"], project=b.get("project") or None)


def _focus_done(svc, reg, agent, b, q):
    return 200, svc.focus_done(agent, project=b.get("project") or None)


def _agents_create(svc, reg, agent, b, q):
    _need(b, "name", "lane")
    tok = reg.create_agent(b["name"], b["lane"], read=tuple(b.get("read", ["*"])), write=tuple(b.get("write", ["*"])),
                           writes_per_hour=int(b.get("writes_per_hour", 60)))
    return 200, {"name": b["name"], "lane": b["lane"], "token": tok, "note": "shown once; store it now"}


def _agents_lane(svc, reg, agent, b, q):
    _need(b, "name", "lane")
    reg.set_lane(b["name"], b["lane"])
    return 200, {"name": b["name"], "lane": b["lane"]}


def _agents_revoke(svc, reg, agent, b, q):
    _need(b, "name")
    reg.revoke(b["name"])
    return 200, {"name": b["name"], "revoked": True}


Handler.ROUTES = {
    "/v1/add": {"POST": (_add, False, True)},
    "/v1/supersede": {"POST": (_supersede, False, True)},
    "/v1/link": {"POST": (_link, False, True)},
    "/v1/recall": {"POST": (_recall, False, False)},
    "/v1/node": {"GET": (_node, False, False)},
    "/v1/pending": {"GET": (lambda s, r, a, b, q: (200, s.pending(a)), False, False)},
    "/v1/stats": {"GET": (lambda s, r, a, b, q: (200, s.stats(a)), False, False)},
    "/v1/focus": {"GET": (_focus_get, False, False), "POST": (_focus_set, False, True)},
    "/v1/focus/done": {"POST": (_focus_done, False, True)},
    "/v1/tools": {"GET": (lambda s, r, a, b, q: (200, tools_for(q.get("set", "all"))), False, False)},
    "/v1/search": {"POST": (_search, False, False)},
    "/v1/timeline": {"POST": (_timeline, False, False)},
    "/v1/observations": {"POST": (_observations, False, False)},
    "/v1/context": {"POST": (_context, False, False)},
    "/v1/sessions": {"GET": (lambda s, r, a, b, q: (200, s.sessions(a, project=q.get("project", "*"), limit=q.get("limit", 30))), False, False)},
    "/v1/compact-report": {"GET": (lambda s, r, a, b, q: (200, s.compact_report(a, float(q.get("dup", 0.95)), int(q.get("top", 10)))), True, False)},
    "/v1/review": {"GET": (lambda s, r, a, b, q: (200, s.review_list(a)), True, False)},
    "/v1/usage": {"GET": (lambda s, r, a, b, q: (200, s.usage_summary(a, days=q.get("days"))), True, False)},
    "/v1/trash": {"GET": (lambda s, r, a, b, q: (200, s.trash(a, limit=q.get("limit", 100))), True, False)},
    "/v1/restore": {"POST": (_restore, True, False)},
    "/v1/approve": {"POST": (lambda s, r, a, b, q: (200, s.approve(a, b["id"]) if "id" in b else (_ for _ in ()).throw(ValueError("missing field: id"))), True, False)},
    "/v1/reject": {"POST": (lambda s, r, a, b, q: (200, s.reject(a, b["id"], b.get("reason", "")) if "id" in b else (_ for _ in ()).throw(ValueError("missing field: id"))), True, False)},
    "/v1/verify": {"POST": (lambda s, r, a, b, q: (200, s.verify(a, b["id"], b.get("conf", "")) if "id" in b else (_ for _ in ()).throw(ValueError("missing field: id"))), True, False)},
    "/v1/agents": {"GET": (lambda s, r, a, b, q: (200, r.list_agents()), True, False), "POST": (_agents_create, True, False)},
    "/v1/agents/lane": {"POST": (_agents_lane, True, False)},
    "/v1/agents/revoke": {"POST": (_agents_revoke, True, False)},
}
