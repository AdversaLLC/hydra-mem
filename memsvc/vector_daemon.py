#!/usr/bin/env python3
"""Persistent embedding daemon: one JSON object per line on stdin, one JSON reply per line on stdout.

Run it with the RAG virtualenv's python (needs chromadb and its cached ONNX model). It loads the model once, so a query
costs milliseconds instead of the 1.5-3 seconds of starting a fresh worker. Same layout as hydra-graph's own store: one
collection per project plus a global mirror (hg_global) that holds everything. Anonymous telemetry is off. No network
listener: it only talks over its own pipes.

  vector_daemon.py CHROMA_PATH
Ops: info | upsert {docs:[{id,text,project,meta}]} | query {text,limit} | delete {ids} | count | geometry {lam_rel,dup,top}

At start it embeds a fixed canary text and compares it with the copy pinned beside the vectors (memsvc/fingerprint.py), and re-embeds a few
stored notes to see that the store still agrees with this embedder. If either fails, upsert, query and geometry answer with the reason instead
of mixing vectors from two models; info, count and delete keep working.
"""
import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fingerprint  # noqa: E402


def main() -> int:
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    import chromadb
    import chromadb.utils.embedding_functions as ef
    embed = ef.DefaultEmbeddingFunction()
    client = chromadb.PersistentClient(path=os.path.expanduser(sys.argv[1]), settings=chromadb.Settings(anonymized_telemetry=False))

    def col(project):
        name = "hg_" + "".join(c if c.isalnum() else "_" for c in (project or "global"))[:50]
        return client.get_or_create_collection(name, embedding_function=embed, metadata={"hnsw:space": "cosine"})

    qcache, stats = OrderedDict(), {"hits": 0}

    def query_embedding(text):
        """One short text costs about 0.3 s to embed on this CPU; agents repeat questions, so remember the vectors."""
        if text in qcache:
            stats["hits"] += 1
            qcache.move_to_end(text)
            return qcache[text]
        emb = [float(x) for x in embed([text])[0]]
        qcache[text] = emb
        while len(qcache) > 512:
            qcache.popitem(last=False)
        return emb

    def store_disagrees(vec):
        """None if the stored vectors agree with this embedder (or there are none), else why not."""
        try:
            g = col(None)
            if g.count() == 0:
                return None
            got = g.get(limit=fingerprint.SAMPLE, include=["embeddings", "documents"])
            docs = [d for d in got["documents"] if d]
            fresh = [[float(x) for x in e] for e in embed(docs)]
            r = fingerprint.spot_check([([float(x) for x in st], fr) for st, fr in zip(got["embeddings"], fresh)])
            return None if r["ok"] else r["reason"]
        except Exception as e:
            return f"the vector store cannot be read with this embedder ({type(e).__name__}: {e})"

    def self_check():
        vec = [float(x) for x in embed([fingerprint.CANARY])[0]]
        pin = Path(os.path.expanduser(sys.argv[1])) / fingerprint.FILE_NAME
        state = fingerprint.inspect(pin, vec, "onnx")
        if state["state"] in ("match", "unpinned"):
            why = store_disagrees(vec)
            if why:
                return {"state": "mismatch", "cosine": state["cosine"], "reason": why}
            if state["state"] == "unpinned":
                fingerprint.save(pin, fingerprint.record(vec, "onnx", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                return {"state": "pinned", "cosine": None, "reason": None}
        return state

    check = self_check()
    blocked = check["reason"] if check["state"] in ("mismatch", "unreadable") else None
    print(json.dumps({"ready": True, "backend": "onnx", "fingerprint": check}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            op = req.get("op")
            if blocked and op in ("upsert", "query", "geometry"):
                out = {"error": "vector search is off: " + blocked}
            elif op == "info":
                out = {"ok": True, "backend": "onnx", "fingerprint": check, "telemetry": bool(client.get_settings().anonymized_telemetry), "collections": len(client.list_collections()),
                       "query_cache_hits": stats["hits"], "query_cache_size": len(qcache)}
            elif op == "upsert":
                by = {}
                for d in req.get("docs", []):
                    if isinstance(d, dict) and d.get("id") and d.get("text"):
                        by.setdefault(d.get("project") or "global", []).append(d)
                n = 0
                for project, docs in by.items():
                    kw = dict(ids=[d["id"] for d in docs], documents=[d["text"] for d in docs], metadatas=[d.get("meta") or {"_": 1} for d in docs])
                    col(project).upsert(**kw)
                    if project != "global":
                        col("global").upsert(**kw)
                    n += len(docs)
                out = {"ok": True, "upserted": n}
            elif op == "query":
                g = col(None)
                n = g.count()
                if n == 0:
                    out = {"ok": True, "hits": []}
                else:
                    r = g.query(query_embeddings=[query_embedding(str(req.get("text", "")))], n_results=max(1, min(int(req.get("limit", 10)), n)))
                    ids, dist = r["ids"][0], r["distances"][0]
                    out = {"ok": True, "hits": [{"id": i, "score": 1.0 - float(d)} for i, d in zip(ids, dist)]}
            elif op == "delete":
                ids = [i for i in req.get("ids", []) if isinstance(i, str)]
                if ids:
                    for c in client.list_collections():
                        client.get_collection(c.name if hasattr(c, "name") else c).delete(ids=ids)
                out = {"ok": True, "deleted": len(ids)}
            elif op == "count":
                out = {"ok": True, "count": col(None).count()}
            elif op == "geometry":
                import numpy as np
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import geometry
                got = col(None).get(include=["embeddings"], limit=200000)
                if len(got["ids"]) == 0:
                    out = {"ok": True, "n": 0, "d": 0, "coherence": 0.0, "mean_leverage": 0.0, "outliers": [], "duplicate_groups": []}
                else:
                    out = {"ok": True, **geometry.report(list(got["ids"]), np.array(got["embeddings"], dtype=float), lam_rel=float(req.get("lam_rel", 0.05)),
                                                          dup_threshold=float(req.get("dup", 0.95)), top=int(req.get("top", 10)))}
            else:
                out = {"error": f"unknown op {op!r}"}
        except Exception as e:                                  # never die on one bad request
            out = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
