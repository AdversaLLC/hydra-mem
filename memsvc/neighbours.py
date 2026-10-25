"""What a write can tell the writer about notes that are already stored: how close they are, and what is different.

Pure functions, no storage. The service decides which notes the writer may see; this module only compares two texts.

Two measures are used on purpose. Refusing a duplicate needs near-identical wording (trigram overlap, see packer), but the notes worth a
second look before writing are paraphrases of the same fact, and those score near zero on trigrams. Measured on the real store: the pair
"extractor takes 2 minutes per row" / "extractor takes 29 minutes for 21 rows" has trigram overlap 0.04 and content-word overlap 0.30, and
the whole store has only 10 non-identical pairs at 0.25 or more, so that floor is quiet.
"""
import re
from collections import Counter

STOP = set("a an and are as at be but by for from has have he her his i in is it its me my not of on or our she so that the their them "
           "they this to us was we were what when where which who why will with you your do does did can could should would about "
           "into over than then there these those how if no yes also just very more most any all some".split())

SIMILAR_FLOOR = 0.25          # content-word overlap at which another note is worth showing
SIMILAR_MAX = 3
MAX_LISTED = 5                # numbers listed per side

_WORD = re.compile(r"[a-z0-9]+")
_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NUMBER = re.compile(r"\d+(?:\.\d+)*")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
NEGATORS = {"not", "no", "never", "cannot", "without", "none", "nor", "neither"}


def content_words(text: str) -> set:
    return {w for w in _WORD.findall(text.lower()) if w not in STOP and len(w) > 1}


def overlap(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _numbers(text: str) -> Counter:
    return Counter(_NUMBER.findall(_THOUSANDS.sub("", text)))


def _negations(text: str) -> int:
    toks = _TOKEN.findall(text.lower().replace("’", "'"))
    return sum(1 for t in toks if t in NEGATORS or t.endswith("n't"))


def differences(new_text: str, old_text: str) -> dict:
    """What the new text changes against the old one that a reader could miss: the numbers on each side that the other lacks, and which side
    has more negations. An empty dict means neither differs. A number that only moved within the text is not a difference."""
    out = {}
    n, o = _numbers(new_text), _numbers(old_text)
    new_only, old_only = n - o, o - n
    if new_only or old_only:
        out["numbers"] = {"new": list(new_only)[:MAX_LISTED], "old": list(old_only)[:MAX_LISTED]}
    a, b = _negations(new_text), _negations(old_text)
    if a != b:
        out["negation"] = "new" if a > b else "old"
    return out
