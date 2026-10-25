"""Agent registry: bearer tokens, lanes, read/write scopes and rate limits. Standard library only.

Every caller of the memory service is an agent with a name, a lane and scopes:

  trusted    writes become active notes that every agent can recall (still start at confidence U)
  probation  writes are stored but NOT recalled by anyone until the owner approves them (small local models start here)
  readonly   may recall, may not write
  admin      the owner; the only identity that can approve, reject, verify or create agents

Tokens are random, shown once, and stored only as SHA-256 hashes in a 0600 file.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LANES = ("trusted", "probation", "readonly")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
RESERVED = {"admin"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Agent:
    name: str
    lane: str
    read: tuple = ("*",)
    write: tuple = ("*",)
    writes_per_hour: int = 60

    def can_read(self, project) -> bool:
        return project is None or "*" in self.read or project in self.read

    def can_write(self, project) -> bool:
        return "*" in self.write or (project is not None and project in self.write)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


class Registry:
    def __init__(self, path, admin_path):
        self.path = Path(path)
        self.admin_path = Path(admin_path)
        self._lock = threading.Lock()
        self._data = self._load()
        self._mtime = self._stat()

    def _stat(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _refresh(self) -> None:
        """Pick up changes made by another process (for example the command line while the server runs)."""
        m = self._stat()
        if m != self._mtime:
            with self._lock:
                self._data = self._load()
                self._mtime = m

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and isinstance(d.get("agents"), dict) else {"agents": {}}
        except (OSError, ValueError):
            return {"agents": {}}

    def _save(self) -> None:
        _atomic_write(self.path, json.dumps(self._data, indent=1, sort_keys=True))
        self._mtime = self._stat()

    def create_agent(self, name: str, lane: str, read=("*",), write=("*",), writes_per_hour: int = 60) -> str:
        if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED:
            raise ValueError("agent name must be 2-32 chars of a-z, 0-9, - or _, and not reserved")
        if lane not in LANES:
            raise ValueError(f"lane must be one of {LANES}")
        with self._lock:
            self._data = self._load()                      # re-read inside the lock: no lost updates between processes
            if name in self._data["agents"]:
                raise ValueError(f"agent {name!r} already exists")
            token = secrets.token_urlsafe(32)
            self._data["agents"][name] = {"hash": _hash(token), "lane": lane, "read": list(read), "write": list(write),
                                          "wph": int(writes_per_hour), "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                          "revoked": False}
            self._save()
            return token

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) < 16:
            return None
        self._refresh()
        h = _hash(token)
        try:
            if hmac.compare_digest(h, json.loads(self.admin_path.read_text(encoding="utf-8")).get("hash", "")):
                return Agent("admin", "admin", ("*",), ("*",), 10_000)
        except (OSError, ValueError):
            pass
        hit = None
        for name, rec in self._data["agents"].items():
            if hmac.compare_digest(h, rec.get("hash", "")) and not rec.get("revoked"):
                hit = Agent(name, rec["lane"], tuple(rec.get("read", ["*"])), tuple(rec.get("write", ["*"])), int(rec.get("wph", 60)))
        return hit

    def ensure_admin(self):
        """Create the admin token once. Returns the token (shown once) or None if one already exists."""
        with self._lock:
            if self.admin_path.exists():
                return None
            token = secrets.token_urlsafe(32)
            _atomic_write(self.admin_path, json.dumps({"hash": _hash(token)}))
            return token

    def set_lane(self, name: str, lane: str) -> None:
        if lane not in LANES:
            raise ValueError(f"lane must be one of {LANES}")
        with self._lock:
            self._data = self._load()
            if name not in self._data["agents"]:
                raise KeyError(name)
            self._data["agents"][name]["lane"] = lane
            self._save()

    def revoke(self, name: str) -> None:
        with self._lock:
            self._data = self._load()
            if name not in self._data["agents"]:
                raise KeyError(name)
            self._data["agents"][name]["revoked"] = True
            self._save()

    def list_agents(self) -> list:
        self._refresh()
        return [{"name": n, "lane": r["lane"], "read": r["read"], "write": r["write"], "writes_per_hour": r.get("wph", 60),
                 "created": r.get("created"), "revoked": bool(r.get("revoked"))} for n, r in sorted(self._data["agents"].items())]


class RateLimiter:
    """Sliding window: at most `limit` allowed events per key per `window` seconds. Denied attempts are not recorded."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int | None = None) -> bool:
        now = self.clock()
        cap = self.limit if limit is None else limit
        with self._lock:
            q = self._events[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= cap:
                return False
            q.append(now)
            return True
