#!/usr/bin/env python3
"""MCP stdio bridge: lets Claude Desktop, Claude Code or any MCP-capable local frontend use the SAME memory service.

It speaks JSON-RPC over stdin/stdout and forwards each tool call to the HTTP API with the token from MEMSVC_TOKEN
(URL from MEMSVC_URL). Because every client goes through the service, they all get the same lanes, scopes and audit trail.
Register it in an MCP client as:  command python3, args ["-I", "/home/adversa/ai-memory/memsvc/mcp_bridge.py"],
env MEMSVC_TOKEN=<an agent token>.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from memsvc import __version__, render  # noqa: E402
from memsvc.client import ApiError, MemoryClient  # noqa: E402
from memsvc.tools import ANNOTATIONS, TOOLS, tools_for  # noqa: E402

DEFAULT_BUDGET = 1500
# The set of tools the bridge offers; MEMSVC_TOOLS=basic|progressive|all (default all) lets a small model see fewer.


def _text(s, error=False):
    out = {"content": [{"type": "text", "text": s}]}
    if error:
        out["isError"] = True
    return out


def _project_default(args: dict) -> dict:
    """MEMSVC_PROJECT opts one client into project isolation (claude-mem's per-project memory): reads and writes that name no project use it.
    An explicit project always wins, and "*" widens a read to every project. Unset (the default), nothing is filled in. Timeline and fetch
    never take the default: an id or handle already says where a note lives."""
    p = os.environ.get("MEMSVC_PROJECT", "").strip()
    return {**args, "project": p} if p and p != "*" and not args.get("project") else args


def call_tool(client: MemoryClient, name: str, args: dict) -> dict:
    if not isinstance(args, dict):
        return _text("arguments must be an object", error=True)
    if name in ("memory_search", "memory_context", "memory_recall", "memory_add"):
        args = _project_default(args)
    try:
        if name == "memory_workflow":
            return _text(render.WORKFLOW)
        if name == "memory_search":
            return _text(render.index(client.post("/v1/search", args)))
        if name == "memory_timeline":
            return _text(render.timeline(client.post("/v1/timeline", args)))
        if name == "memory_get_observations":
            return _text(render.observations(client.post("/v1/observations", args)))
        if name == "memory_context":
            return _text(render.context(client.post("/v1/context", {"budget_tokens": DEFAULT_BUDGET, **args})))
        if name == "memory_recall":
            r = client.recall(args["query"], project=args.get("project", "*"), limit=args.get("limit", 8), budget_tokens=args.get("budget_tokens") or DEFAULT_BUDGET)
            return _text(render.recall(r))
        if name == "memory_add":
            r = client.add(args["kind"], args["title"], args["body"], project=args.get("project"), tags=args.get("tags", ""),
                           ttl_seconds=args.get("ttl_seconds"), scope=args.get("scope", "shared"), session=args.get("session"),
                           work_tokens=args.get("work_tokens"))
            return _text(render.written(r))
        if name == "memory_supersede":
            r = client.supersede(args["old_id"], args["kind"], args["title"], args["body"], project=args.get("project"))
            return _text(render.written(r))
        if name == "memory_get":
            return _text(json.dumps(client.get(args["id"])))
        if name == "memory_pending":
            return _text(json.dumps(client.pending()))
        return _text(f"unknown tool {name}", error=True)
    except ApiError as e:
        return _text(f"memory service error: {e}", error=True)
    except KeyError as e:
        return _text(f"missing argument: {e}", error=True)


def handle(client: MemoryClient, msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None                                              # notifications get no reply
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": proto, "capabilities": {"tools": {}},
                                                        "serverInfo": {"name": "memsvc", "version": __version__}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        try:
            chosen = tools_for(os.environ.get("MEMSVC_TOOLS", "all"))
        except ValueError:
            chosen = TOOLS
        tools = [{"name": t["function"]["name"], "description": t["function"]["description"], "inputSchema": t["function"]["parameters"],
                  "annotations": ANNOTATIONS[t["function"]["name"]]} for t in chosen]
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        p = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": mid, "result": call_tool(client, p.get("name", ""), p.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    client = MemoryClient()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}), flush=True)
            continue
        reply = handle(client, msg)
        if reply is not None:
            print(json.dumps(reply), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
