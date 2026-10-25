"""Remove <private> blocks (and a few other tags that must never be remembered) before anything is stored. Standard library only.

The idea comes from claude-mem's privacy tags: whatever the writer wraps in <private>...</private> is kept out of storage. This is a
separate implementation with one deliberate difference. claude-mem leaves an UNCLOSED <private> tag in place, which stores the secret
whenever the writer forgets the close tag. Here an unclosed <private> fails closed: everything from it to the end is dropped.

Rules (all covered by tests/test_privacy.py):
  * tag names are matched case-insensitively and exactly (<private-key> and <privately> are ordinary text)
  * a close tag pairs with the earliest still-open tag of its name, so nested and mis-nested blocks lose everything they enclose
  * an opener runs to the first '>', and a close tag inside that span is attribute text, not a close
  * other unclosed tags stay (they are prose such as "explain how <system-reminder> works")
  * input without a removable span is returned byte for byte
The scan is linear: a stack per tag name, positions of '>' found once, no backtracking.
"""
import re
from bisect import bisect_left

TAGS = ("private", "memory-context", "system-reminder", "system_instruction", "system-instruction", "persisted-output")
_TAG_SET = frozenset(TAGS)
_NAME = re.compile(r"<(/?)([A-Za-z_][A-Za-z0-9_-]*)")
_CLOSE_END = re.compile(r"\s*>")


def strip_private(text: str) -> tuple:
    """Return (clean_text, {"removed": spans removed, "unclosed_private": bool}). Raises TypeError for non-strings."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    info = {"removed": 0, "unclosed_private": False}
    if "<" not in text:
        return text, info
    n = len(text)
    gts = [i for i, ch in enumerate(text) if ch == ">"]

    def next_gt(j: int) -> int:
        k = bisect_left(gts, j)
        return gts[k] if k < len(gts) else n

    opens = {}                                   # name -> [list of (start, attr_end), head index]
    spans = []
    i = 0
    while True:
        i = text.find("<", i)
        if i == -1:
            break
        m = _NAME.match(text, i)
        if not m:
            i += 1
            continue
        closing, name = bool(m.group(1)), m.group(2).lower()
        if name not in _TAG_SET:
            i = m.end()
            continue
        if closing:
            e = _CLOSE_END.match(text, m.end())
            if not e:
                i = m.end()
                continue
            entry = opens.get(name)
            if entry and entry[1] < len(entry[0]) and i >= entry[0][entry[1]][1]:
                start = entry[0][entry[1]][0]
                entry[1] += 1
                spans.append((start, e.end()))
                info["removed"] += 1
            i = e.end()
            continue
        entry = opens.setdefault(name, [[], 0])
        entry[0].append((i, next_gt(m.end())))
        i = m.end()                              # continue after the name only: a tag inside this opener's attribute is still a tag
    entry = opens.get("private")
    if entry and entry[1] < len(entry[0]):       # an opener that never closed: fail closed
        spans.append((entry[0][entry[1]][0], n))
        info["removed"] += 1
        info["unclosed_private"] = True
    if not spans:
        return text, info
    spans.sort()
    out, cursor = [], 0
    for start, end in spans:
        if start > cursor:
            out.append(text[cursor:start])
        cursor = max(cursor, end)
    out.append(text[cursor:])
    merged = ""
    for piece in out:
        if merged and piece and merged[-1].isspace() and piece[0].isspace():
            piece = piece.lstrip(" \t")           # close the gap a removed block leaves, one space only
        merged += piece
    return merged.strip(), info
