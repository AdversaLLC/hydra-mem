# Proposal: take vibe-log-cli's useful parts into hydra-mem, local only

Status 2026-10-23: **round 1 is built** (owner said yes): `hydra_mem.py standup` in `~/.claude/hydra-mem`, 23 new tests written first, 19 mutants all caught, hydra-mem suite 86 OK. Built differently from the text below in three places: the suggestion about open `question` notes and the memsvc focus is not built (hydra-mem has no question type and must not call another service), the probe's four fixes are all in, and a fifth was found on the real data: a session summary belongs to the project the session ended in, not to every project it passed through. Nothing else from vibe-log was taken.

Written 2026-10-23 as the ABSORB step of the hydra-evolve skill, at the owner's request ("make what it does also part of the hydra mem"). It is a proposal: nothing here is built, installed or run against the real store except two read-only queries. Evidence labels: **V** I ran it here, **S** a source says so, **U** unverified.

## Verdict

Absorb one small thing now, decline the rest. Round 1 is a model-free `standup` command inside hydra-mem (`~/.claude/hydra-mem/hydra_mem.py`), built from data hydra-mem already stores. vibe-log's cloud sync, sign-in, telemetry, per-prompt coach and its own hooks do not come in. We keep one spine: hydra-mem's hooks stay the only capture path.

## What vibe-log-cli is, and what was checked

- **Provenance (S, GitHub API fetched 2026-10-23):** `vibe-log/vibe-log-cli`, an organisation repo, not a fork, not archived, MIT, TypeScript, created 2025-08-15, last push 2026-04-19, head `0354df1` "chore: release v0.8.14" (Danny Shmueli), 339 stars, 5 open issues. Six months without a push is a slow project. Popularity is not evidence.
- **Install (S, package.json):** no preinstall or postinstall script. Dependencies include `axios`, `event-source-polyfill`, `update-notifier` (checks the npm registry), `open`, `better-sqlite3`, and `@zed-industries/claude-code-acp` and `codex-acp` (it drives Claude Code or Codex as a subprocess).
- **What it reads (S, README and file names):** Claude Code and Codex session files.
- **Telemetry (S, `src/lib/telemetry.ts`):** sent only when signed in; setup state only (hooks installed, hook mode, tracked project count, statusline personality, CLI and hook versions); no session content, no project names. No off switch other than not signing in.
- **Sanitiser (S, `src/lib/message-sanitizer.ts`):** regex passes for code blocks, paths, URLs, emails, IPs and a list of key shapes. By the summariser's reading it misses PEM keys, `KEY=value` secrets outside quoted JSON, `ssh://` and `ftp://` URLs and IPv6, over-redacts "bearer token", and in debug mode keeps the first 20 characters of each matched credential in memory. hydra-mem's own `redact` (V, read in `hydra_mem.py`) already covers PEM blocks and `key=value` shapes, so nothing is taken from vibe-log here.
- **Local standup (S, `src/commands/standup-local.ts`):** no model call. It reads the previous working day's sessions (Monday and Sunday roll back to Friday), counts edited and created files by basename, records the first word of each shell command and flags test runs, adds "active/intensive development" by message count and "extended focus" by duration, and prints up to five suggestions. Visible bugs: the first "general" entry is listed twice, and merged test entries print with a blank label.
- **The "local" prompt coach (S):** goes through the ACP packages, so it calls a model on every prompt. That costs tokens and time, which is why it is declined below.
- **Not done:** I did not clone, install or run it. I read the README, `package.json` and four source files through a page-fetch tool whose summariser can misstate code, so every S above is second hand. Nothing in the fetched text was addressed to an AI reader.

Vetting-checklist result for the three traits our use depends on: *no network calls of its own* **fails** (axios, update-notifier, cloud sync, telemetry), so it is not run; *safe handling of secrets* is weaker than ours; *licence* allows reimplementation (MIT, and we would write Python from the behaviour, not copy TypeScript). Hence: ideas only.

## Triage

| vibe-log element | Verdict | Where we stand |
|---|---|---|
| Capture of prompts, tool calls, sessions, summaries | REDUNDANT | hydra-mem hooks SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd. Its DB today (V, read-only): 8,902 observations, 52 sessions, 959 prompts, 7,651 tool rows (Bash 5,819, Write 813, Edit 357), 39 session summaries |
| Redaction before storing | REDUNDANT (ours is stronger) | `SECRET_PATTERNS` and `<private>` stripping in `hydra_mem.py` |
| Hook installer with backup and uninstall | REDUNDANT | `install.py --dry-run --uninstall` |
| Daily standup, no model | **DELTA** | nothing like it; round 1 below |
| Productivity report (local HTML) | DELTA, later | round 2, gated |
| Previous-working-day window (Mon/Sun to Fri) | DELTA | small, part of round 1 |
| Cloud sync, GitHub sign-in, dashboard | DECLINED | against local-only |
| Telemetry, update-notifier | DECLINED | phones home |
| Prompt coach on the statusline | TENSION, declined for now | a model call per prompt; hydra-mem's UserPromptSubmit hook is async and model-free by design; it would also compete for the statusline |
| Codex hooks, Cursor stats, personalities, ccusage | DEFERRED | not needed; re-check if we use Codex |
| Its own hooks (SessionStart, PreCompact, UserPromptSubmit) | DECLINED | a second capture path; PreCompact adds little because Stop already reads the transcript from a saved offset (U: confirm Claude Code does not rewrite the transcript when it compacts) |

Frame check: an accessory (a reporting layer over session data), not a rival spine, provided its hooks are not installed.

## Round 1 candidate: `hydra_mem.py standup`

One small change in `hydra_mem.py`, plus tests in `test_hydra_mem.py` and a README paragraph. No new hooks, no new tables, no network, no model.

**Behaviour.** `hydra_mem.py standup [--day YYYY-MM-DD] [--project P] [--json]`. Default day is the previous working day in local time (Saturday, Sunday and Monday roll back to Friday). For each project with records that day print: sessions, active minutes, prompts, files edited and created (basenames, top three by count), what was run (see below), test runs, and the day's tagged wisdom (decisions, failed paths, corrections) with ids so `get` can open them. Then up to five suggestions, each from a plain rule. An empty day says so.

**Where it improves on vibe-log, from a read-only probe of the real DB for 2026-10-22 (V, `scratchpad/standup_probe.py`, 2,551 records, 24 projects):**
1. *Active time, not span.* First-to-last record gave 1,191 minutes for one project (20 hours). Count a gap of more than 10 minutes as a break.
2. *Useful commands.* The first word of a command was `cd` for most of them. Skip a leading `cd ... &&` and variable assignments, then take the program; group by program.
3. *Projects.* hydra-mem derives the project from the folder name, so one day produced 24 "projects", most of them folders such as `tests` and `logs`. Group by session first and show a project only when it has two or more sessions or any wisdom; fold the rest into "other". Allow `--project`.
4. *Wisdom.* The tagged INSIGHT/DECISION/FAILED PATH/CORRECTION lines of the day are the most useful standup content and vibe-log has none.
5. *Do not copy its bugs* (duplicated "general" entry, blank test label).
6. *Suggestions.* Rules, no model: resume the last session's summary when it ended with a question or a failed test run; list open `question` notes and the current focus (`memctl focus`, when memsvc runs); say when a session produced no tagged wisdom.

**Safety.** Read-only on the DB; output passes through the existing redaction; private spans are never printed; stored text is shown as data.

**Gauntlet (red first, as the skill requires).** On a temporary DB built in the tests: window choice including the Monday, Saturday and Sunday cases and a day with no records; edited and created counts by basename (Windows and Unix paths); command grouping with a leading `cd` and with `A=b cmd`; active time with a 9-minute and an 11-minute gap; the projects-folding rule; wisdom lines listed with ids; `--json` is valid and stable; a secret in a prompt never appears; no socket or subprocess is opened (patch both and fail on use); the real 8,902-row DB renders in under one second. Add one mutant per rule in the repo's mutation script and require each to be caught. The existing hydra-mem suite must still pass.

**Not in round 1:** the HTML report, the prompt coach, any hook change, any cloud path.

## Later rounds (the entropy gate applies: no round 3 without fresh outside signal)

- **Round 2, `report --days N --out FILE`:** a static HTML or Markdown page from the same data (sessions per day, active hours, tool mix, top files, wisdom counts). Start only after the owner has used `standup` and says what is missing.
- **Prompt hygiene, model-free (U):** counts only (prompts with no file, error text or acceptance criterion). The value is unproven; do not build without a reason from the owner.

## Deferred, not dead (re-check triggers)

Prompt coach: if there is ever a local model with spare capacity and an opt-in switch. Codex session reading: if Codex is used here. vibe-log cloud: only if the owner decides to leave local-only, and then only after reading the sanitiser and `api-client.ts` in a scratch directory. Re-check vibe-log on a version bump, a new maintainer, new network behaviour or a changed install script.

## Ledger

Adopted (idea only): standup, working-day window. Declined: cloud sync, sign-in, telemetry, update-notifier, its hooks, the coach (for now). Already present: capture, redaction, hook install and uninstall. Source: vibe-log-cli `0354df1`, MIT. The decision to build round 1 is the owner's.
