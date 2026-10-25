"""Python client for the memory service. Standard library only; drop this one file into any agent project.

    from memsvc.client import MemoryClient
    m = MemoryClient()                     # MEMSVC_URL and MEMSVC_TOKEN from the environment
    hits = m.recall("how do I restart the indexer", budget_tokens=800)
    if hits["sufficient"]: prompt += hits["context"]      # else: say you do not know
    m.add("decision", "Indexer restart", "restart with the service unit, not by hand", project="infra")
"""
import json
import os
import urllib.error
import urllib.request


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


class MemoryClient:
    def __init__(self, url=None, token=None, timeout: float = 30.0):
        self.url = (url or os.environ.get("MEMSVC_URL") or "http://127.0.0.1:8765").rstrip("/")
        self.token = token if token is not None else os.environ.get("MEMSVC_TOKEN", "")
        self.timeout = timeout

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", "")
            except (ValueError, OSError):
                msg = ""
            finally:
                e.close()
            raise ApiError(e.code, msg or e.reason)
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(0, f"cannot reach the memory service at {self.url}: {getattr(e, 'reason', e)}")

    def post(self, path, body): return self._req("POST", path, body)          # for callers that already hold a request body (the MCP bridge)

    # ---- everyone
    def health(self): return self._req("GET", "/v1/health")
    def stats(self): return self._req("GET", "/v1/stats")
    def tools(self, set=None): return self._req("GET", "/v1/tools" + (f"?set={urllib.request.quote(set, safe='')}" if set else ""))
    def pending(self): return self._req("GET", "/v1/pending")

    def recall(self, query, project="*", limit=8, budget_tokens=None):
        b = {"query": query, "project": project, "limit": limit}
        if budget_tokens:
            b["budget_tokens"] = budget_tokens
        return self._req("POST", "/v1/recall", b)

    def add(self, kind, title, body, project=None, tags="", ttl_seconds=None, scope="shared", session=None, work_tokens=None):
        b = {"kind": kind, "title": title, "body": body, "tags": tags, "scope": scope}
        if project:
            b["project"] = project
        if ttl_seconds:
            b["ttl_seconds"] = ttl_seconds
        if session:
            b["session"] = session
        if work_tokens is not None:
            b["work_tokens"] = work_tokens
        return self._req("POST", "/v1/add", b)

    # ---- the three-step read path (search -> timeline -> get_observations), session context, sessions
    def search(self, query="", project="*", kind=None, agent=None, date_start=None, date_end=None, limit=20, offset=0, order_by=None, tag=None):
        b = {"query": query, "project": project, "limit": limit, "offset": offset}
        for k, v in (("kind", kind), ("agent", agent), ("date_start", date_start), ("date_end", date_end), ("order_by", order_by), ("tag", tag)):
            if v:
                b[k] = v
        return self._req("POST", "/v1/search", b)

    def timeline(self, anchor=None, query=None, depth_before=3, depth_after=3, project=None, session=None):
        b = {"depth_before": depth_before, "depth_after": depth_after}
        for k, v in (("anchor", anchor), ("query", query), ("project", project), ("session", session)):
            if v:
                b[k] = v
        return self._req("POST", "/v1/timeline", b)

    def get_observations(self, ids, max_tokens=None):
        b = {"ids": list(ids)}
        if max_tokens:
            b["max_tokens"] = max_tokens
        return self._req("POST", "/v1/observations", b)

    def context(self, project="*", budget_tokens=1500, full=False):
        return self._req("POST", "/v1/context", {"project": project, "budget_tokens": budget_tokens, "full": bool(full)})

    def sessions(self, project="*", limit=30):
        return self._req("GET", f"/v1/sessions?project={urllib.request.quote(project, safe='')}&limit={int(limit)}")

    def supersede(self, old_id, kind, title, body, project=None):
        b = {"old_id": old_id, "kind": kind, "title": title, "body": body}
        if project:
            b["project"] = project
        return self._req("POST", "/v1/supersede", b)

    def focus(self, project=None):
        return self._req("GET", "/v1/focus" + (f"?project={urllib.request.quote(project, safe='')}" if project else ""))

    def set_focus(self, text, project=None):
        return self._req("POST", "/v1/focus", dict({"text": text}, **({"project": project} if project else {})))

    def end_focus(self, project=None):
        return self._req("POST", "/v1/focus/done", {"project": project} if project else {})

    def link(self, src, dst, kind): return self._req("POST", "/v1/link", {"src": src, "dst": dst, "kind": kind})
    def get(self, node_id): return self._req("GET", "/v1/node?id=" + urllib.request.quote(node_id, safe=""))

    # ---- owner (admin token)
    def review(self): return self._req("GET", "/v1/review")
    def usage(self, days=None): return self._req("GET", "/v1/usage" + (f"?days={int(days)}" if days else ""))
    def approve(self, node_id): return self._req("POST", "/v1/approve", {"id": node_id})
    def reject(self, node_id, reason=""): return self._req("POST", "/v1/reject", {"id": node_id, "reason": reason})
    def verify(self, node_id, conf): return self._req("POST", "/v1/verify", {"id": node_id, "conf": conf})
    def trash(self, limit=100): return self._req("GET", f"/v1/trash?limit={int(limit)}")
    def restore(self, node_id): return self._req("POST", "/v1/restore", {"id": node_id})
    def agents(self): return self._req("GET", "/v1/agents")
    def compact_report(self, dup=0.95, top=10): return self._req("GET", f"/v1/compact-report?dup={dup}&top={top}")

    def create_agent(self, name, lane, read=("*",), write=("*",), writes_per_hour=60):
        return self._req("POST", "/v1/agents", {"name": name, "lane": lane, "read": list(read), "write": list(write), "writes_per_hour": writes_per_hour})
