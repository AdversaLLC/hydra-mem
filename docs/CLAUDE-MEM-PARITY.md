# Parity with claude-mem: what was ported, what was not, and where memsvc differs

Written 2026-10-22, updated the same day. Sources: claude-mem's README (github.com/thedotmack/claude-mem), its source at commit `bbb3379`
(2026-10-20) and its older documentation repo (github.com/thedotmack/claude-mem-docs, last commit 2025-09-21, describing the v3.x design),
all read from shallow clones in scratch space; licence Apache-2.0 for the source and MIT for the docs (GitHub). **claude-mem was not
installed or run**, and no code was copied: the behaviour was read, then re-implemented in this project's own terms. So every comparison
below is from reading its code and docs (label S), not from running it side by side.

## Ported

| claude-mem | memsvc | How it differs |
|---|---|---|
| `search`: index with ids; filters `type`, `dateStart`, `dateEnd`, `project`, `platformSource`, `offset`, `orderBy` | `memory_search`, `POST /v1/search`, `memctl search` | claude-mem's spellings are accepted as aliases. Each row carries a short `#handle` and its token cost; the answer says `sufficient: false` when nothing matches. Filters are applied while candidates are collected, so a match ranked far down is still found (a bug in the first version, found by review and fixed). |
| `timeline`: `anchor` or `query`, `depth_before`, `depth_after` | `memory_timeline`, `POST /v1/timeline`, `memctl timeline` | Hidden neighbours are skipped and not counted; an unknown anchor and a hidden one give the identical empty answer. Scope defaults to the anchor's project. Also: a whole-session view. |
| `get_observations`: batch of ids | `memory_get_observations`, `POST /v1/observations`, `memctl get` | At most 20 per call, optional `max_tokens` that stops the batch and lists what was skipped. A note you may not read is reported exactly like a missing one. |
| `important_workflow` (the three-step guide) | `memory_workflow`, `memctl workflow` | Under 1,600 characters, so a small model reads all of it. |
| `dumb_search` (one call) | `memory_recall` (already existed) | Rank-fused keyword and vector search with the abstention rule. |
| `save_memory` | `memory_add` (already existed) | Now also takes `session` and `work_tokens`. |
| `session_start_context` | `memory_context`, `POST /v1/context`, `memctl context`, the Context tab | Latest session summaries, then corrections, failed paths and decisions, then the rest, inside a token budget; project chains allowed. |
| `<private>` tags | `memsvc/privacy.py`, applied to title, body and tags on every write | Stricter: an unclosed `<private>` tag fails closed (everything after it is dropped). claude-mem leaves an unclosed tag in place, which stores the secret. Removal happens before the duplicate check, the keyword index and the vectors. |
| Stripping of `<system-reminder>`, `<persisted-output>`, `<system_instruction>`, `<claude-mem-context>` | same tag family in `privacy.py` (`memory-context` stands in for claude-mem's own tag) | Balanced blocks only, as in claude-mem. A note that re-saves a recalled block is caught by the near-duplicate check, which is weaker than tag stripping. |
| Token economics (read tokens vs discovery tokens, savings) | `economics` in the context answer and the Context tab | "Discovery tokens" come from `work_tokens`, which the writing agent reports. The numbers are shown as agent-reported, and with none reported there is no savings claim. |
| Sessions and session summaries | `session` id on `memory_add`, `GET /v1/sessions`, Sessions tab, tag `session-summary` | The service never calls a model, so a summary is a note an agent writes (tagged `session-summary`), not one generated for it. |
| Viewer: live feed, type filter, cards with expandable detail, sessions, context preview, theme, phone layout | tabs Feed, Search, Sessions, Context (plus the owner tabs that already existed) | Live updates poll every 5 seconds while the tab is visible and stop when you leave it; claude-mem streams. |
| Smart Trash and `claude-mem restore` (docs repo, v3.x) | Trash tab, `memctl trash`, `memctl restore ID`, `GET /v1/trash`, `POST /v1/restore` | Owner only. Reject keeps the note and its audit trail; restore brings it back and re-indexes it. A note nobody had vetted (a probation agent's) is restored as an approval, a trusted agent's note keeps exactly its old standing, and a rejected replacement replaces its target as approving it would have. |
| Project detection (a memory space per project, docs repo) | `MEMSVC_PROJECT` for the MCP bridge: reads and writes that name no project use it; an explicit project wins and `*` widens | Opt-in on purpose. memsvc does not infer a project from the working directory: that exact habit made recall look broken earlier in this stack. |
| `doctor` / `status` | `memctl doctor` | Read-only. Checks modes of the state folder and files, that the owner token file holds a hash, the audit key and chain, the vector tier, disk and whether the service answers. It never prints a token or hash, never repairs, and refuses to open the graph if the audit key is missing (opening it would create a new one). |
| Observation ids as citations | `#handle` (eight hex characters) | Small models mistype long ids; handles are accepted anywhere an id is. |

## Not ported, and why

- **Automatic capture by hooks and AI compression of tool use.** Needs a model, and auto-captured text is the main injection path into a
  memory. memsvc is written to on purpose by agents. hydra-mem's hooks already capture sessions locally in their own store.
- **Hosted observer, sign-in, Cloud Sync, TeamBrain.** They send data off the machine. This system is local only by requirement.
- **`get_tool_uses` (raw tool input and output).** memsvc does not store tool I/O.
- **Folder-level CLAUDE.md generation.** It writes instruction files from memory, which is an injection route.
- **Modes and observation languages, smart search/unfold/outline, corpora.** Model-dependent or a different product area
  (code navigation, knowledge corpora); graphify and hydra-graph cover those jobs here.
- **The v3.x ranking formula and settings file** (30-day half-life, weights for recency, project, type and quality). The weights have no
  stated basis; the plan is to measure first (see the plan page).
- **Harness installers (13 hosts).** Not applicable: the MCP bridge covers MCP clients, `GET /v1/tools` covers function-calling frameworks.

## Where memsvc is better, measured here

- **Visibility on every path.** Search, timeline, fetch, context and sessions all use the same `_visible` rule as recall (lanes,
  project scope, private scope, expiry, status). A mutation check removes each guard in turn and the tests fail every time; `tests/test_never_returned.py` now also runs every kind of hidden note against every agent and read path.
- **Says "nothing relevant" instead of guessing** (`sufficient`), carried through to the text views and the viewer.
- **Fail-closed private tags**, stripped before any index sees the text.
- **Token budgets everywhere:** the index shows the cost of fetching, fetch and context take limits and report what they dropped.
- **Fewer tools for small models:** `GET /v1/tools?set=basic|progressive|all`, and `MEMSVC_TOOLS` narrows the MCP bridge the same way.

## Where claude-mem is ahead (honest)

Automatic capture and model-written summaries; streaming updates; a settings modal in the viewer; many host integrations; a larger user
base testing it. memsvc's "better" claims are the properties listed above, not an overall verdict.

## Evidence

- Tests: 440 in 27 suites pass (12 geometry tests skip without numpy). Phase 1 of the 0.3 plan added degraded 12, handles 12, found-by 4, tag 9,
  MCP hints 6, neighbours 51, fingerprint 18, vector status 13, scale 20, never-returned 21, six daemon tests, and five doctor tests. The earlier new suites were:
  privacy 17, service functions 41, text views 12, HTTP, client, MCP and CLI 29, viewer contract 8, trash 8. Each new file failed before
  the code existed (module missing or function missing) and passed after.
- Mutation check (`tools/mutation_check.py`, single-line breaks in throwaway copies of the package and tests). It now has 90 breaks. All 79 were caught in one full run before the current-focus note was added; after it, the 11 new ones and the 29 that exercise code it changed were caught. The first ten were: timeline ignores
  visibility, fetch ignores visibility, write skips private stripping, search ignores status, context ignores its budget, unclosed private
  kept, restore skips the owner check, restore raises trust, filters applied after the candidate cut, the project default overrides an
  explicit project. All ten are killed, each by the test that guards that line. Two earlier runs were wrong and were fixed: one let the
  status mutant survive (a test was added), and one counted every mutant as killed because the copy lacked the `memctl` launcher.
- Found by reviewing my own code against the readings: filters ran after the top-40 cut, so a matching note ranked 41st was missed. A test
  was written first and failed, the fix passed it, and the mutant above keeps it from returning.
- Browser check on throwaway demo data (`tools/demo_server.py`, port 8791): feed shows 12 active notes newest first with the three
  probation notes absent; Full text and Timeline expand inline; Search reports cost and the no-evidence state; Sessions opens the default
  group and a named one; Context shows the 88% saving labelled agent-reported; a note added while the feed was open appeared within one
  poll and was highlighted; polling stopped on leaving the tab; theme cycled auto, dark, light and persisted in the tab only; at 390 px wide
  nothing overflowed; Review reject moved a note to Trash, and Restore returned it to the Feed with trust "user"; the console stayed empty.
  Not checked: Safari or Firefox, a screen reader, very large feeds.

## Using it

The page is read from disk on every request, so a reload shows the new viewer, but new API routes need the service restarted
(`Ctrl-C` in its terminal tab, then `python3 -I memctl serve --port 8765` again).
