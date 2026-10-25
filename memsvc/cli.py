"""memctl: the owner's command line for the memory service. Standard library only.

  memctl init                                   create the owner (admin) token, shown once
  memctl serve [--port 8765] [--no-vectors]     run the local service (127.0.0.1 only)
  memctl agent create NAME --lane trusted|probation|readonly [--read P ...] [--write P ...]
  memctl agent list | revoke NAME | lane NAME LANE
  memctl review | approve ID | reject ID [--reason R] | verify ID V|S|U     (needs MEMSVC_TOKEN = the admin token)
  memctl trash | restore ID        rejected notes are kept, and a rejection can be undone (admin token)
  memctl recall "question" [--project P] [--limit N] [--budget N]           stats

Reading in three steps (claude-mem's search -> timeline -> get_observations), any agent token:
  memctl search ["query"] [--project P] [--kind K] [--agent A] [--since D] [--until D] [--limit N] [--offset N] [--order relevance|date_desc|date_asc]
  memctl timeline --anchor HANDLE | --query Q [--before N] [--after N] [--project P] [--session S]
  memctl get HANDLE [HANDLE ...] [--max-tokens N]
  memctl context [--project P ...] [--budget N] [--full]     background for the start of a session
  memctl sessions [--project P]                                memctl workflow     (how to read memory cheaply; needs no server)
  memctl focus [--project P]                  what is being worked on now (one per project; no --project is the global one)
  memctl focus set TEXT... [--project P]      replace it (a trusted agent's or the owner's token; a small model's waits for approval)
  memctl focus done [--project P]             finish it; the note is kept
  memctl doctor [--db PATH] [--chroma PATH] [--url URL]        read-only health check; never prints a secret
  memctl ui [--port 8765] [--no-open]          open the viewer already signed in as the owner (sign-in kept in ui.token, mode 600)
  memctl census [--days N] [--json]           what the reads cost and found: counts only, from usage.jsonl (no token or server needed)
  memctl eval [--questions F ...] [--no-vectors] [--json]     recall@5, MRR, abstention and a 5-fold check of the floors, on a COPY of the store

State lives in ~/.hydra/memsvc (override with MEMSVC_HOME): agents.json (hashed tokens), admin.token (hash), access.jsonl, usage.jsonl.
"""
import argparse
import json
import os
import re
import shutil
import signal
import stat
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import fingerprint, render
from .client import ApiError, MemoryClient
from .registry import LANES, Registry


def _home_path() -> Path:
    return Path(os.environ.get("MEMSVC_HOME") or (Path.home() / ".hydra" / "memsvc"))


def home() -> Path:
    h = _home_path()
    h.mkdir(parents=True, exist_ok=True)
    os.chmod(h, 0o700)
    return h


def registry() -> Registry:
    h = home()
    return Registry(h / "agents.json", h / "admin.token")


def hydra_config() -> dict:
    try:
        return json.loads((Path.home() / ".hydra" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cmd_init(a):
    tok = registry().ensure_admin()
    if tok is None:
        print("already initialized: the admin token exists and cannot be shown again (delete admin.token to reset it)")
        return 0
    print(f"admin token (shown once, store it in a password manager):\n{tok}\nexport MEMSVC_TOKEN=... when running review/approve/reject/verify")
    return 0


def cmd_agent(a):
    reg = registry()
    try:
        if a.agent_cmd == "create":
            tok = reg.create_agent(a.name, a.lane, read=a.read or ["*"], write=a.write or ["*"], writes_per_hour=a.writes_per_hour)
            print(f"agent {a.name} ({a.lane}) created. token (shown once):\n{tok}")
        elif a.agent_cmd == "list":
            for x in reg.list_agents():
                print(f"{x['name']:<20} {x['lane']:<10} read={','.join(x['read'])} write={','.join(x['write'])} {'REVOKED' if x['revoked'] else ''}")
        elif a.agent_cmd == "revoke":
            reg.revoke(a.name)
            print(f"revoked {a.name}")
        elif a.agent_cmd == "lane":
            reg.set_lane(a.name, a.lane)
            print(f"{a.name} is now {a.lane}")
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_serve(a):
    from .server import make_server
    from .service import MemoryService
    cfg = hydra_config()
    db = a.db or os.path.expanduser(cfg.get("graph_db") or "~/.hydra/graph.db")
    index = None
    if not a.no_vectors:
        rag = cfg.get("rag", {})
        py, chroma = rag.get("python"), a.chroma or rag.get("chroma_path")
        if py and chroma and Path(py).exists():
            from .vector import ChromaDaemonIndex
            index = ChromaDaemonIndex(py, os.path.expanduser(chroma))
    from .usage import UsageLog
    svc = MemoryService(db, registry(), index=index, floor=a.floor, fts_coverage=a.coverage, usage=UsageLog(home() / "usage.jsonl"))
    httpd = make_server(svc, svc.reg, host="127.0.0.1", port=a.port, access_log=home() / "access.jsonl")
    port = httpd.server_address[1]
    print(f"memory service listening on http://127.0.0.1:{port} (db {db}, vectors {'on' if index else 'off'})", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if index:
            index.close()
    return 0


def cmd_workflow(a):
    print(render.WORKFLOW)
    return 0


def cmd_doctor(a):
    """Read-only: reports, never repairs, never prints a token or a hash. Exit 1 if anything FAILs; warnings do not fail it."""
    fails = 0

    def say(level, name, detail):
        nonlocal fails
        fails += level == "FAIL"
        print(f"{level:<5} {name}: {detail}")

    def mode_check(p: Path, name: str, want: str):
        m = stat.S_IMODE(p.stat().st_mode)
        if m & 0o077:
            say("FAIL", name, f"mode {m:o}, expected {want}: other users could read it (fix: chmod {want} {p})")
        else:
            say("ok", name, f"mode {m:o}")

    h = _home_path()
    if not h.is_dir():
        say("FAIL", "state dir", f"{h} does not exist: run memctl init")
    else:
        mode_check(h, "state dir", "700")
        for name, required in (("admin.token", True), ("agents.json", False), ("access.jsonl", False), ("usage.jsonl", False), ("ui.token", False)):
            p = h / name
            if not p.exists():
                say("FAIL" if required else "ok", name, "missing: run memctl init" if required else "not created yet")
            else:
                mode_check(p, name, "600")
        try:
            d = json.loads((h / "admin.token").read_text(encoding="utf-8"))
            good = isinstance(d, dict) and isinstance(d.get("hash"), str) and re.fullmatch(r"[0-9a-f]{64}", d["hash"])
            say("ok" if good else "FAIL", "admin.token content", "a SHA-256 hash, as designed" if good else "not a token hash: reset it")
        except (OSError, ValueError):
            if (h / "admin.token").exists():
                say("FAIL", "admin.token content", "unreadable or not JSON: reset it")
        try:
            agents = Registry(h / "agents.json", h / "admin.token").list_agents()
            say("ok", "agents", f"{len(agents)} registered, {sum(1 for x in agents if x['revoked'])} revoked")
        except (OSError, ValueError, KeyError) as e:
            say("FAIL", "agents", f"agents.json is unreadable ({type(e).__name__})")
    cfg = hydra_config()
    db = Path(os.path.expanduser(a.db or cfg.get("graph_db") or "~/.hydra/graph.db"))
    if not db.exists():
        say("WARN", "graph db", f"{db} does not exist yet; it is created on first use")
    else:
        key = db.with_suffix(".key")
        if not key.exists():
            say("FAIL", "audit key", f"{key} is missing: the audit chain cannot be checked (opening the graph would create a NEW key, so doctor does not)")
        else:
            mode_check(key, "audit key", "600")
            try:
                from .service import load_graph_module
                g = load_graph_module().Graph(db)
                try:
                    ok, msg = g.verify_integrity()
                    say("ok" if ok else "FAIL", "audit chain", str(msg))
                    n = g.db.execute("SELECT count(*) FROM nodes WHERE status='active'").fetchone()[0]
                    say("ok", "graph db", f"{db}: {n} active notes")
                    from .service import scale_status, vector_gaps
                    sc = scale_status(n)
                    if sc["message"]:
                        say("WARN", "scale", sc["message"])
                    gaps = vector_gaps(g)
                    miss, stale = gaps["missing"], gaps["stale"]
                    if miss:
                        say("WARN", "vectors", f"{len(miss)} of {gaps['active']} active notes have no vector, so recall finds them by keyword only "
                                               f"(fix: python3 ~/.hydra/stack/memory/hydra_graph.py embed)")
                        for m in miss[:10]:
                            print(f"        {m['id']}  {str(m['title'])[:60]}")
                        if len(miss) > 10:
                            print(f"        ...and {len(miss) - 10} more")
                    if stale:
                        say("WARN", "vectors", f"{len(stale)} vector(s) were made from older text and are out of date (same fix: hydra_graph.py embed)")
                    if len(gaps["embedders"]) > 1:
                        say("WARN", "vectors", "vectors come from more than one embedder (" + ", ".join(f"{k}: {v}" for k, v in sorted(gaps["embedders"].items()))
                                               + "); rankings that mix them are unreliable")
                    if not miss and not stale and len(gaps["embedders"]) <= 1:
                        say("ok", "vectors", f"every one of the {gaps['active']} active notes has a vector")
                finally:
                    g.db.close()
            except Exception as e:                               # a broken graph module or database is a finding, not a crash
                say("FAIL", "graph db", f"cannot open {db} ({type(e).__name__})")
    rag = cfg.get("rag", {}) or {}
    if rag.get("python") and rag.get("chroma_path") and Path(rag["python"]).exists() and Path(os.path.expanduser(rag["chroma_path"])).exists():
        say("ok", "vector tier", "configured and present")
    else:
        say("WARN", "vector tier", "not configured here: recall is keyword-only until it is (memctl serve --no-vectors does this on purpose)")
    chroma = Path(os.path.expanduser(a.chroma or rag.get("chroma_path") or "~/.hydra/chroma"))
    pin = chroma / fingerprint.FILE_NAME
    if chroma.is_dir():
        try:
            rec = fingerprint.load(pin)
        except ValueError as e:
            say("FAIL", "embedder fingerprint", f"{e}: the vector service will refuse to run until the owner decides (delete the file to pin again, once the store is known good)")
        else:
            if rec is None:
                say("WARN", "embedder fingerprint", "not pinned yet (the vector service pins it the first time it starts)")
            else:
                say("ok", "embedder fingerprint", f"pinned to {rec['backend']}, {rec['dim']} dimensions, on {str(rec.get('created') or '?')[:10]}")
    free = shutil.disk_usage(db.parent if db.parent.exists() else Path.home()).free
    say("ok" if free > 1 << 30 else "WARN", "disk", f"{free / (1 << 30):.1f} GiB free beside the db")
    try:
        with urllib.request.urlopen(a.url.rstrip("/") + "/v1/health", timeout=2) as r:
            say("ok", "service", f"answering at {a.url} (v{json.loads(r.read().decode('utf-8')).get('version', '?')})")
    except Exception:
        say("WARN", "service", f"not reachable at {a.url}: not running, or another port")
    print("doctor: " + ("problems found" if fails else "no failures"))
    return 1 if fails else 0


def ui_token(h: Path) -> str:
    """The owner's sign-in for the viewer, kept in ui.token (mode 600) beside admin.token. When it is missing, or no longer the owner's,
    a fresh owner token is made: the old admin.token is moved aside to admin.token.replaced-<time>, never deleted."""
    reg = Registry(h / "agents.json", h / "admin.token")
    saved = h / "ui.token"
    if saved.exists():
        tok = saved.read_text(encoding="utf-8").strip()
        who = reg.authenticate(tok)
        if who is not None and who.lane == "admin":
            return tok
    if (h / "admin.token").exists():
        (h / "admin.token").rename(h / f"admin.token.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
    tok = reg.ensure_admin()
    fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.chmod(saved, 0o600)
    return tok


def cmd_ui(a):
    """Opens the viewer signed in as the owner. The token rides in the address fragment (#token=), which browsers never send to a server;
    the page keeps it for the tab and wipes it from the address bar. It is never printed."""
    tok = ui_token(home())
    base = f"http://127.0.0.1:{a.port}/"
    try:
        with urllib.request.urlopen(base + "v1/health", timeout=2):
            running = True
    except OSError:
        running = False
    if not running:
        print(f"the memory service is not running on port {a.port}: start it with  systemctl --user start memsvc", file=sys.stderr)
    if a.no_open:
        print(f"signed in: the viewer at {base} will open as the owner (sign-in kept in {home() / 'ui.token'})")
        return 0
    import webbrowser
    webbrowser.open(base + "#token=" + tok)
    print(f"opened {base} signed in as the owner")
    return 0


def cmd_census(a):
    """Reads usage.jsonl directly: the file is the owner's (mode 600), so no token and no running service are needed."""
    from .usage import UsageLog
    s = UsageLog(_home_path() / "usage.jsonl").summary(since=time.time() - a.days * 86_400 if a.days else None)
    print(json.dumps(s, indent=1) if a.json else render.census(s))
    return 0


def cmd_eval(a):
    """Works on a COPY: the graph file and its write-ahead log, the audit key and (with vectors) the vector store are copied into a new folder
    in the system temp directory, kept afterwards for inspection. The live store is never opened, so a running service is not disturbed."""
    from . import evaluate
    from .service import MemoryService
    cfg = hydra_config()
    db = Path(os.path.expanduser(a.db or cfg.get("graph_db") or "~/.hydra/graph.db"))
    if not db.exists():
        print(f"error: {db} does not exist", file=sys.stderr)
        return 2
    files = a.questions or [p for p in evaluate.default_question_files() if p.exists()]
    if not files:
        print("error: no question files found; pass --questions FILE", file=sys.stderr)
        return 2
    try:
        questions = evaluate.load_questions(files)
        floors = [float(x) for x in a.floors.split(",")]
        covs = [float(x) for x in a.coverages.split(",")]
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="memsvc-eval-"))
    for suffix in ("", "-wal"):                                   # the -wal file holds commits not yet folded into the main file
        if Path(str(db) + suffix).exists():
            shutil.copy2(str(db) + suffix, tmp / ("graph.db" + suffix))
    if db.with_suffix(".key").exists():
        shutil.copy2(db.with_suffix(".key"), tmp / "graph.key")
    index = None
    if not a.no_vectors:
        rag = cfg.get("rag", {}) or {}
        py, chroma = rag.get("python"), os.path.expanduser(a.chroma or rag.get("chroma_path") or "")
        if py and chroma and Path(py).exists() and Path(chroma).is_dir():
            shutil.copytree(chroma, tmp / "chroma")
            from .vector import ChromaDaemonIndex
            index = ChromaDaemonIndex(py, str(tmp / "chroma"), timeout=180)
        else:
            print("note: no vector tier configured here; evaluating keyword search only", file=sys.stderr)
    auto = [] if a.no_auto_must_not else evaluate.auto_must_not(tmp / "graph.db")
    questions += auto
    reg = Registry(tmp / "agents.json", tmp / "admin.token")
    agent = reg.authenticate(reg.create_agent("evalbot", "trusted"))
    configs = sorted({(f, c) for f in floors for c in covs} | {evaluate.DEFAULT})
    try:
        table = evaluate.grid(lambda f, c: MemoryService(tmp / "graph.db", reg, index=index, floor=f, fts_coverage=c), agent, questions, configs,
                              k=a.k, project=a.project)
    finally:
        if index:
            index.close()
    con = sqlite3.connect(str(tmp / "graph.db"))
    try:
        corpus = con.execute("SELECT count(*) FROM nodes WHERE status='active'").fetchone()[0]
    finally:
        con.close()
    r = evaluate.evaluate(table, questions, k=a.k, n_folds=a.folds, seed=a.seed)
    r.update(copy=str(tmp), corpus=corpus, vectors=index is not None, files=[str(p) for p in files],
             questions={**{t: sum(1 for q in questions if q["type"] == t) for t in evaluate.TYPES}, "auto_must_not": len(auto)})
    print(json.dumps(r, indent=1) if a.json else evaluate.report(r))
    return 0


def cmd_http(a):
    c = MemoryClient()
    try:
        if a.cmd == "search":
            print(render.index(c.search(a.query, project=a.project, kind=a.kind, agent=a.agent, date_start=a.since, date_end=a.until,
                                        limit=a.limit, offset=a.offset, order_by=a.order, tag=a.tag)))
        elif a.cmd == "timeline":
            print(render.timeline(c.timeline(anchor=a.anchor, query=a.query, depth_before=a.before, depth_after=a.after, project=a.project, session=a.session)))
        elif a.cmd == "get":
            print(render.observations(c.get_observations(a.ids, max_tokens=a.max_tokens)))
        elif a.cmd == "context":
            print(render.context(c.context(project=a.project or ["*"], budget_tokens=a.budget, full=a.full)))
        elif a.cmd == "focus":
            if a.action == "show":
                print(render.focus(c.focus(project=a.project)["focus"], project=a.project))
            elif a.action == "set":
                text = " ".join(a.text).strip()
                if not text:
                    print("error: say what you are working on: memctl focus set TEXT [--project P]", file=sys.stderr)
                    return 2
                r = c.set_focus(text, project=a.project)
                print({"add": "focus set", "unchanged": "focus unchanged (that is already the focus)", "reference": "focus proposed, waiting for the owner's approval",
                       "quarantined": "held for review because it looked unsafe", "duplicate": "not set: a near-identical note already exists"}.get(r["outcome"], r["outcome"])
                      + f" ({r['id']})")
            else:
                print(f"focus finished ({c.end_focus(project=a.project)['id']}); the note is kept")
        elif a.cmd == "sessions":
            rows = c.sessions(project=a.project)
            for s in rows:
                print(f"{s['session']:<30} {s['agent'] or '-':<14} {s['count']:>4} notes  last {s['last_ts']}  {s['summary'] or ''}")
            if not rows:
                print("(no sessions yet)")
        elif a.cmd == "trash":
            rows = c.trash()
            for x in rows:
                print(f"{x['id']}  rejected {x['rejected_at']}  [{x['kind']}] by {x['agent']}: {x['title']}")
            if not rows:
                print("(trash is empty)")
        elif a.cmd == "restore":
            print(c.restore(a.id))
        elif a.cmd == "review":
            for x in c.review():
                print(f"{x['id']}  [{x['status']}] by {x['agent']}: {x['title']}  {('screen: ' + ','.join(x['screen'])) if x['screen'] else ''}")
        elif a.cmd == "approve":
            print(c.approve(a.id))
        elif a.cmd == "reject":
            print(c.reject(a.id, a.reason))
        elif a.cmd == "verify":
            print(c.verify(a.id, a.conf))
        elif a.cmd == "compact-report":
            r = c.compact_report(dup=a.dup)
            print(f"{r['n']} notes, {r['d']} dimensions, coherence {r['coherence']:.2f}. {r['note']}")
            for g in r["duplicate_groups"]:
                print(f"near-duplicates (keep {g['keep']}):")
                for x in g["items"]:
                    print(f"  {'KEEP ' if x['id'] == g['keep'] else 'extra'} [{x['conf']}] {x['title']}  ({x['id']})")
            print("most unusual notes:")
            for o in r["outliers"]:
                print(f"  leverage {o['relative']:.1f}x mean  [{o['conf']}] {o['title']}  ({o['id']})")
        elif a.cmd == "stats":
            print(json.dumps(c.stats(), indent=1))
        elif a.cmd == "recall":
            r = c.recall(a.query, project=a.project, limit=a.limit, budget_tokens=a.budget)
            if r.get("degraded"):
                print(render.KEYWORD_ONLY if r["items"] else render.DEGRADED_EMPTY, file=sys.stderr)
            if r.get("context"):
                print(r["context"])
            else:
                for i in r["items"]:
                    print(f"- [{i['id']}] ({i['kind']}, conf {i['conf']}, {i['trust']}) {i['title']}")
                if not r["items"] and not r.get("degraded"):
                    print("(insufficient evidence: nothing relevant stored)")
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="memctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init")
    s = sp.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--db")
    s.add_argument("--chroma")
    s.add_argument("--no-vectors", action="store_true")
    s.add_argument("--floor", type=float, default=0.25, help="minimum vector similarity to count as evidence (0.25: measured, see docs/EVAL.md)")
    s.add_argument("--coverage", type=float, default=0.34, help="share of the query's content words a note must contain to count as keyword evidence")
    ag = sp.add_parser("agent")
    asp = ag.add_subparsers(dest="agent_cmd", required=True)
    c = asp.add_parser("create")
    c.add_argument("name")
    c.add_argument("--lane", required=True, choices=LANES)
    c.add_argument("--read", action="append")
    c.add_argument("--write", action="append")
    c.add_argument("--writes-per-hour", type=int, default=60)
    asp.add_parser("list")
    asp.add_parser("revoke").add_argument("name")
    ln = asp.add_parser("lane")
    ln.add_argument("name")
    ln.add_argument("lane", choices=LANES)
    sp.add_parser("review")
    sp.add_parser("approve").add_argument("id")
    r = sp.add_parser("reject")
    r.add_argument("id")
    r.add_argument("--reason", default="")
    v = sp.add_parser("verify")
    v.add_argument("id")
    v.add_argument("conf", choices=("V", "S", "U"))
    sp.add_parser("stats")
    cr = sp.add_parser("compact-report")
    cr.add_argument("--dup", type=float, default=0.95, help="cosine similarity at which notes count as near-duplicates")
    rc = sp.add_parser("recall")
    rc.add_argument("query")
    rc.add_argument("--project", default="*")
    rc.add_argument("--limit", type=int, default=8)
    rc.add_argument("--budget", type=int)
    se = sp.add_parser("search")
    se.add_argument("query", nargs="?", default="")
    se.add_argument("--project", default="*")
    se.add_argument("--kind")
    se.add_argument("--agent")
    se.add_argument("--tag", help="one whole tag: a concept, a file name or session-summary")
    se.add_argument("--since", help="2026-03-05 or an ISO time")
    se.add_argument("--until")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--offset", type=int, default=0)
    se.add_argument("--order", choices=("relevance", "date_desc", "date_asc"))
    tl = sp.add_parser("timeline")
    tl.add_argument("--anchor", help="a #handle or note id from the index")
    tl.add_argument("--query")
    tl.add_argument("--before", type=int, default=3)
    tl.add_argument("--after", type=int, default=3)
    tl.add_argument("--project")
    tl.add_argument("--session")
    gt = sp.add_parser("get")
    gt.add_argument("ids", nargs="+")
    gt.add_argument("--max-tokens", type=int)
    cx = sp.add_parser("context")
    cx.add_argument("--project", action="append")
    cx.add_argument("--budget", type=int, default=1500)
    cx.add_argument("--full", action="store_true")
    fo = sp.add_parser("focus")
    fo.add_argument("action", nargs="?", choices=["show", "set", "done"], default="show")
    fo.add_argument("text", nargs="*")
    fo.add_argument("--project")
    ss = sp.add_parser("sessions")
    ss.add_argument("--project", default="*")
    sp.add_parser("workflow")
    sp.add_parser("trash")
    sp.add_parser("restore").add_argument("id")
    dr = sp.add_parser("doctor")
    dr.add_argument("--db")
    dr.add_argument("--chroma", help="vector store folder, where the embedder fingerprint is pinned (default: the hydra config's)")
    dr.add_argument("--url", default="http://127.0.0.1:8765")
    ev = sp.add_parser("eval")
    ev.add_argument("--questions", action="append", help="a JSONL question file (repeat for more); default: the gsd-hydra-learn gold sets")
    ev.add_argument("--db")
    ev.add_argument("--chroma")
    ev.add_argument("--no-vectors", action="store_true")
    ev.add_argument("--no-auto-must-not", action="store_true", help="skip the must-not questions made from hidden notes")
    ev.add_argument("--project", default="*")
    ev.add_argument("--k", type=int, default=5)
    ev.add_argument("--floors", default="0,0.25,0.3,0.35,0.4,0.5")
    ev.add_argument("--coverages", default="0,0.34,0.5")
    ev.add_argument("--folds", type=int, default=5)
    ev.add_argument("--seed", type=int, default=1)
    ev.add_argument("--json", action="store_true")
    ui = sp.add_parser("ui")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-open", action="store_true", help="make or check the sign-in without opening a browser")
    ce = sp.add_parser("census")
    ce.add_argument("--days", type=int, help="only the last N days")
    ce.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    return {"init": cmd_init, "serve": cmd_serve, "agent": cmd_agent, "workflow": cmd_workflow, "doctor": cmd_doctor, "census": cmd_census, "eval": cmd_eval, "ui": cmd_ui}.get(a.cmd, cmd_http)(a)
