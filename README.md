# memsvc: one private memory for Claude, local models and any agent

A small local service that every agent shares. It runs only on this machine (127.0.0.1), uses no cloud and no paid service, and
never calls a language model on its own. It sits on top of hydra-graph (the stack's SQLite memory with its screening rules,
HMAC audit chain and rollback), so there is still one store, one audit trail.

What it adds: per-agent identity and lanes, scopes enforced on every retrieval path, answers sized for small context windows,
an explicit "nothing relevant stored" signal, supersession, scratch notes that expire, a dashboard and a command line for the owner.
Since 0.2.0 it also has claude-mem's read path and viewer, ported without installing it: see "Read memory in three steps" below and
`docs/CLAUDE-MEM-PARITY.md` (what was ported, what was not, and why).

## Quick start (nothing here is started for you)

```bash
cd ~/ai-memory
python3 -I memctl init                                    # prints the OWNER token once: store it in a password manager
python3 -I memctl agent create claude --lane trusted      # prints that agent's token once
python3 -I memctl agent create local-qwen --lane probation
python3 -I memctl serve --port 8765                       # 127.0.0.1 only; vectors on if ~/.hydra/config.json points at the RAG venv
```

Open `http://127.0.0.1:8765/`, paste the owner token, and use Review / Recall / Agents / Compaction. To run it at login, see
`contrib/memsvc.service` (a hardened systemd user unit; copy it yourself).

State lives in `~/.hydra/memsvc/` (agent tokens as hashes only, access log without bodies). Notes live in the existing `graph.db`.

## Lanes

| Lane | Writes | Who sees them |
|---|---|---|
| trusted | become active notes at confidence U | every agent |
| probation | stored as pending, held for the owner | nobody until the owner approves (small local models start here) |
| readonly | refused | n/a (can still recall) |
| admin (owner) | everything; approve, reject, verify, create agents | everyone |

Confidence can only be raised by the owner. Notes that look like instructions or secrets are quarantined by hydra-graph's screen
whatever the lane. Private notes (`scope: private`) and project scopes are checked on the keyword, vector and graph-neighbour paths alike.

## Read memory in three steps (claude-mem's search, timeline, get_observations)

Reading everything is what burns a small model's window. The three steps cost little at each stage:

1. `memory_search` (CLI `memctl search "query"`): an index, one line per note with a short `#handle`, kind, confidence, project, title,
   snippet and its token cost. Filters: kind, agent, project, tag, date range; no query lists the newest. It says so when nothing matches.
   Each hit shows which lane found it (kw, vec, both, graph); an answer made without the vector search says so.
2. `memory_timeline` (`memctl timeline --anchor HANDLE`): the notes just before and after a hit, so you see what was going on.
3. `memory_get_observations` (`memctl get HANDLE ...`): full text for only the few you picked (at most 20, optional token limit).

Also: `memory_workflow` (the guide above, `memctl workflow`), `memory_context` (`memctl context --project P`: what to show a model at the
start of a session, with reported "economics"), `GET /v1/sessions` and a `session` id on `memory_add` to group a run's notes (tag a
wrap-up `session-summary`), and `work_tokens` on `memory_add` for how much effort a note represents (a claim, shown as one).
Text inside `<private>...</private>` is removed before anything is stored; an unclosed tag drops everything after it.
`memctl doctor` is a read-only health check. Tool sets for small models: `GET /v1/tools?set=basic|progressive|all`, and `MEMSVC_TOOLS` for the MCP bridge.
Writes tell the writer what is already there: up to three similar notes it may see, and, when a refused duplicate differs in a number or a
negation, how to replace the old note (`memory_supersede` takes the `#handle`). The vector daemon pins a fingerprint of its embedder and turns
itself off, with a reason, if the embedder or the stored vectors change. `stats`, `memctl doctor` and the viewer warn when notes lack vectors and
when the store nears the 20,000-note listing scan (results carry `scan_capped`). MCP tools carry read-only and destructive hints. A non-owner's
`stats` shows only how many notes it can read.
Current focus: one short statement of what is being worked on, per project (no `--project` is the global one), that survives between sessions.
`memctl focus` shows it, `memctl focus set TEXT...` replaces it (the old note is kept, superseded; the same text again changes nothing),
`memctl focus done` ends it. It is an ordinary note tagged `focus`, so lanes, scopes and the audit chain apply (a small model's focus waits for the
owner's approval), and `memory_context` / `memctl context` open with it. HTTP: `GET` or `POST /v1/focus`, `POST /v1/focus/done`. Not yet a
tool in the MCP bridge. Text is limited to 600 characters and `<private>` spans are removed.
Owner: a rejected note is kept, not deleted; the Trash tab, `memctl trash` and `memctl restore ID` bring one back. `MEMSVC_PROJECT=name` makes one
MCP client default to a project (opt-in; an explicit project wins and `*` reads every project).

## Use it from a local model

- **Any agent code:** `from memsvc.client import MemoryClient` (one stdlib file). `recall(query, budget_tokens=800)` returns the best
  notes plus a ready-to-paste context block, and `sufficient: false` when nothing relevant is stored: tell the model to say it doesn't know.
- **Any chat model, no tool-calling needed:** `examples/local_agent.py` (JSON-mode loop). Live check against Qwen3-4B on this CPU:
  `docs/LOCAL_MODEL_TEST.md` (3 of 4 scenarios passed; the one miss is explained there).
- **Frameworks with function calling:** `GET /v1/tools` returns OpenAI-style schemas.
- **MCP clients (Claude Desktop, Claude Code, local frontends):** command `python3`, args `["-I", "/home/adversa/ai-memory/memsvc/mcp_bridge.py"]`,
  env `MEMSVC_TOKEN=<that agent's token>`. They all share the same lanes and audit trail.

## API (all JSON over HTTP; bearer token; `/v1/health` and the page itself need none)

`POST /v1/recall` `/v1/add` `/v1/supersede` `/v1/link` `/v1/search` `/v1/timeline` `/v1/observations` `/v1/context` `/v1/focus` `/v1/focus/done`;
`GET /v1/node` `/v1/pending` `/v1/stats` `/v1/tools` `/v1/sessions` `/v1/focus`.
Owner only: `GET /v1/review` `/v1/trash` `/v1/agents` `/v1/compact-report` `/v1/usage`; `POST /v1/approve` `/v1/reject` `/v1/restore` `/v1/verify` `/v1/agents` `/v1/agents/lane` `/v1/agents/revoke`.

Web defences: loopback bind only; Host and Origin checks (DNS-rebinding and cross-site requests refused); no CORS headers; JSON-only POSTs;
64 KB body cap; per-agent rate limits; errors never carry internals; the access log never holds a body or a token.

## What was measured (details and caveats: `docs/EVAL.md`)

- Answering questions that have no answer: without the new rule the service returned notes for 14 of 14 out-of-corpus questions; with the
  default (vector floor 0.25, keyword coverage 0.34) it did for 1 of 14, at a cost of about one answerable question in 36. No difference
  from stock recall on the answerable questions.
- 492 tests in 29 suites pass; the 12 geometry tests skip without numpy. `tools/mutation_check.py` holds 110 deliberate breaks (the 20 for Phase 2 all caught). `tests/test_never_returned.py` is a standing matrix of hidden notes against every agent and read path. The new read path was built test-first and mutation-checked
  (`docs/CLAUDE-MEM-PARITY.md`, Evidence), and the viewer was exercised in a real browser on throwaway data.

## Open the viewer

`memctl ui` (or the Memory icon in the app grid) opens http://127.0.0.1:8765/ already signed in as the owner. The sign-in is kept in
`~/.hydra/memsvc/ui.token` (mode 600). If it is missing or stale, a fresh owner token is made and the old `admin.token` is moved aside,
never deleted. The token travels in the address fragment, which browsers never send to a server; the page wipes it from the address bar.
Nobody has to keep the once-shown `memctl init` token to get in.

## Count, then check (0.3 plan, Phase 2)

- **Usage log**: `memctl census` (reads the file; no token or server needed) or the owner-only `GET /v1/usage`. Every read writes one line
  of counts to `usage.jsonl` (mode 600): which read, which agent, how many notes, tokens served against the full text of those notes,
  whether a question found nothing, whether a lane was down, milliseconds, and how old the notes were (day, week, month, quarter, year, older).
  No text, no ids, no project names. It is for review only: nothing reads it back, so it cannot move rankings. The savings card states its
  baseline and never shows more than 99%.
- **`memctl eval`**: answerable, unanswerable and must-not-retrieve questions (the must-not ones are also made from rejected, superseded,
  quarantined and pending notes, each asked for by its own title), always on a COPY of the store. Reports recall@5 and MRR with bootstrap
  intervals, how many unanswerable questions got an answer, any must-not leak, and a 5-fold check of the served floors: each fold picks a
  setting on the other folds and is scored on its own questions, then a paired bootstrap compares that with the defaults.

## Honest limits

- Retrieval and abstention are measured; downstream answer quality is not. Samples are small (36 answerable and 14 unanswerable questions; one live run per scenario).
- Geometry-based "unusual note" ranking is flat while there are fewer notes than embedding dimensions (252 < 384); the duplicate finding is the useful part today.
- The model's own resistance to injected text is untested: lanes keep hostile notes away from it, which is the actual control.
- The local-model check used one 4B model; a 1B model, other models and long sessions are untested.
- Tokens are bearer secrets on a local port: keep them out of shared shells and logs. A speed bump for local attackers is not a sandbox.

## Layout

`memsvc/` service, server, registry, packer, privacy, render, vector client and daemon, geometry, client, CLI, MCP bridge, viewer. `tools/` evals and demo.
`examples/` local-model loop. `contrib/` systemd unit and the Modelfile used for the live check. `tests/` (5,140 lines). Package: 3,237 lines of Python and a 412-line page.
