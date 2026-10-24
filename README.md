# hydra-mem

Self-hosted persistent memory for coding agents. One Python file, stdlib only,
one SQLite database with full-text search. No accounts, no network, no vendor.

What it does:

- **Captures automatically** through Claude Code lifecycle hooks: prompts, edits and
  commands (timeline), a rolling session summary, and any line in a reply tagged
  `INSIGHT:`, `DECISION:`, `FAILED PATH:`, `RETRO:`, `CORRECTION:`, `META_PARAM:`,
  `NOTE:` (append `[V]`/`[S]`/`[U]` for confidence, `#tags` for tags).
- **Injects at session start**: last summary, known dead ends, corrections, recent wisdom
  for the current project, under ~2.4k characters.
- **Serves any client** via CLI or an MCP stdio server with `search -> timeline ->
  get_observations` (compact index first, full text only for chosen ids) plus `add`.
- **Redacts** API keys, tokens, passwords and `<private>...</private>` spans before storing.
- **Round-trips** with the Hydra `wisdom.jsonl` format (`export` / `import`).
- **Summarises a day** with `standup`, from rows it already stores, with no model and no network, and read-only:

      python hydra_mem.py standup                      # the previous working day (Sat, Sun, Mon give Friday), local time
      python hydra_mem.py standup --day 2026-10-22 --project myapp --json

  Per project: sessions, active minutes (gaps over 10 minutes are breaks), prompts, files edited and created, the programs run (a leading
  `cd ... &&` is skipped), test runs, the day's tagged decisions, failed paths and corrections with their ids, and where the last session
  left off. Projects with one session and nothing tagged are folded into one "other" line unless named with `--project`. A few rule-based
  suggestions end it (a last summary that ended on a question, edits with no test run, many commands and nothing tagged). Counts are of
  distinct prompts, commands and files: hydra-mem stores each distinct line once per project, and the output says so. Idea from the local
  standup of vibe-log-cli (MIT); written here from the behaviour, with its cloud sync, telemetry, hooks and per-prompt coach left out.

## Install (Claude Code)

    python install.py            # hooks + MCP server, user scope
    python install.py --dry-run  # see the edits first
    python install.py --uninstall

Database: `~/.claude/hydra-mem/mem.db` (override with `HYDRA_MEM_DB` or `--db`).

Hook configuration in use (`~/.claude/settings.json`; check it after re-running `install.py`,
which may write its own defaults): `SessionStart` synchronous, timeout 10 s (its stdout becomes
`additionalContext`); `UserPromptSubmit` and `PostToolUse` `async: true` (they run in parallel
in the background; SQLite WAL plus a 10 s busy wait serializes their short writes), with the
`PostToolUse` matcher excluding Read/Glob/Grep/ToolSearch/LS/TodoWrite/TodoRead/WebSearch/WebFetch
and `mcp__ccd_*`; `Stop` and `SessionEnd` synchronous, timeout 20 s.

Hook failure semantics: a hook never exits 2 (that would block Claude Code, e.g. drop the
prompt on `UserPromptSubmit`). Any failure (unreadable DB, lock held too long, odd payload)
is one `hydra-mem: <event> hook failed: ...` line on stderr and exit 1, a non-blocking error.
`SessionStart` waits at most 2 s for a write lock and still injects context if it cannot get one.
Hook stdin is decoded as UTF-8 regardless of the Windows console code page.

Duplicates: an exact repeat is rejected by hash; a wisdom line that differs from one of the
last 300 rows of the same type and project only in case, whitespace, punctuation, backticks
or a trailing `[...]` annotation is rejected too. Paraphrases are NOT detected (e.g. two
differently worded "Chloe and Luna are not Joshua" corrections both persist); remove the
weaker one with `python hydra_mem.py delete <id>`.

## Use from anything else

    python hydra_mem.py add "rotate refresh tokens on every use" --type decision --project myapp --tags auth --conf V
    python hydra_mem.py search "refresh token" --project myapp
    python hydra_mem.py timeline --around 42
    python hydra_mem.py get 42 43
    python hydra_mem.py context --project myapp     # what a new session is told
    python hydra_mem.py mcp                          # MCP server over stdio for any MCP client

For a local model runner that speaks MCP (Ollama-based agents, LM Studio, OpenHands, etc.)
register `python hydra_mem.py mcp` as a stdio server. For one that doesn't, call the CLI
with `--json` and paste `context` output into the system prompt.

## Second store: kuzu-memory via `hydra_bridge.py` (unified hook layer)

The same five hydra-mem hooks also feed a per-project **kuzu-memory** graph
(`<project>/.kuzu-memory/memories.db`). hydra-mem stays the fast capture layer;
`hydra_bridge.py` (runs under the kuzu-memory tool interpreter, *not* ours) moves
the durable wisdom across and provides the read side. hydra-mem only ever
*spawns* the bridge; it never imports kuzu and stays stdlib-only.

What the hooks do when the bridge is present:

| Event | hydra-mem | bridge |
|---|---|---|
| `Stop` (new wisdom extracted) / `SessionEnd` (always) | extract tagged lines into SQLite | spawn **detached** `promote --project-root <root>` then `context` into a cache file; the hook never waits |
| `SessionStart` | build the 2.4k hydra context | append the bridge's `context` (fresh cache -> instant; else live call capped at 2s; else stale cache); total <= 3200 chars |
| CLI `promote` | | run `promote` synchronously, print its output, refresh the cache |

Discovery and knobs (all optional):

- `HYDRA_BRIDGE_PYTHON` interpreter for the bridge. Default
  `C:/Users/Admin/AppData/Roaming/uv/tools/kuzu-memory/Scripts/python.exe`.
- `HYDRA_BRIDGE` path to `hydra_bridge.py`. Default: next to `hydra_mem.py`, then
  `C:/Users/Admin/Desktop/USB/hydra-mem/hydra_bridge.py`.
- `HYDRA_BRIDGE_CONTEXT_TIMEOUT` seconds for the live `context` call at SessionStart (default 2).
- `HYDRA_BRIDGE_DISABLE=1` turns the whole bridge path off.
- `<root>` is the git toplevel of the hook's `cwd`, else the `cwd` itself.

Files it writes, both next to the hydra-mem DB, i.e. in the directory of `HYDRA_MEM_DB` /
`--db` (default `~/.claude/hydra-mem/`; a run against an overridden DB logs next to that DB):
`bridge.log` (stdout/stderr of every detached chain), `bridge-cache/<project>-<hash>.txt`
(the last `context` output per project root) and, while a chain runs, `bridge-chain.lock`
(chains run one at a time; a lock older than 15 min is treated as stale). At SessionStart,
bridge lines that repeat a line already in the hydra half are dropped. `python hydra_mem.py stats` shows the
bridge status under `"bridge"`.

Graceful degradation: if the interpreter or script is missing, `stats.bridge.available`
is false, SessionStart injects only the hydra half, Stop/SessionEnd spawn nothing, and
`promote` exits 1 with `bridge not available: <reason>`. Nothing else changes.

Why a cache: the real kuzu stack needs ~5-6 s to import and open the graph (measured
on this machine), far above the 2 s SessionStart cap. The detached chain run at
Stop/SessionEnd therefore precomputes the next session's context; SessionStart reads
it in milliseconds.

Two rules the kuzu side imposes (see kuzu-memory's `ClaudeHooksInstaller`):

1. No Claude Code hook *command* may contain the substring `kuzu` (case-insensitive) or
   kuzu-memory's installer deletes it. hydra-mem's hook commands and the bridge file name
   don't; the interpreter path does, but it is only used inside this process.
2. Kuzu is single-writer. While the kuzu MCP server (or any other process) holds the
   project DB, `promote` writes are rejected and `context` reports "(unavailable: ...)";
   the chain simply retries at the next Stop/SessionEnd and the cache keeps the last good
   context. Check `bridge.log` when the kuzu side looks stale.

    python hydra_mem.py promote --cwd <project>        # push wisdom now, synchronously
    python hydra_mem.py context --cwd <project>        # what SessionStart injects (both halves)
    python hydra_mem.py context --cwd <project> --no-bridge

## Tests

    python test_hydra_mem.py

58 tests: the original 11, 12 for the bridge layer, 9 for the graph MCP tools, 2 for the
migration (skipped without the graph interpreter), 1 for `install.py` and 23 regression
tests from the 2026-10-20 review (hook robustness and exit codes, UTF-8 stdin, locked DB,
transcript partial lines, tag regexes, near-duplicates, context footer, bridge chain
serialization, MCP protocol errors). The bridge and
graph tests use a stub `hydra_bridge.py` (env-steered: slow, failing, oversized output) so
they pass with or without the real bridge and never touch a real kuzu database.

## Graph store: migration and MCP unification

See `GRAPH.md`. In short:

    python migrate_to_graph.py --dry-run     # exact mapped records, nothing written
    python migrate_to_graph.py               # hydra wisdom -> <project>/.kuzu-memory/memories.db, non-expiring, deduped

and the MCP server gains `graph_recall {query, limit}` and `graph_context {}` which read that
graph (inline kuzu read, 0.5-2.6 s; `HYDRA_GRAPH_TIMEOUT` default 15 s, set from the measured
bridge latency, see GRAPH.md) and answer
`graph store unavailable: <reason>` instead of failing. Tests: `McpGraphTests`, `MigrateToGraphTests`.
