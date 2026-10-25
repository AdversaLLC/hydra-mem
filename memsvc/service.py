"""MemoryService: the rules around hydra-graph for many callers. Standard library only (vectors are optional and injected).

hydra-graph stays the single source of truth (notes, edges, screening, HMAC audit chain, rollback). This layer adds:
  lanes       trusted writes are recalled; probation writes wait for the owner; readonly cannot write
  scopes      every retrieval path (keyword, vector, graph neighbours) checks project scope, privacy and expiry
  write gate  atomic notes only, allowed kinds only, confidence always starts at U, near-duplicates reported not stored
  supersede   a newer note replaces an older one in default recall; probation proposals wait for approval
  recall      stopword-aware keyword search + optional vectors, rank fusion, an explicit "insufficient evidence" answer,
              and a token-budgeted context block for small windows; results are cached per audit-chain state
progressive search -> timeline -> get_observations (claude-mem's three-step read path): an index with short handles and token costs,
              the notes around a hit, then full text for only the chosen few. Same visibility rule as recall on every path.
privacy     <private>...</private> is removed before storage (an unclosed tag fails closed); see privacy.py
sessions    an optional session id groups a run's notes; session-start context packs summary, corrections and decisions first
              and reports agent-claimed "work tokens" as economics, labelled as claims
No language model is ever called here.
"""
import functools
import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from . import neighbours, packer, privacy
from .neighbours import STOP
from .usage import age_band

ALLOWED_KINDS = ("note", "insight", "decision", "failed_path", "correction", "question", "claim", "measurement")
LINK_KINDS = ("supports", "contradicts", "depends_on", "part_of", "references", "fixed_by", "caused")
EXPAND_KINDS = ("supports", "part_of", "fixed_by", "references", "depends_on")
MAX_CHARS = 1200
SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_WORK_TOKENS = 10_000_000
MAX_BATCH = 20
SUMMARY_TAG = "session-summary"
FIRST_KINDS = ("correction", "failed_path", "decision")
CONF_RANK = {"V": 2, "S": 1, "U": 0}
ORDERS = (None, "relevance", "date_desc", "date_asc")
FOCUS_TAG = "focus"                       # the one reserved tag: a note carrying it is the current focus of its project
FOCUS_TITLE = "Current focus"
FOCUS_MAX = 600                           # one thing to work on, not a plan
READ_CAP = 20000                          # listings, timelines, session lists and the start-of-session context read this many of the newest active notes
WARN_AT = 8000                            # from tools/bench_read_path.py: the listing scan is noticeably slower from about here
CONTEXT_POOL = 3000                       # visible notes the start-of-session context sorts through
NEIGHBOUR_POOL = 800                      # keyword candidates a write looks through for neighbours (the same depth a filtered search uses)


def load_graph_module(path=None):
    path = path or os.environ.get("HYDRA_GRAPH_PY") or str(Path.home() / ".hydra" / "stack" / "memory" / "hydra_graph.py")
    spec = importlib.util.spec_from_file_location("hydra_graph_lib", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hydra_graph_lib"] = mod
    spec.loader.exec_module(mod)
    mod.TRUST.setdefault("svc-trusted", "agent")      # probation sources stay unknown prefixes, so untrusted -> reference
    mod.TRUST.setdefault("svc-admin", "user")         # the owner is the user lane
    return mod


class NullIndex:
    backend = None

    def upsert(self, docs): pass
    def delete(self, ids): pass
    def query(self, text, project, limit): return []


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def scale_status(active: int) -> dict:
    """Where the store stands against the listing scan: ok, slowing (reads cost more with every note) or full (the oldest notes are left out of
    listings, timelines, session lists and the context; a search with words still reaches every note)."""
    out = {"level": "ok", "message": None, "active": active, "warn_at": WARN_AT, "cap": READ_CAP}
    if active >= READ_CAP:
        left = active - READ_CAP
        out["level"] = "full"
        out["message"] = (f"{active:,} active notes: listings, timelines, session lists and the start-of-session context read only the newest {READ_CAP:,}; "
                          + (f"{left:,} older note{'' if left == 1 else 's'} {'is' if left == 1 else 'are'} left out of them. " if left else
                             "no older note is left out yet, but the next note added leaves the oldest out. ")
                          + "recall and searches with words still reach every note.")
    elif active >= WARN_AT:
        out["level"] = "slowing"
        out["message"] = (f"{active:,} active notes: reads are getting slower, because a listing scans every note. At {READ_CAP:,} the oldest notes start to drop "
                          "out of listings, timelines, session lists and the start-of-session context; a search with words still reaches them.")
    return out


def vector_gaps(g) -> dict:
    """Which active notes the vector tier has not covered: no vector at all, or one made from older text, and which embedders made the vectors
    that exist. Plain SQL on the graph, so `memctl doctor` can use it without the service running."""
    rows = g.db.execute("SELECT n.id, n.title, n.hash AS nh, v.hash AS vh, v.backend AS vb FROM nodes n LEFT JOIN vectors v ON v.node_id=n.id "
                        "WHERE n.status='active' AND n.trust IN ('agent','user') ORDER BY n.ts, n.rowid").fetchall()
    return {"active": len(rows),
            "missing": [{"id": r["id"], "title": r["title"]} for r in rows if r["vh"] is None],
            "stale": [{"id": r["id"], "title": r["title"]} for r in rows if r["vh"] is not None and r["vh"] != r["nh"]],
            "embedders": dict(Counter(r["vb"] for r in rows if r["vh"] is not None))}


def _counted(op, asked):
    """Every read that returns writes one counts-only row to the usage log, when there is one (2.1). A read that raises writes nothing, and a
    log that cannot be written never fails the read. `asked(args, kwargs)` says whether the read asked a question, so an empty answer to it
    counts as an abstention."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, agent, *a, **kw):
            t0 = time.perf_counter()
            res = fn(self, agent, *a, **kw)
            if self.usage is not None:
                try:
                    self._log_read(op, agent, res, (time.perf_counter() - t0) * 1000, asked(a, kw))
                except Exception:
                    pass
            return res
        return inner
    return wrap


def _never(a, kw) -> bool:
    return False


def _shingles(text: str, n: int = 3) -> set:
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(max(len(w) - n + 1, 1))} if w else set()


def _int(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be a whole number")
    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"{name} must be a whole number")
    return max(lo, min(hi, n))


def _when(value, end: bool = False) -> str:
    """A date (2026-03-05) or an ISO time as the graph's stored form, UTC to the second. A bare end date means the end of that day."""
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            raise ValueError("dates must look like 2026-03-05 or 2026-03-05T10:00:00Z")
        return s + ("T23:59:59Z" if end else "T00:00:00Z")
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("dates must look like 2026-03-05 or 2026-03-05T10:00:00Z")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemoryService:
    def __init__(self, db_path, registry, index=None, clock=time.time, hydra_py=None, floor: float = 0.0, cache_size: int = 256,
                 fts_coverage: float = 0.0, usage=None):
        self.hg = load_graph_module(hydra_py)
        self.usage = usage                        # a usage.UsageLog, or None: counts only, read back by nothing here
        self.db_path = Path(db_path)
        self.reg = registry
        self.index = index or NullIndex()
        self.clock = clock
        self.floor = floor
        self.fts_coverage = fts_coverage          # share of the query's content words a note must contain to count as keyword evidence
        self._wlock = threading.RLock()
        self._cache = OrderedDict()
        self._cache_size = cache_size
        self._hits = self._misses = 0
        g = self._g()
        g.db.close()

    # ---------------------------------------------------------------- plumbing
    def _g(self):
        return self.hg.Graph(self.db_path)

    @staticmethod
    def _extra(row) -> dict:
        try:
            return json.loads(row["extra"] or "{}")
        except ValueError:
            return {}

    @staticmethod
    def _need_admin(agent):
        if agent.lane != "admin":
            raise PermissionError("admin only")

    def _visible(self, agent, row) -> bool:
        """The one place that decides whether an agent may see a note. Used on every retrieval path."""
        if row is None:
            return False
        ex = self._extra(row)
        if agent.lane == "admin":
            return True
        if row["status"] != "active" or row["trust"] not in ("agent", "user"):
            return False
        if not agent.can_read(row["project"]):
            return False
        if ex.get("scope") == "private" and ex.get("owner") != agent.name:
            return False
        exp = ex.get("expires_at")
        if exp is not None and self.clock() >= exp:
            return False
        return True

    def _source(self, agent) -> str:
        return f"svc-{agent.lane}:{agent.name}" if agent.lane in ("trusted", "probation") else f"svc-admin:{agent.name}"

    def _index_row(self, g, row):
        docs = [{"id": row["id"], "text": row["title"] + "\n" + row["body"], "project": row["project"],
                 "meta": {"kind": row["kind"], "conf": row["conf"], "ts": row["ts"]}}]
        try:
            self.index.upsert(docs)
        except Exception:
            return                                          # the vector tier is optional; hydra-graph's own embed step catches up later
        if getattr(self.index, "backend", None):
            g.db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", (row["id"], self.index.backend, row["hash"], self.hg.now_iso()))

    # ---------------------------------------------------------------- writes
    def add(self, agent, kind, title, body, project=None, tags="", ttl_seconds=None, scope="shared", conf="U", force=False,
            _supersedes=None, session=None, work_tokens=None) -> dict:
        if agent.lane == "readonly":
            raise PermissionError("readonly agents cannot write")
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"kind must be one of {ALLOWED_KINDS}")
        if any(x is not None and not isinstance(x, str) for x in (title, body, tags)):
            raise ValueError("title, body and tags must be text")
        # <private> content is removed first, so it can never reach the duplicate check, the keyword index, the vectors or the log
        (title, ti), (body, bi), (tags, gi) = (privacy.strip_private(x or "") for x in (title, body, tags))
        removed = ti["removed"] + bi["removed"] + gi["removed"]
        unclosed = ti["unclosed_private"] or bi["unclosed_private"] or gi["unclosed_private"]
        title, body, tags = title.strip(), body.strip(), tags.strip()
        if removed and (not title or not body):
            raise ValueError("nothing left to store after removing <private> content")
        if not title or not body:
            raise ValueError("a note needs a title and a body")
        if len(title) + len(body) > MAX_CHARS:
            raise ValueError(f"too long: keep a note under {MAX_CHARS} characters and split it into atomic notes")
        if not agent.can_write(project):
            raise PermissionError(f"agent {agent.name!r} may not write to project {project!r}")
        if scope not in ("shared", "private"):
            raise ValueError("scope must be shared or private")
        if session is not None and not (isinstance(session, str) and SESSION_RE.match(session)):
            raise ValueError("session must be 1-64 letters, digits, dots, dashes or underscores")
        if work_tokens is not None and (isinstance(work_tokens, bool) or not isinstance(work_tokens, int) or not 0 <= work_tokens <= MAX_WORK_TOKENS):
            raise ValueError(f"work_tokens must be a whole number from 0 to {MAX_WORK_TOKENS}")
        ex = {"agent": agent.name, "scope": scope}
        if scope == "private":
            ex["owner"] = agent.name
        if ttl_seconds:
            ex["expires_at"] = self.clock() + float(ttl_seconds)
        if _supersedes:
            ex["supersedes"] = _supersedes
        if session:
            ex["session"] = session
        if work_tokens:
            ex["work_tokens"] = work_tokens

        def done(res: dict) -> dict:
            if removed:
                res["private_removed"] = removed
            if unclosed:
                res["private_unclosed"] = True
            return res

        with self._wlock:
            g = self._g()
            try:
                forced = bool(force and agent.lane == "admin")
                h = self.hg.norm_hash(title + " " + body)
                known, copies = self._known_rows(g, agent, title, body, project, h)
                dup, near = self._duplicate_of(known, title, body, h, skip=_supersedes) if not forced else (None, [])
                similar = self._similar(known, title, body, exclude={_supersedes, dup["id"] if dup else None})
                if dup is not None:
                    res = {"id": dup["id"], "handle": self.handle(dup["id"]), "outcome": "duplicate", "status": dup["status"], "lane": agent.lane}
                    diff = neighbours.differences(title + " " + body, dup["title"] + " " + dup["body"]) if dup["hash"] != h else {}
                    if diff:
                        res["differs"] = diff
                    if similar:
                        res["similar"] = similar
                    return done(res)
                # The service has already decided what counts as a duplicate, so the graph's own exact-copy check (which ignores privacy,
                # project and status) is switched off. A copy the writer may not see gets a fresh id of its own.
                node_id, outcome = g.add(kind, title, body, project=project, source=self._source(agent), conf="U", tags=tags,
                                         node_id=self._free_id(g, kind, title, h) if copies else None,
                                         session=f"svc:{agent.name}:{session}" if session else f"svc:{agent.name}", extra=ex, force=True)
                row = g.get(node_id)
                if outcome == "add":
                    self._index_row(g, row)
                    if _supersedes:
                        self._apply_supersession(g, node_id, _supersedes, agent.name)
                g.commit()
                res = {"id": node_id, "outcome": outcome, "status": row["status"], "lane": agent.lane}
                if similar:
                    res["similar"] = similar
                return done(res)
            finally:
                g.db.close()

    def _may_be_told(self, agent, row, project) -> bool:
        """Whether a write may learn that `row` exists: an active note the writer can read, or the writer's own pending one, in the same project
        or global. Hidden, trashed, superseded and expired notes are none of its business, and neither is a note in another project."""
        if row["project"] is not None and row["project"] != project:
            return False
        if row["status"] in ("reference", "quarantined"):
            return self._extra(row).get("agent") == agent.name
        return row["status"] == "active" and self._visible(agent, row)

    def _known_rows(self, g, agent, title, body, project, h):
        """The notes this write may be told about, and whether any note in any state already has this exact text. Candidates are fetched wide and
        filtered afterwards, so hidden notes can neither crowd a visible one out of the pool nor be revealed by being reported."""
        words = " ".join(w for w in re.findall(r"[a-z0-9]+", (title + " " + body).lower()) if w not in STOP) or title
        rows = {r["id"]: r for r in g.fts(words, project, NEIGHBOUR_POOL)}
        copies = g.db.execute("SELECT * FROM nodes WHERE hash=?", (h,)).fetchall()
        rows.update({r["id"]: r for r in copies})
        for r in g.db.execute("SELECT * FROM nodes WHERE source LIKE ? AND status IN ('reference','quarantined') ORDER BY ts DESC LIMIT 100",
                              (f"svc-%:{agent.name}",)):
            rows[r["id"]] = r
        return [r for r in rows.values() if self._may_be_told(agent, r, project)], bool(copies)

    @staticmethod
    def _duplicate_of(known, title, body, h, skip=None):
        """The note this text duplicates, if any: an exact copy, or one whose wording overlaps by 0.85 or more. The note being replaced is skipped
        unless the text is unchanged, because correcting a note by editing it is the point of replacing it."""
        mine = _shingles(title + " " + body)
        best, best_score = None, 0.0
        for r in known:
            if r["hash"] == h:
                score = 1.0
            elif r["id"] == skip:
                continue
            else:
                other = _shingles(r["title"] + " " + r["body"])
                score = len(mine & other) / len(mine | other) if mine and other else 0.0
            if score >= 0.85 and score > best_score:
                best, best_score = r, score
        return best, best_score

    def _similar(self, known, title, body, exclude) -> list:
        """Up to SIMILAR_MAX notes that share a topic with this text, closest first, each with what differs (numbers, a negation)."""
        mine = neighbours.content_words(title + " " + body)
        scored = []
        for r in known:
            if r["id"] in exclude:
                continue
            ov = neighbours.overlap(mine, neighbours.content_words(r["title"] + " " + r["body"]))
            if ov >= neighbours.SIMILAR_FLOOR:
                scored.append((ov, r))
        scored.sort(key=lambda t: -t[0])
        out = []
        for ov, r in scored[:neighbours.SIMILAR_MAX]:
            item = {"id": r["id"], "handle": self.handle(r["id"]), "title": packer.clean_text(r["title"])[:120], "overlap": round(ov, 2)}
            diff = neighbours.differences(title + " " + body, r["title"] + " " + r["body"])
            if diff:
                item["differs"] = diff
            out.append(item)
        return out

    def _free_id(self, g, kind, title, h):
        """An id for a note whose exact text another note, hidden from this writer, already has. The graph's own rule would collide on a third copy."""
        base = f"{kind}/{self.hg.now_iso()[:10]}/{self.hg.slugify(title)}"
        for n in range(1, 1000):
            nid = base if n == 1 else f"{base}-{h[:6]}" + ("" if n == 2 else f"-{n - 1}")
            if g.get(nid) is None:
                return nid
        raise RuntimeError("could not find a free id")

    def _safe_delete(self, ids):
        try:
            self.index.delete(ids)
        except Exception:
            pass

    def _apply_supersession(self, g, new_id, old_id, actor):
        g.link(new_id, old_id, "supersedes", source=f"svc:{actor}")
        g.set_status(old_id, "superseded", actor=actor, reason=f"superseded by {new_id}", override=True)
        self._safe_delete([old_id])

    def supersede(self, agent, old_id, kind, title, body, project=None, **kw) -> dict:
        if not isinstance(old_id, str):
            raise ValueError("old_id must be a note id or a #handle")
        g = self._g()
        try:
            seen = [r for r in self._pick(g, agent, old_id) if self._visible(agent, r)]
            if not seen:
                raise KeyError(old_id)
            if len(seen) > 1:
                raise ValueError(f"ambiguous handle {old_id!r}: it matches {len(seen)} notes ({', '.join(r['id'] for r in seen)}); use a full id")
            old = seen[0]
            owner = self._extra(old).get("agent") == agent.name or agent.lane == "admin"
            if agent.lane == "trusted" and not owner:
                raise PermissionError("only the owner of a note (or the admin) can supersede it directly")
        finally:
            g.db.close()
        return self.add(agent, kind, title, body, project=project, _supersedes=old["id"], **kw)

    # ---------------------------------------------------------------- current focus
    def _focus_scope(self, agent, project):
        if project is not None and (not isinstance(project, str) or not project.strip() or project == "*"):
            raise ValueError("project must be one project's name, or left out for the global focus")
        if project is not None and not agent.can_read(project):
            raise PermissionError(f"agent {agent.name!r} may not read project {project!r}")

    def _focus_row(self, g, agent, project):
        """The newest live focus note of this project (None means the global one) that the agent may read."""
        rows = g.db.execute("SELECT * FROM nodes WHERE status='active' AND project IS ? AND (',' || tags || ',') LIKE ? ORDER BY ts DESC, rowid DESC LIMIT 200",
                            (project, f"%,{FOCUS_TAG},%")).fetchall()
        return next((r for r in rows if self._visible(agent, r)), None)

    def _focus_item(self, row) -> dict:
        return {"id": row["id"], "handle": self.handle(row["id"]), "text": row["body"], "project": row["project"],
                "agent": self._extra(row).get("agent"), "since": row["ts"]}

    def focus_get(self, agent, project=None):
        """What the project is working on right now, or None. It is an ordinary note, so the visibility rule decides who sees it."""
        self._focus_scope(agent, project)
        g = self._g()
        try:
            row = self._focus_row(g, agent, project)
            return self._focus_item(row) if row is not None else None
        finally:
            g.db.close()

    def focus_set(self, agent, text, project=None) -> dict:
        """Make `text` the current focus of the project. The note it replaces is kept, superseded. The same text again changes nothing.
        A probation agent's focus waits for the owner like any of its notes, and replaces the old one when approved."""
        if agent.lane == "readonly":
            raise PermissionError("readonly agents cannot write")
        self._focus_scope(agent, project)
        if not isinstance(text, str):
            raise ValueError("the focus must be text")
        clean, info = privacy.strip_private(text)
        clean = clean.strip()
        if info["removed"] and not clean:
            raise ValueError("nothing left to store after removing <private> content")
        if not clean:
            raise ValueError("say what the focus is")
        if len(clean) > FOCUS_MAX:
            raise ValueError(f"keep the focus under {FOCUS_MAX} characters: it is one thing to work on")
        with self._wlock:
            g = self._g()
            try:
                cur = self._focus_row(g, agent, project)
            finally:
                g.db.close()
            if cur is not None and cur["body"].strip() == clean:
                return {"outcome": "unchanged", "id": cur["id"], "handle": self.handle(cur["id"]), "status": cur["status"], "lane": agent.lane}
            r = self.add(agent, "note", FOCUS_TITLE, text, project=project, tags=FOCUS_TAG, _supersedes=cur["id"] if cur is not None else None)
            r.pop("similar", None)
            return dict(r, handle=self.handle(r["id"]))

    def focus_done(self, agent, project=None) -> dict:
        """End the current focus. The note is kept, superseded, and the audit chain records who ended it."""
        if agent.lane == "readonly":
            raise PermissionError("readonly agents cannot write")
        self._focus_scope(agent, project)
        with self._wlock:
            g = self._g()
            try:
                cur = self._focus_row(g, agent, project)
                if cur is None:
                    raise KeyError("focus")
                if not agent.can_write(cur["project"]):
                    raise PermissionError(f"agent {agent.name!r} may not write to project {cur['project']!r}")
                g.set_status(cur["id"], "superseded", actor=agent.name, reason="focus done", override=True)
                g.commit()
            finally:
                g.db.close()
            self._safe_delete([cur["id"]])
            return {"id": cur["id"], "handle": self.handle(cur["id"]), "done": True}

    def link(self, agent, src, dst, kind) -> bool:
        if agent.lane not in ("trusted", "admin"):
            raise PermissionError("only trusted agents and the admin can link notes")
        if kind not in LINK_KINDS:
            raise ValueError(f"edge kind must be one of {LINK_KINDS}")
        with self._wlock:
            g = self._g()
            try:
                a, b = g.get(src), g.get(dst)
                if not (self._visible(agent, a) and self._visible(agent, b)):
                    raise KeyError("note not found")
                ok = g.link(src, dst, kind, source=self._source(agent))
                g.commit()
                return bool(ok)
            finally:
                g.db.close()

    # ---------------------------------------------------------------- reads
    @_counted("node", _never)
    def get(self, agent, node_id):
        """One note by id or #handle, if this agent may read it. An ambiguous handle is an error that lists only notes the agent can see."""
        if not isinstance(node_id, str):
            return None
        g = self._g()
        try:
            seen = self._pick(g, agent, node_id)
            if not seen:
                return None
            if len(seen) > 1:
                raise ValueError(f"ambiguous handle {node_id!r}: it matches {len(seen)} notes ({', '.join(r['id'] for r in seen)}); use a full id")
            row = seen[0]
            return {k: row[k] for k in ("id", "kind", "title", "body", "project", "conf", "trust", "status", "source", "tags", "ts")}
        finally:
            g.db.close()

    def pending(self, agent) -> list:
        g = self._g()
        try:
            rows = g.db.execute("SELECT * FROM nodes WHERE source LIKE ? AND status IN ('reference','quarantined') ORDER BY ts DESC LIMIT 100",
                                (f"svc-%:{agent.name}",)).fetchall()
            return [{"id": r["id"], "status": r["status"], "title": r["title"], "screen": self._extra(r).get("screen", [])} for r in rows]
        finally:
            g.db.close()

    def cache_stats(self) -> dict:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}

    def _log_read(self, op, agent, res, ms, asked):
        """One usage row: how many notes, the tokens served against the full text of those notes, and their ages in bands. The notes are
        looked up by id only to count them; no text, id or project name reaches the log."""
        items = ([res] if res else []) if op == "node" else ((res or {}).get("items") or [])
        g = self._g()
        try:
            rows = [r for r in (g.get(i["id"]) for i in items if isinstance(i, dict) and i.get("id")) if r is not None]
        finally:
            g.db.close()
        full = sum(packer.est_tokens(r["title"] + "\n" + r["body"]) for r in rows)
        if op in ("search", "timeline"):
            served = sum(packer.est_tokens(i["title"] + " " + i["snippet"]) for i in items)
        elif op == "context" or (op == "recall" and res.get("context")):
            served = res["used_tokens"]
        else:
            served = full
        now, ages = self.clock(), Counter()
        for r in rows:
            ages[age_band(now - datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp())] += 1
        degraded = bool(isinstance(res, dict) and res.get("degraded"))
        self.usage.record(op, agent.name, notes=len(items), served_tokens=served, full_tokens=full, asked=asked,
                          abstained=asked and not items, degraded=degraded, ms=ms, ages=dict(ages))

    def usage_summary(self, admin, days=None) -> dict:
        """The owner's view of the usage log: counts only, and for review only (nothing here ranks by it)."""
        self._need_admin(admin)
        if self.usage is None:
            return {"enabled": False, "reads": 0}
        since = None if days in (None, "") else self.clock() - _int(days, "days", 1, 3650) * 86_400
        return dict(self.usage.summary(since=since), enabled=True)

    @_counted("recall", lambda a, kw: True)
    def recall(self, agent, query, project="*", limit=8, budget_tokens=None) -> dict:
        if project != "*" and not agent.can_read(project):
            raise PermissionError(f"agent {agent.name!r} may not read project {project!r}")
        limit = max(1, min(int(limit), 50))
        g = self._g()
        try:
            key = (agent.name, agent.lane, agent.read, str(query).strip().lower(), project, limit, budget_tokens, g._last_chain())
            hit = self._cache.get(key)
            if hit is not None and self.clock() < hit[0]:
                self._hits += 1
                self._cache.move_to_end(key)
                return hit[1]
            self._misses += 1
            result = self._recall(g, agent, str(query), project, limit, budget_tokens)
            valid_until = result.pop("_valid_until", float("inf"))
            if not result.get("degraded"):                       # never freeze a keyword-only answer: the next call should try the vector lane again
                self._cache[key] = (valid_until, result)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            return result
        finally:
            g.db.close()

    @staticmethod
    def _degraded(trace) -> list:
        """Which retrieval lanes failed. A vector tier that was never configured is not a failure; only a configured one that raised is."""
        return ["vector"] if trace and trace.get("vector_error") else []

    def _scope_ok(self, agent, project):
        def ok(row):
            return self._visible(agent, row) and (project == "*" or row["project"] in (project, None))
        return ok

    def _rank(self, g, agent, query, project, accept=None):
        """Keyword search and vectors fused by rank. Returns (note ids, best first; trace; lane per id: keyword, vector or both). Every id passed
        _visible and the project scope.
        `accept` is an extra row filter (kind, writer, dates) applied while candidates are collected, BEFORE the top-N cut: filtering the
        finished top 40 instead would miss a matching note ranked 41st. A filtered search therefore looks deeper into both candidate lists."""
        toks = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in STOP and len(t) > 1]
        ranks, trace = {}, {"fts": 0, "vector": 0, "fused": 0, "expanded": 0}
        if not toks:
            return [], trace, {}
        scope_ok = self._scope_ok(agent, project)
        ok = scope_ok if accept is None else (lambda row: scope_ok(row) and accept(row))
        fts_depth, vec_depth = (80, 60) if accept is None else (800, 400)
        want = {_stem(t) for t in toks}

        def covered(row):
            have = {_stem(w) for w in re.findall(r"[a-z0-9]+", (row["title"] + " " + row["body"]).lower())}
            return len(want & have) / len(want) >= self.fts_coverage

        kw_ids, vec_ids = set(), set()
        for i, r in enumerate([r for r in g.fts(" ".join(toks), None, fts_depth) if ok(r) and covered(r)][:40]):
            ranks[r["id"]] = ranks.get(r["id"], 0) + 1 / (60 + i)
            kw_ids.add(r["id"])
            trace["fts"] += 1
        vec = []
        try:
            hits = self.index.query(query, None if project == "*" else project, vec_depth)
        except Exception as e:                              # degrade to keyword search, say so in the trace
            hits = []
            trace["vector_error"] = type(e).__name__
        for nid, score in hits:
            row = g.get(nid)
            if score >= self.floor and row is not None and ok(row):
                vec.append(nid)
        for i, nid in enumerate(vec[:40]):
            ranks[nid] = ranks.get(nid, 0) + 1 / (60 + i)
            vec_ids.add(nid)
            trace["vector"] += 1
        seeds = sorted(ranks, key=ranks.get, reverse=True)
        trace["fused"] = len(seeds)
        lanes = {i: "both" if (i in kw_ids and i in vec_ids) else ("keyword" if i in kw_ids else "vector") for i in ranks}
        return seeds, trace, lanes

    def _recall(self, g, agent, query, project, limit, budget_tokens) -> dict:
        empty = {"items": [], "sufficient": False, "context": "", "used_tokens": 0}
        ok = self._scope_ok(agent, project)
        seeds, trace, lanes = self._rank(g, agent, query, project)
        degraded = self._degraded(trace)
        if not seeds:
            return {**empty, "trace": trace, "degraded": degraded}
        ordered, seen = [], set()
        for nid in seeds[:limit]:
            ordered.append((nid, "match"))
            seen.add(nid)
        for nid in seeds[:3]:
            for other, ek, _d in g.neighbors(nid, EXPAND_KINDS):
                if other not in seen and len(ordered) < limit:
                    row = g.get(other)
                    if row is not None and ok(row):
                        seen.add(other)
                        ordered.append((other, f"{ek} of {nid}"))
                        trace["expanded"] += 1
        items = []
        for nid, via in ordered:
            r = g.get(nid)
            items.append({"id": nid, "handle": self.handle(nid), "ref": "#" + self.handle(nid), "kind": r["kind"], "conf": r["conf"], "trust": r["trust"],
                          "title": self.hg.sanitize_for_context(r["title"]), "body": r["body"], "project": r["project"], "via": via,
                          "found_by": lanes.get(nid, "graph")})
        exps = [self._extra(g.get(i["id"])).get("expires_at") for i in items]
        out = {"items": items, "sufficient": True, "trace": trace, "degraded": degraded, "context": "", "used_tokens": 0,
               "_valid_until": min([e for e in exps if e is not None], default=float("inf"))}
        if budget_tokens:
            p = packer.pack(items, int(budget_tokens))
            out.update(context=p["text"], used_tokens=p["used_tokens"], dropped=p["dropped"], truncated=p["truncated"])
            out["sufficient"] = bool(p["items"])
        return out

    # ---------------------------------------------------------------- three-step read path: search -> timeline -> get_observations
    @staticmethod
    def handle(node_id: str) -> str:
        """A short, stable name for a note. Small models mistype long ids; eight hex characters they can copy."""
        return hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _label(session):
        s = str(session or "")
        return (s[4:] if s.startswith("svc:") else s) or None

    def _item(self, row, found_by=None) -> dict:
        """One line of an index: no body, a snippet, and what reading the whole note would cost. `found_by` is the lane that found it, when known."""
        ex = self._extra(row)
        snippet = packer.clean_text(row["body"])
        if len(snippet) > 140:
            snippet = snippet[:139].rstrip() + "…"
        item = {"id": row["id"], "handle": self.handle(row["id"]), "ts": row["ts"], "kind": row["kind"], "conf": row["conf"], "trust": row["trust"],
                "project": row["project"], "agent": ex.get("agent"), "title": self.hg.sanitize_for_context(row["title"]), "snippet": snippet,
                "tags": row["tags"] or "", "read_tokens": packer.est_tokens(row["title"] + "\n" + row["body"])}
        if found_by:
            item["found_by"] = found_by
        return item

    def _full(self, row) -> dict:
        ex = self._extra(row)
        d = {k: row[k] for k in ("id", "kind", "title", "body", "project", "conf", "trust", "status", "source", "tags", "ts")}
        d.update(handle=self.handle(row["id"]), agent=ex.get("agent"), session=self._label(row["session"]), work_tokens=int(ex.get("work_tokens") or 0),
                 read_tokens=packer.est_tokens(row["title"] + "\n" + row["body"]))
        return d

    @staticmethod
    def _hex_ref(ref: str):
        """The lower-case hex of a handle reference (8 to 40 characters, optional leading #), or None if it is a full id."""
        r = ref.strip()
        h = (r[1:] if r.startswith("#") else r).lower()
        return h if re.fullmatch(r"[0-9a-f]{8,40}", h) else None

    def _handle_table(self, g) -> list:
        """[(handle, full sha1 hex, id)] for every note. Built once per call that has a handle in it."""
        return [(self.handle(nid), hashlib.sha1(nid.encode("utf-8")).hexdigest(), nid) for (nid,) in g.db.execute("SELECT id FROM nodes")]

    def _pick(self, g, agent, ref: str, table=None, active_only: bool = False) -> list:
        """The rows `ref` can mean AND this agent may read. A full id names one note. A handle names every note whose handle matches, and a hex
        prefix longer than eight characters narrows a clash. Hidden notes are filtered out here, so they can neither block a visible note nor be
        revealed by a clash. `active_only` is for timeline anchors, which must be live notes; reading allows an author's own pending note."""
        h = self._hex_ref(ref)
        if h is None:
            ids = [ref.strip()]
        else:
            table = table if table is not None else self._handle_table(g)
            ids = [nid for hd, _hx, nid in table if hd == h] if len(h) == 8 else [nid for _hd, hx, nid in table if hx.startswith(h)]
        seen = []
        for nid in ids:
            row = g.get(nid)
            if row is None:
                continue
            if active_only:
                ok = row["status"] == "active" and self._visible(agent, row)
            else:
                own_pending = row["status"] in ("reference", "quarantined") and self._extra(row).get("agent") == agent.name
                ok = self._visible(agent, row) or own_pending
            if ok:
                seen.append(row)
        return seen

    @staticmethod
    def _active_rows(g, desc: bool = True, svc_only: bool = False):
        """(rows, capped): the newest READ_CAP active notes in the order asked for. Past the cap it is always the OLDEST that are left out,
        whichever way the caller sorts, and `capped` says it happened."""
        where = "status='active'" + (" AND (source LIKE 'svc-%' OR source LIKE 'approved:svc-%')" if svc_only else "")
        rows = g.db.execute(f"SELECT * FROM nodes WHERE {where} ORDER BY ts DESC, rowid DESC LIMIT ?", (READ_CAP + 1,)).fetchall()
        capped = len(rows) > READ_CAP
        rows = rows[:READ_CAP]
        if not desc:
            rows.reverse()
        return rows, capped

    @_counted("search", lambda a, kw: bool(str(kw.get("query", a[0] if a else "") or "").strip()))
    def search(self, agent, query="", project="*", kind=None, agent_name=None, date_start=None, date_end=None, limit=20, offset=0,
               order_by=None, tag=None) -> dict:
        """Step 1: an index of matching notes (no bodies), newest first when there is no query. Filters narrow it before paging.
        `tag` matches one whole tag exactly (case-insensitive), such as a concept, a file name or session-summary."""
        project = project or "*"
        if project != "*" and not agent.can_read(project):
            raise PermissionError(f"agent {agent.name!r} may not read project {project!r}")
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be text")
        if kind is not None and kind not in ALLOWED_KINDS:
            raise ValueError(f"kind must be one of {ALLOWED_KINDS}")
        if order_by not in ORDERS:
            raise ValueError("order_by must be relevance, date_desc or date_asc")
        lo = _when(date_start) if date_start else None
        hi = _when(date_end, end=True) if date_end else None
        limit, offset = _int(limit, "limit", 1, 100), _int(offset, "offset", 0, 100_000)
        q = (query or "").strip()
        tag_key = None
        if tag not in (None, ""):
            if not isinstance(tag, str) or not tag.strip() or len(tag) > 64 or "," in tag or any(ord(ch) < 32 for ch in tag):
                raise ValueError("tag must be one short word or phrase, with no commas or control characters")
            tag_key = tag.strip().lower()

        def accept(r) -> bool:
            return (r["status"] == "active" and (kind is None or r["kind"] == kind)
                    and (agent_name is None or self._extra(r).get("agent") == agent_name)
                    and (lo is None or r["ts"] >= lo) and (hi is None or r["ts"] <= hi)
                    and (tag_key is None or tag_key in {t.strip().lower() for t in (r["tags"] or "").split(",")}))

        g = self._g()
        try:
            trace, lanes, capped = None, {}, False
            if q:
                seeds, trace, lanes = self._rank(g, agent, q, project, accept=accept)
                rows = [r for r in (g.get(i) for i in seeds) if r is not None]
                if order_by in ("date_desc", "date_asc"):
                    rows.sort(key=lambda r: (r["ts"], r["id"]), reverse=(order_by == "date_desc"))
            else:
                ok = self._scope_ok(agent, project)
                scan, capped = self._active_rows(g, desc=(order_by != "date_asc"))
                rows = [r for r in scan if ok(r)]
            rows = [r for r in rows if accept(r)]
            page = rows[offset:offset + limit]
            items = [self._item(r, lanes.get(r["id"])) for r in page]
            res = {"items": items, "sufficient": bool(rows), "total": len(rows), "total_read_tokens": sum(i["read_tokens"] for i in items),
                   "next_offset": offset + len(items) if offset + len(items) < len(rows) else None,
                   "degraded": self._degraded(trace)}
            if trace is not None:
                res["trace"] = trace
            if capped:
                res["scan_capped"] = True
            return res
        finally:
            g.db.close()

    @_counted("timeline", lambda a, kw: bool(kw.get("anchor", a[0] if a else None) or kw.get("query", a[1] if len(a) > 1 else None)))
    def timeline(self, agent, anchor=None, query=None, depth_before=3, depth_after=3, project=None, session=None) -> dict:
        """Step 2: the visible notes just before and after a hit, in time order. The anchor is an id, a handle, a query's best hit or
        (with session) a whole session. Hidden notes are skipped and not counted; an unknown anchor and a hidden one look the same."""
        before, after = _int(depth_before, "depth_before", 0, 20), _int(depth_after, "depth_after", 0, 20)
        if project not in (None, "*") and not agent.can_read(project):
            raise PermissionError(f"agent {agent.name!r} may not read project {project!r}")
        sess_key = None
        if session:
            if not (isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}(:[A-Za-z0-9._-]{0,64})?", session)):
                raise ValueError("session must be an id, agent:id, or agent: for that agent's notes without a session id")
            sess_key = f"svc:{session.rstrip(':')}" if ":" in session else f"svc:{agent.name}:{session}"
        none = {"anchor": None, "items": [], "total_read_tokens": 0, "sufficient": False}
        degraded = []
        g = self._g()
        try:
            anchor_row = None
            if anchor:
                if not isinstance(anchor, str):
                    raise ValueError("anchor must be a note id or handle")
                seen = self._pick(g, agent, anchor, active_only=True)
                if len(seen) > 1:
                    return dict(none, ambiguous=[r["id"] for r in seen])
                if not seen:
                    return none
                anchor_row = seen[0]
            elif query:
                if not isinstance(query, str):
                    raise ValueError("query must be text")
                seeds, trace, _lanes = self._rank(g, agent, query, project or "*")
                degraded = self._degraded(trace)
                rows = [r for r in (g.get(i) for i in seeds) if r is not None and r["status"] == "active"]
                if not rows:
                    return dict(none, degraded=degraded) if degraded else none
                anchor_row = rows[0]
            scope = (anchor_row["project"] if project is None else project) if anchor_row is not None else (project or "*")
            scan, capped = self._active_rows(g, desc=False)
            vis = [r for r in scan if self._visible(agent, r) and (scope == "*" or r["project"] == scope)]
            if anchor_row is not None:
                idx = next((k for k, r in enumerate(vis) if r["id"] == anchor_row["id"]), None)
                if idx is None:
                    return dict(none, scan_capped=True) if capped else none
                window = vis[max(0, idx - before): idx + after + 1]
            elif sess_key:
                window = [r for r in vis if r["session"] == sess_key][-50:]
            else:
                window = vis[-(before + after + 1):]
            anchor_id = anchor_row["id"] if anchor_row is not None else None
            items = [dict(self._item(r), is_anchor=(r["id"] == anchor_id)) for r in window]
            res = {"anchor": anchor_id, "items": items, "total_read_tokens": sum(i["read_tokens"] for i in items), "sufficient": bool(items)}
            if degraded:
                res["degraded"] = degraded
            if capped:
                res["scan_capped"] = True
            return res
        finally:
            g.db.close()

    @_counted("observations", _never)
    def get_observations(self, agent, ids, max_tokens=None) -> dict:
        """Step 3: full text for the ids (or handles) you picked. Notes you may not read are reported as missing, exactly like absent ones."""
        if not isinstance(ids, (list, tuple)) or not ids:
            raise ValueError("ids must be a non-empty list")
        if len(ids) > MAX_BATCH:
            raise ValueError(f"fetch at most {MAX_BATCH} notes at once; filter with memory_search first")
        if any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError("every id must be a text id or handle")
        budget = None if max_tokens is None else _int(max_tokens, "max_tokens", 1, 1_000_000)
        g = self._g()
        try:
            items, missing, skipped, ambiguous, used = [], [], [], [], 0
            table = self._handle_table(g) if any(self._hex_ref(i) for i in ids) else None
            for ref in ids:
                seen = self._pick(g, agent, ref, table)
                if len(seen) > 1:
                    ambiguous.append({"ref": ref, "candidates": [r["id"] for r in seen]})
                    continue
                if not seen:
                    missing.append(ref)
                    continue
                full = self._full(seen[0])
                if budget is not None and items and used + full["read_tokens"] > budget:
                    skipped.append(ref)
                    continue
                items.append(full)
                used += full["read_tokens"]
            return {"items": items, "missing": missing, "ambiguous": ambiguous, "skipped": skipped, "truncated": bool(skipped), "total_read_tokens": used}
        finally:
            g.db.close()

    @_counted("context", _never)
    def context(self, agent, project="*", budget_tokens=1500, full=False) -> dict:
        """What to show a model at the start of a session: the latest session summaries, then corrections, failed paths and decisions, then the
        rest, inside a token budget. Economics compare what the block costs to read with the work its notes' authors say they cost to find."""
        projects = [project] if isinstance(project, str) else list(project or ["*"])
        if not projects or len(projects) > 10 or any(not isinstance(p, str) or not p for p in projects):
            raise ValueError("project must be a name, or a list of up to 10 names")
        for p in projects:
            if p != "*" and not agent.can_read(p):
                raise PermissionError(f"agent {agent.name!r} may not read project {p!r}")
        budget = _int(budget_tokens, "budget_tokens", 50, 20_000)
        g = self._g()
        try:
            scan, capped = self._active_rows(g, desc=True)
            # the caller's own notes are picked out of the scan first and only then cut to CONTEXT_POOL, so newer notes it may not read cannot push them out
            rows = list(islice((r for r in scan if self._visible(agent, r) and ("*" in projects or r["project"] in projects or r["project"] is None)),
                               CONTEXT_POOL))
            foci = [r for r in (self._focus_row(g, agent, p) for p in dict.fromkeys([p for p in projects if p != "*"] + [None])) if r is not None]
            rows = [r for r in rows if FOCUS_TAG not in (r["tags"] or "").split(",")]          # the focus is placed first below, once per project asked for
            summaries = [r for r in rows if SUMMARY_TAG in (r["tags"] or "").split(",")][:2]
            taken = {r["id"] for r in summaries}
            firsts = sorted((r for r in rows if r["id"] not in taken and r["kind"] in FIRST_KINDS), key=lambda r: (-CONF_RANK.get(r["conf"], 0)), reverse=False)
            taken |= {r["id"] for r in firsts}
            rest = sorted((r for r in rows if r["id"] not in taken), key=lambda r: -CONF_RANK.get(r["conf"], 0))
            ordered = (foci + summaries + firsts + rest)[:60]                # sorted() is stable: recency order survives inside each confidence band
            work = {r["id"]: int(self._extra(r).get("work_tokens") or 0) for r in ordered}
            items = [{"id": r["id"], "handle": self.handle(r["id"]), "ref": "#" + self.handle(r["id"]), "kind": r["kind"], "conf": r["conf"], "trust": r["trust"],
                      "title": self.hg.sanitize_for_context(r["title"]), "body": r["body"]} for r in ordered]
            per_item = 400 if full else 80

            def footer(p, read):
                w = sum(work[i["id"]] for i in p["items"])
                if not w:
                    return None, w
                return (f"economics: {len(p['items'])} notes, ~{read} tokens to read; they stand for ~{w} tokens of earlier work "
                        "(reported by the writing agents, not measured)"), w

            p = packer.pack(items, budget, max_item_tokens=per_item)
            text, w = p["text"], 0
            if p["items"]:
                foot, w = footer(p, packer.est_tokens(p["text"]))
                if foot and packer.est_tokens(p["text"] + "\n" + foot) > budget:
                    p = packer.pack(items, max(0, budget - packer.est_tokens("\n" + foot) - 2), max_item_tokens=per_item)
                    foot, w = footer(p, packer.est_tokens(p["text"])) if p["items"] else (None, 0)
                text = p["text"]
                if foot:
                    read = packer.est_tokens(text)
                    for _ in range(4):                                # the footer quotes its own block's cost: settle the number
                        cand = text + "\n" + footer(p, read)[0]
                        nxt = packer.est_tokens(cand)
                        if nxt == read:
                            break
                        read = nxt
                    cand = text + "\n" + footer(p, read)[0]
                    if packer.est_tokens(cand) <= budget:
                        text = cand
                    else:
                        w = 0
            used = packer.est_tokens(text) if text else 0
            saved = max(0, w - used)
            econ = {"notes": len(p["items"]), "read_tokens": used, "work_tokens": w, "saved_tokens": saved,
                    "saved_percent": round(100 * saved / w) if w else 0, "agent_reported": True}
            res = {"context": text, "items": p["items"], "used_tokens": used, "dropped": p["dropped"], "truncated": p["truncated"],
                   "sufficient": bool(p["items"]), "economics": econ}
            if capped:
                res["scan_capped"] = True
            return res
        finally:
            g.db.close()

    def sessions(self, agent, project="*", limit=30) -> list:
        """The sessions the service knows about (notes written with a session id, or by an agent without one), newest first."""
        project = project or "*"
        if project != "*" and not agent.can_read(project):
            raise PermissionError(f"agent {agent.name!r} may not read project {project!r}")
        limit = _int(limit, "limit", 1, 200)
        g = self._g()
        try:
            ok = self._scope_ok(agent, project)
            groups = {}
            scan, capped = self._active_rows(g, desc=True, svc_only=True)
            for r in scan:
                if not ok(r):
                    continue
                s = groups.setdefault(r["session"], {"session": self._label(r["session"]), "agent": self._extra(r).get("agent"), "projects": set(),
                                                     "count": 0, "first_ts": r["ts"], "last_ts": r["ts"], "summary": None})
                s["count"] += 1
                s["first_ts"] = min(s["first_ts"], r["ts"])
                s["last_ts"] = max(s["last_ts"], r["ts"])
                if r["project"]:
                    s["projects"].add(r["project"])
                if s["summary"] is None and SUMMARY_TAG in (r["tags"] or "").split(","):
                    s["summary"] = r["title"]                          # rows arrive newest first, so this is the latest summary
            out = sorted(groups.values(), key=lambda s: s["last_ts"], reverse=True)[:limit]
            for s in out:
                s["projects"] = sorted(s["projects"])
                if capped:
                    s["scan_capped"] = True
            return out
        finally:
            g.db.close()

    # ---------------------------------------------------------------- owner (admin) actions
    def review_list(self, admin) -> list:
        self._need_admin(admin)
        g = self._g()
        try:
            rows = g.db.execute("SELECT * FROM nodes WHERE source LIKE 'svc-%' AND status IN ('quarantined','reference') ORDER BY ts DESC LIMIT 200").fetchall()
            return [{"id": r["id"], "status": r["status"], "agent": self._extra(r).get("agent"), "title": r["title"], "body": r["body"][:300],
                     "screen": self._extra(r).get("screen", []), "project": r["project"]} for r in rows]
        finally:
            g.db.close()

    def approve(self, admin, node_id) -> dict:
        self._need_admin(admin)
        with self._wlock:
            g = self._g()
            try:
                row = g.get(node_id)
                if row is None:
                    raise KeyError(node_id)
                g.set_status(node_id, "active", actor="approved:" + admin.name, reason="owner approved", override=True)
                row = g.get(node_id)
                self._index_row(g, row)
                sup = self._extra(row).get("supersedes")
                if sup and g.get(sup) is not None:
                    self._apply_supersession(g, node_id, sup, admin.name)
                g.commit()
                return {"id": node_id, "status": "active"}
            finally:
                g.db.close()

    def reject(self, admin, node_id, reason="") -> dict:
        self._need_admin(admin)
        with self._wlock:
            g = self._g()
            try:
                if g.get(node_id) is None:
                    raise KeyError(node_id)
                g.set_status(node_id, "rolled_back", actor=admin.name, reason=reason or "rejected by owner", override=True)
                self._safe_delete([node_id])
                g.commit()
                return {"id": node_id, "status": "rolled_back"}
            finally:
                g.db.close()

    def trash(self, admin, limit=100) -> list:
        """The notes the owner rejected, newest first. Nothing is ever deleted: a rejected note keeps its text and its audit trail."""
        self._need_admin(admin)
        limit = _int(limit, "limit", 1, 500)
        g = self._g()
        try:
            rows = g.db.execute("SELECT * FROM nodes WHERE status='rolled_back' AND (source LIKE 'svc-%' OR source LIKE 'approved:svc-%') "
                                "ORDER BY updated DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
            return [dict(self._item(r), rejected_at=r["updated"]) for r in rows]
        finally:
            g.db.close()

    def restore(self, admin, node_id) -> dict:
        """Undo a rejection. The note becomes active again, is searchable by meaning again, and, if it was a proposed replacement, replaces
        its target exactly as approving it would have. Only rejected notes written through this service can be restored."""
        self._need_admin(admin)
        with self._wlock:
            g = self._g()
            try:
                row = g.get(node_id)
                if row is None:
                    raise KeyError(node_id)
                if row["status"] != "rolled_back" or not str(row["source"]).startswith(("svc-", "approved:svc-")):
                    raise ValueError("only a rejected note can be restored (a superseded note was replaced, not rejected)")
                # A note that was never vetted (a probation agent's) is restored as an approval, like review would have done. A note that
                # already had a trusted or owner-approved standing keeps exactly that standing: restoring must not raise anyone's trust.
                actor = ("restored:" if row["trust"] in ("agent", "user") else "approved:") + admin.name
                g.set_status(node_id, "active", actor=actor, reason="owner restored a rejected note", override=True)
                row = g.get(node_id)
                self._index_row(g, row)
                sup = self._extra(row).get("supersedes")
                target = g.get(sup) if sup else None
                if target is not None and target["status"] == "active":
                    self._apply_supersession(g, node_id, sup, admin.name)
                g.commit()
                return {"id": node_id, "status": "active"}
            finally:
                g.db.close()

    def verify(self, admin, node_id, conf) -> dict:
        self._need_admin(admin)
        if conf not in ("V", "S", "U"):
            raise ValueError("conf must be V, S or U")
        with self._wlock:
            g = self._g()
            try:
                if not g.set_conf(node_id, conf, admin.name):
                    raise KeyError(node_id)
                g.commit()
                return {"id": node_id, "conf": conf}
            finally:
                g.db.close()

    def compact_report(self, admin, dup: float = 0.95, top: int = 10) -> dict:
        """Which notes are redundant or unusual, from embedding geometry. Review only: nothing is archived or deleted here."""
        self._need_admin(admin)
        if not hasattr(self.index, "geometry"):
            raise ValueError("the compaction report needs the vector tier")
        rep = self.index.geometry(dup=dup, top=top)
        rank = {"V": 2, "S": 1, "U": 0}
        g = self._g()
        try:
            def info(nid):
                r = g.get(nid)
                if r is None or r["status"] != "active":
                    return None
                return {"id": nid, "title": r["title"], "conf": r["conf"], "kind": r["kind"], "project": r["project"], "ts": r["ts"]}

            groups = []
            for grp in rep.get("duplicate_groups", []):
                items = [x for x in (info(i) for i in grp) if x]
                if len(items) < 2:
                    continue
                keep = max(items, key=lambda x: (rank.get(x["conf"], 0), x["ts"], x["id"]))          # best evidence, then newest
                groups.append({"items": items, "keep": keep["id"], "archive_candidates": [x["id"] for x in items if x["id"] != keep["id"]]})
            outliers = []
            for o in rep.get("outliers", []):
                i = info(o["id"])
                if i:
                    outliers.append({**o, "title": i["title"], "kind": i["kind"], "conf": i["conf"]})
        finally:
            g.db.close()
        return {"n": rep.get("n"), "d": rep.get("d"), "coherence": rep.get("coherence"), "mean_leverage": rep.get("mean_leverage"),
                "outliers": outliers, "duplicate_groups": groups,
                "note": "review only: nothing is archived or deleted by this report; leverage is geometry, not importance to the owner"}

    def vector_status(self, agent) -> dict:
        """The state of the vector tier in words the owner can act on. Asking never starts the embedding daemon. Only the owner gets note ids."""
        backend = getattr(self.index, "backend", None)
        fp, reason = None, None
        if not backend:
            state, reason = "off", "no vector index is configured, so recall is keyword-only"
        else:
            state = "ok"
            health = getattr(self.index, "health", None)
            if health is not None:
                try:
                    fp = health().get("fingerprint")
                except Exception as e:
                    state, reason = "down", f"the vector daemon is not answering ({e})"
                else:
                    if fp is None:
                        state, reason = "idle", "the vector daemon has not started yet (it starts on first use and checks its embedder then)"
                    elif fp.get("state") in ("mismatch", "unreadable"):
                        state, reason = "blocked", fp.get("reason")
        g = self._g()
        try:
            gaps = vector_gaps(g)
        finally:
            g.db.close()
        warnings = []
        if backend and gaps["missing"]:
            n = len(gaps["missing"])
            warnings.append(f"{n} active note{' has' if n == 1 else 's have'} no vector, so recall finds {'it' if n == 1 else 'them'} by keyword only")
        if gaps["stale"]:
            warnings.append(f"{len(gaps['stale'])} vector(s) were made from older text and are out of date")
        if len(gaps["embedders"]) > 1:
            warnings.append("vectors come from more than one embedder (" + ", ".join(f"{k}: {v}" for k, v in sorted(gaps["embedders"].items()))
                            + "); rankings that mix them are unreliable")
        out = {"state": state, "reason": reason, "backend": backend, "active": gaps["active"], "with_vector": gaps["active"] - len(gaps["missing"]),
               "missing": len(gaps["missing"]), "stale": len(gaps["stale"]), "embedders": gaps["embedders"], "fingerprint": fp, "warnings": warnings}
        if agent.lane == "admin":
            out["missing_ids"] = [m["id"] for m in gaps["missing"]][:20]
            out["stale_ids"] = [m["id"] for m in gaps["stale"]][:20]
        return out

    def stats(self, agent) -> dict:
        """The owner sees the whole store. Everyone else sees only how many notes they can read and whether the audit chain is sound: how big the
        store is, how busy its cache is and who is registered would tell an agent about notes and agents it has no business knowing."""
        g = self._g()
        try:
            ok, msg = g.verify_integrity()
            if agent.lane != "admin":
                scan, _capped = self._active_rows(g)
                return {"active": sum(1 for r in scan if self._visible(agent, r)), "chain_ok": bool(ok)}
            by = {r[0]: r[1] for r in g.db.execute("SELECT status, count(*) FROM nodes GROUP BY status")}
            out = {"active": by.get("active", 0), "chain_ok": bool(ok), "cache": self.cache_stats(), "agents": len(self.reg.list_agents()),
                   "vectors": self.vector_status(agent), "scale": scale_status(by.get("active", 0))}
            out["by_status"] = by
            out["chain"] = msg
            return out
        finally:
            g.db.close()
