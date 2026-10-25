"""Usage log for the read path (2.1 of the 0.3 plan): one line of counts per read, for the owner to review. Standard library only.

Each row says which read ran, which agent asked, how many notes came back, the tokens served and the tokens a full fetch of those same notes
would have cost, whether the read asked a question and found nothing (abstained), whether a retrieval lane was down (degraded), how long it
took, and how old the notes were in bands. Never text, never ids, never project names. The log is for review only: nothing in the service
reads it back, so an agent that reads a lot cannot push anything up the rankings.
"""
import json
import math
import os
import statistics
import threading
import time
from pathlib import Path

OPS = ("recall", "search", "timeline", "observations", "context", "node")
AGE_BANDS = (("day", 86_400), ("week", 7 * 86_400), ("month", 30 * 86_400), ("quarter", 91 * 86_400), ("year", 365 * 86_400), ("older", None))
SAVINGS_CAP = 99                          # a card that says 100% saved is claiming the reads cost nothing
BASELINE = "the full text of every note each read returned or listed"
NOTE = "for review only: this log never feeds ranking"
FIELDS = ("ts", "op", "agent", "notes", "served_tokens", "full_tokens", "asked", "abstained", "degraded", "ms", "age")


def age_band(age_seconds: float) -> str:
    """The band a note of this age falls in. A note stamped after the reader's clock (clock skew) counts as a day old."""
    for name, upper in AGE_BANDS:
        if upper is None or age_seconds < upper:
            return name
    return AGE_BANDS[-1][0]


def savings_card(served: int, full: int) -> dict:
    """Tokens served against the baseline, which the card states, with the saving capped below 100%."""
    served, full = max(0, int(served)), max(0, int(full))
    raw = 100 * (full - served) / full if full else 0.0
    pct = max(0, min(SAVINGS_CAP, round(raw)))
    return {"served_tokens": served, "baseline_tokens": full, "baseline": BASELINE, "saved_tokens": max(0, full - served),
            "saved_percent": pct, "capped": raw > SAVINGS_CAP}


def _times(values: list) -> dict:
    """Median, and the time the slowest 5% of reads took at least (nearest rank, so it is a time some read really took)."""
    if not values:
        return {"median": 0.0, "p95": 0.0}
    v = sorted(values)
    return {"median": round(statistics.median(v), 1), "p95": round(v[math.ceil(0.95 * len(v)) - 1], 1)}


class UsageLog:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()

    def record(self, op, agent, *, notes, served_tokens, full_tokens, asked, abstained, degraded, ms, ages) -> None:
        if op not in OPS:
            raise ValueError(f"op must be one of {OPS}")
        bands = {b for b, _ in AGE_BANDS}
        row = {"ts": round(self.clock(), 3), "op": op, "agent": str(agent), "notes": int(notes), "served_tokens": int(served_tokens),
               "full_tokens": int(full_tokens), "asked": bool(asked), "abstained": bool(abstained), "degraded": bool(degraded),
               "ms": round(float(ms), 1), "age": {b: int(n) for b, n in ages.items() if b in bands and n}}
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)

    def rows(self, since=None) -> tuple:
        """(rows, skipped): every well-formed row at or after `since`, and how many lines were damaged."""
        out, skipped = [], 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return [], 0
        for line in lines:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(r, dict) or set(r) != set(FIELDS) or r["op"] not in OPS:
                skipped += 1
                continue
            if since is None or r["ts"] >= since:
                out.append(r)
        return out, skipped

    def summary(self, since=None) -> dict:
        rows, skipped = self.rows(since)
        by_op, by_agent, age = {}, {}, {}
        for r in rows:
            by_op[r["op"]] = by_op.get(r["op"], 0) + 1
            by_agent[r["agent"]] = by_agent.get(r["agent"], 0) + 1
            for b, n in r["age"].items():
                age[b] = age.get(b, 0) + n
        ms = [r["ms"] for r in rows]
        return {"reads": len(rows), "skipped": skipped, "since": since, "first": rows[0]["ts"] if rows else None,
                "last": rows[-1]["ts"] if rows else None, "by_op": by_op, "by_agent": by_agent, "notes": sum(r["notes"] for r in rows),
                "asked": sum(r["asked"] for r in rows), "abstained": sum(r["abstained"] for r in rows),
                "degraded": sum(r["degraded"] for r in rows), "ms": _times(ms),
                "age": {b: age[b] for b, _ in AGE_BANDS if b in age},
                "savings": savings_card(sum(r["served_tokens"] for r in rows), sum(r["full_tokens"] for r in rows)), "note": NOTE}
