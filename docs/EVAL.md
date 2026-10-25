# What was measured, and how much it proves

All numbers are from 2026-10-22, on a scratch COPY of the real graph and vector store (242 active notes, ONNX MiniLM
embeddings, CPU only). Questions come from three files kept with the gsd-hydra-learn skill: `gold.sample.jsonl` (20, easy),
`gold.hard.jsonl` (16, paraphrased, none of the note's own words) and `gold.negative.jsonl` (14 questions whose right answer is
NOTHING). Reproduce with `python3 -I tools/run_sweep.py` and `tools/compare_stock.py`.

## 1. Abstention: the service should say "I don't know"

Positives are the 36 answerable questions; "negatives answered" is how many of the 14 unanswerable ones still got notes back.
Floor = minimum vector similarity; coverage = share of the query's content words a note must contain to count as keyword evidence.

| floor | coverage | hit@1 | hit@5 | MRR | negatives answered |
|---|---|---|---|---|---|
| 0.00 | 0.00 | 0.89 | 0.94 | 0.91 | **14 / 14** |
| 0.25 | 0.00 | 0.89 | 0.97 | 0.92 | 9 / 14 |
| **0.25** | **0.34** | 0.86 | 0.94 | 0.90 | **1 / 14** |
| 0.30 | 0.34 | 0.83 | 0.92 | 0.87 | 1 / 14 |
| 0.35 | 0.34 | 0.83 | 0.89 | 0.85 | 0 / 14 |
| 0.50 | 0.34 | 0.67 | 0.67 | 0.67 | 0 / 14 |

Reading it:
- With no rule the service answers every out-of-corpus question with its nearest junk. That is the failure small models trust most.
- The vector floor alone does not fix it (9 of 14 still answered): the keyword path matches one incidental word. Coverage is what fixes it.
- The default (floor 0.25, coverage 0.34) costs about one answerable question out of 36 and removes 13 of 14 junk answers.
- Caveat: 14 negatives and 36 positives. The large effect (14 to 1) is real; the exact best floor is within noise (one question moves a row).

## 2. No regression against the stock recall

Same 36 positives, service (0.25 / 0.34) against stock hydra-graph hybrid recall:

| | hit@1 [95% CI] | hit@5 | MRR [95% CI] |
|---|---|---|---|
| stock | 0.83 [0.69-0.94] | 0.94 | 0.89 [0.80-0.96] |
| service | 0.86 [0.72-0.97] | 0.94 | 0.90 [0.80-0.97] |

Paired: service better on 2 questions at hit@1, stock better on 1 (sign test p = 1.0). Conclusion: no measurable difference in
answer quality; the gain is the ability to abstain. Not shown: any benefit of the stopword filtering on its own.

## 3. Latency (this CPU)

- Embedding one short question: about 0.3 s, a fixed cost (thread settings made no difference). A repeated question hits the daemon's
  embedding cache (under 0.1 s), and an unchanged repeat hits the service's cache keyed to the audit-chain state.
- A fresh worker per query (the stock design) added 1.5 to 3 s; the persistent daemon removes that.

## 4. Geometry report on the real notes

252 vectors in the store (some are superseded duplicates of the same text): 20 near-duplicate groups at cosine 0.95, the same 20 at 0.90,
21 at 0.85, so the boundary between duplicates and everything else is clean. Mostly one fact stored under two projects. The
"most unusual notes" ranking is flat (about 1.2x the mean): with fewer notes than dimensions (252 < 384) leverage carries little
signal, as the paper's own analysis predicts. It will become informative as the store grows; today only the duplicate finding is useful.
The report never archives or deletes anything.

## What is NOT shown

- A live local model using the service end to end (see `docs/LOCAL_MODEL_TEST.md` if it has been run).
- Any effect on answer correctness downstream; these are retrieval and abstention measurements only.
- Behaviour at 10k or 100k notes (the geometry maths says it is cheap; not measured here).
- Stale-fact handling under real change (supersession is unit-tested, not evaluated on real history).

## 5. `memctl eval` and the 5-fold check (2026-10-23, 0.3 plan item 2.2)

Run: `python3 -I memctl eval` (vectors on, a copy of the live store: 325 active notes; the copy is kept under the system temp folder).
Questions: the same 36 answerable and 14 unanswerable gold questions, plus 27 must-not questions made from the store (rejected, superseded,
quarantined and pending notes, each asked for by its own title; none may come back).

| served defaults (floor 0.25, coverage 0.34) | value [95% bootstrap interval] |
|---|---|
| recall@5 | 0.93 [0.85-1.00] |
| MRR | 0.88 [0.78-0.96] |
| hit@1 | 0.83 |
| unanswerable questions answered | 1 of 14 |
| must-not leaks | 0 of 27 |

The 5-fold check (stratified by kind; each fold picks the best of 18 floor/coverage settings on the other four folds by
MRR + share of unanswerable left alone - 2 x share of leaks, and is scored on its own questions): the defaults were picked in 0 of 5 folds,
yet the held-out choices did WORSE than the defaults, by 0.034 utility per question [-0.075, -0.003] (paired bootstrap, 77 questions).
Picking a setting from these questions overfits them; nothing here supports changing the defaults.

Caveat: the defaults themselves were chosen in October by looking at these same 50 questions, so their held-out score is not truly held
out and their edge is partly flattering. Only new questions (from real use, written before looking at results) can settle that. Leaks were 0
at every setting tried: the visibility rules, not the floors, keep hidden notes out.
