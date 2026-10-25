"""Client for the persistent embedding daemon: starts it on demand, restarts it if it dies, times out instead of hanging.
Standard library only. The service treats this tier as optional: any VectorError degrades recall to keyword search.
"""
import json
import os
import select
import subprocess
import threading
from pathlib import Path

DAEMON = Path(__file__).with_name("vector_daemon.py")


class VectorError(Exception):
    pass


class ChromaDaemonIndex:
    backend = "onnx"

    def __init__(self, python: str, chroma_path: str, daemon=None, timeout: float = 60.0):
        self.python, self.chroma_path = python, chroma_path
        self.daemon = str(daemon or DAEMON)
        self.timeout = timeout
        self._proc = None
        self._lock = threading.Lock()
        self.fingerprint = None                                 # what the daemon reported about its embedder the last time it started

    # ---- process handling
    def _env(self) -> dict:
        env = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", ""), "ANONYMIZED_TELEMETRY": "False",
               "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        return env

    def _readline(self, timeout: float) -> str:
        r, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not r:
            raise VectorError("vector daemon timed out")
        line = self._proc.stdout.readline()
        if not line:
            raise VectorError("vector daemon closed its pipe")
        return line

    def _start(self):
        self._kill()
        self._proc = subprocess.Popen([self.python, "-I", self.daemon, self.chroma_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1, env=self._env())
        try:
            ready = json.loads(self._readline(self.timeout))
        except (VectorError, ValueError) as e:
            self._kill()
            raise VectorError(f"vector daemon failed to start: {e}")
        if not ready.get("ready"):
            self._kill()
            raise VectorError("vector daemon did not report ready")
        self.fingerprint = ready.get("fingerprint")

    def _kill(self):
        p, self._proc = self._proc, None
        if p is not None and p.poll() is None:
            p.kill()
        if p is not None:
            try:
                p.wait(timeout=2)
            except Exception:
                pass
            for f in (p.stdin, p.stdout):
                try:
                    f.close()
                except Exception:
                    pass

    def close(self):
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
            self._kill()

    def _call(self, req: dict) -> dict:
        with self._lock:
            for attempt in (1, 2):                              # one automatic restart if the process died
                if self._proc is None or self._proc.poll() is not None:
                    self._start()
                try:
                    self._proc.stdin.write(json.dumps(req) + "\n")
                    self._proc.stdin.flush()
                    resp = json.loads(self._readline(self.timeout))
                except (VectorError, BrokenPipeError, OSError, ValueError) as e:
                    self._kill()
                    if attempt == 2:
                        raise VectorError(f"vector daemon call failed: {e}")
                    continue
                if resp.get("error"):
                    raise VectorError(resp["error"])
                return resp
        raise VectorError("unreachable")

    def health(self) -> dict:
        """What is known without starting anything: the daemon starts on first use, and asking for status must not cost seconds."""
        p = self._proc
        return {"backend": self.backend, "running": p is not None and p.poll() is None, "fingerprint": self.fingerprint}

    # ---- the index interface the service expects
    def upsert(self, docs):
        self._call({"op": "upsert", "docs": docs})

    def delete(self, ids):
        self._call({"op": "delete", "ids": list(ids)})

    def geometry(self, lam_rel=0.05, dup=0.95, top=10):
        r = self._call({"op": "geometry", "lam_rel": lam_rel, "dup": dup, "top": top})
        r.pop("ok", None)
        return r

    def query(self, text, project, limit):
        return [(h["id"], h["score"]) for h in self._call({"op": "query", "text": text, "limit": limit})["hits"]]
