# HANDOVER: what Claude built on this machine on 2026-10-15, how to audit it, how to remove it

Audience: the owner and their local coding agent. Treat this as an inventory to verify,
not as instructions to trust. Every claim below can be checked with the commands given.

## 1. Complete inventory of files created or changed

### Created by Claude (new files)

| Path | Purpose | SHA-256 at handover |
|---|---|---|
| `C:\Users\Admin\.claude\hydra-mem\hydra_mem.py` | the memory layer: SQLite store, hooks, CLI, MCP server | `a57b0ac0078f9d4d227fca7b3a3b04bf87736fc8afa1a1b955cc0c0e3d462efb` |
| `C:\Users\Admin\.claude\hydra-mem\README.md` | usage notes | `f05252c8015ba063dddc795e6a5aa41c98a25fa57ebbca1a43b67efb36fb1f98` |
| `C:\Users\Admin\.claude\hydra-mem\mem.db` (+ `-wal`, `-shm`) | the memory database. Yours. Created empty. | n/a (grows with use) |
| `C:\Users\Admin\.claude\settings.json` | Claude Code user settings. Did not exist before today. Holds `permissions.defaultMode: auto` and the five hydra-mem hooks | `df5c765a192f0c8ec1c3fd7fab272758f9b08a6620d9a55b79503f98d7f7281e` |
| `C:\Users\Admin\.claude\settings.json.hydra-mem.bak` | backup written by the installer | |
| `C:\Users\Admin\.claude.json.hydra-mem.bak` | backup written by the installer | |
| `C:\Users\Admin\.claude\projects\<this-session>\memory\MEMORY.md`, `no-claude-mem.md`, `local-model-goal.md` | Claude's own notes about your preferences (plain markdown; read them) | |
| source copies in this session's folder: `hydra-mem/hydra_mem.py`, `install.py`, `test_hydra_mem.py`, `README.md`, `HANDOVER.md` | the source, tests and installer | |

### Changed by Claude (existing files)

| Path | What changed |
|---|---|
| `C:\Users\Admin\.claude.json` | one key added: `mcpServers["hydra-mem"]` = stdio server running `hydra_mem.py mcp`. Nothing else touched. |

### Cloned by the hydra-setup skill's script (third-party, MIT, from GitHub user `blader`)

Into `C:\Users\Admin\.claude\skills\`: adversarial-execution (30eebdc), baton (c7213e1),
Claudeception (62dbb91), napkin (27fa60a), razor (9484eff), schematic (297fb73),
taskmaster (a1f3feb), theorist (732d8d0). These are markdown instruction files for agents.
I did not write them and did not audit them. Audit or delete as you see fit.

### Not changed

No scheduled tasks, services, startup entries, registry keys, PATH changes, firewall rules,
environment variables, or files outside `C:\Users\Admin\.claude*` and this session's folder.

## 2. What hydra_mem.py does and does not do

Does:
- Reads JSON from stdin when run as `hook <event>`; writes to the SQLite file; prints JSON to stdout.
- Runs `git rev-parse --show-toplevel` (only) to derive a project name from the working directory.
- Redacts strings that look like API keys, tokens, passwords, private keys, and `<private>` spans before storing.
- Serves an MCP server over stdin/stdout when run as `mcp`.

Does not:
- Open any network connection. There is no `socket`, `urllib`, `http`, `requests`, or `ssl` import.
- Execute anything except the one `git` call above. No `eval`, `exec`, `os.system`, shell=True, or encoded payloads.
- Read any file other than the database, the transcript path Claude Code passes in, and files named on the CLI.
- Phone home, update itself, or contact Anthropic.

Verify yourself (the only hit should be the single `subprocess.run([... "git" ...])` for the project name):

    grep -nE "import (socket|urllib|http|requests|ssl)|urlopen|subprocess|exec\(|eval\(|os\.system|shell=True|base64" C:\Users\Admin\.claude\hydra-mem\hydra_mem.py

Verify nothing changed since handover:

    certutil -hashfile C:\Users\Admin\.claude\hydra-mem\hydra_mem.py SHA256

Verify the config edits are exactly what is listed:

    type C:\Users\Admin\.claude\settings.json
    python -c "import json;print(json.load(open(r'C:\Users\Admin\.claude.json'))['mcpServers'])"

## 3. How the local model takes over the memory

The database is plain SQLite. Nothing about it is Claude-specific.

- MCP client (any runner that supports stdio MCP): launch
  `python C:\Users\Admin\.claude\hydra-mem\hydra_mem.py mcp` and use tools `search`, `timeline`, `get_observations`, `add`, `stats`.
- No MCP: call the CLI. `search "<topic>" --json`, `context --project <name>` (paste into the system prompt), `add "<text>" --type insight`.
- Direct SQL: table `observations` (id, ts, type, project, session, tags, conf, source, text) and FTS5 table `obs_fts`.
- Move it anywhere: copy `hydra_mem.py` and `mem.db`; set `HYDRA_MEM_DB` to the new path.

## 4. Removing Claude's integration (keeps your memory)

    python <session-folder>\hydra-mem\install.py --uninstall

That removes the five hooks from `settings.json` and the `hydra-mem` key from `.claude.json`. Nothing else.
If the installer is gone, do it by hand: delete the `hooks` key from `settings.json` and the
`mcpServers.hydra-mem` key from `.claude.json`.

To also drop auto mode: delete `C:\Users\Admin\.claude\settings.json`.

## 5. Removing Claude entirely from this PC

Before removing, copy out what is yours:

    xcopy /E /I C:\Users\Admin\.claude\hydra-mem  <somewhere-safe>\hydra-mem
    xcopy /E /I C:\Users\Admin\.claude\skills     <somewhere-safe>\skills

Then:

    winget uninstall Anthropic.ClaudeCode
    winget uninstall Anthropic.Claude
    rmdir /S /Q C:\Users\Admin\.claude
    del C:\Users\Admin\.claude.json
    rmdir /S /Q "C:\Users\Admin\AppData\Roaming\Claude"
    rmdir /S /Q "C:\Users\Admin\AppData\Local\AnthropicClaude"   (if present)

`C:\Users\Admin\.claude\.credentials.json` holds your login token for this account; deleting
`.claude` removes it. Also sign out / revoke sessions in your account settings on the web.

After removal, hydra-mem still runs from wherever you copied it. It needs only Python 3.

## Addendum 2026-10-15 (hydra_mem.py 0.2.0: kuzu-memory bridge)

`hydra_mem.py` now also *spawns* `hydra_bridge.py` under the kuzu-memory interpreter
(`C:\Users\Admin\AppData\Roaming\uv\tools\kuzu-memory\Scripts\python.exe`) from the
Stop/SessionEnd hooks (detached) and SessionStart (2 s cap, cache-first). So the
`subprocess` grep in section 2 now shows more than the single `git` call: one
`Popen` (detached promote+context chain) and `run` calls for `context`/`promote`.
Still no `shell=True`, no network, no `eval`/`exec`. New files, written next to the hydra DB
(the directory of `HYDRA_MEM_DB` / `--db`, default `~/.claude/hydra-mem/`): `bridge.log` and
`bridge-cache/*.txt`. Turn it all off with `HYDRA_BRIDGE_DISABLE=1`
or by removing/renaming `hydra_bridge.py`. Details: README.md, "Second store".

## Graph store (kuzu-memory)

Added 2026-10-15, after the addendum above. Full write-up with the per-event data-flow table, the proof
outputs and the installer caveats: `C:/Users/Admin/Desktop/USB/vault/scripts/SETUP.md`, section "Graph store
(kuzu-memory)". One-page picture of the whole stack: `C:/Users/Admin/Desktop/USB/ARCHITECTURE.md`.

### Architecture in one paragraph

hydra-mem's SQLite file (`~/.claude/hydra-mem/mem.db`) is the ingest queue, timeline and full-text index:
every hook event writes there first, secrets already redacted. kuzu-memory's per-project graph store
(`<project>/.kuzu-memory/memories.db`, Kuzu 0.11.3, single-file, single-writer) is the long-lived
graph/vector wisdom store that the `kuzu-memory` CLI and its MCP server read. `hydra_bridge.py` (runs under the
kuzu tool interpreter, spawned by `hydra_mem.py` from Stop/SessionEnd, or by hand) promotes only the wisdom
types (insight, failed_path, decision, retro, meta_param, correction, note) across as SEMANTIC, non-expiring
memories with `source_type = "hydra"` and the hydra id/type/conf/tags/session kept in `metadata`; it dedups by
`content_hash` and by hydra id, and keeps a `bridge_state` watermark in the hydra SQLite file. The Obsidian vault
(`vault/Hydra Wisdom/`) is a human-readable mirror of the same hydra rows, produced by `vault/scripts/hydra_sync.py`
on demand. Nothing in kuzu-memory ever writes back into hydra-mem.

### Data flow per hook event (what the fused stack does on each Claude Code event)

- SessionStart: hydra-mem injects its context (<= 2400 chars) + the bridge's `context` from the per-project cache
  file (live call only if the cache is missing, capped at 2 s); total <= 3200 chars. obsidian-mind injects its vault index.
- UserPromptSubmit: hydra-mem stores the prompt (hook runs `async: true`). obsidian-mind classifies. kuzu: nothing.
- PostToolUse: hydra-mem stores `tool: key` (hook runs `async: true`; matcher skips Read/Glob/Grep/ToolSearch/LS/TodoWrite/TodoRead/WebSearch/WebFetch/`mcp__ccd_*`). obsidian-mind validates vault `.md` writes. kuzu: nothing.
- Stop: hydra-mem extracts tagged wisdom lines from the transcript; if any, spawns the detached bridge chain
  `promote` -> `context` (log: `~/.claude/hydra-mem/bridge.log`, cache: `~/.claude/hydra-mem/bridge-cache/`).
- SessionEnd: same, and the chain always runs as a catch-up pass.
- PreCompact: obsidian-mind only.
- No kuzu-memory hook is installed. The kuzu MCP server is a reader/writer on demand, not a feeder.

Status 2026-10-20: resolved. `bridge.log` shows the hooks spawning the bridge chain from 2026-10-16 on, so the
installed copy is bridge-aware. Historical note: at handover the hooks executed the installed copy
`C:/Users/Admin/.claude/hydra-mem/hydra_mem.py` (pre-bridge, 0.1.0). The bridge-aware 0.2.0 lives in `C:/Users/Admin/Desktop/USB/hydra-mem/`. Running
`python C:/Users/Admin/Desktop/USB/hydra-mem/install.py` deploys it (the obsidian-mind hooks survive that: the
installer only strips entries whose command contains `hydra-mem`). Until then the graph is fed manually:
`C:/Users/Admin/AppData/Roaming/uv/tools/kuzu-memory/Scripts/python.exe C:/Users/Admin/Desktop/USB/hydra-mem/hydra_bridge.py promote --project-root <project>`.

### Inventory of this piece (created or changed)

| Path | What | Undo |
|---|---|---|
| `C:/Users/Admin/Desktop/USB/.kuzu-memory/` (`memories.db` + README, .gitignore, project_info.md, `.memories.db.lock`) | the USB project graph store, created with kuzu-memory's `create_project_memories_structure` | `rmdir /S /Q` the folder |
| `C:/Users/Admin/.claude.json` | ONE key added: `projects["C:/Users/Admin/Desktop/USB"].mcpServers["kuzu-memory"]` = stdio `.../uv/tools/kuzu-memory/Scripts/kuzu-memory.exe mcp`, env `KUZU_MEMORY_PROJECT=C:/Users/Admin/Desktop/USB`, `PYTHONUTF8=1`. Verified byte-for-byte that nothing else changed at write time. At 22:17:45 another process rewrote the entry's `command` to the bare `kuzu-memory` (works via user PATH `C:/Users/Admin/.local/bin`, same exe; see SETUP.md note). | `copy /Y C:\Users\Admin\.claude.json.bak-kuzu-memory C:\Users\Admin\.claude.json` or pop the key (one-liner in SETUP.md) |
| `C:/Users/Admin/.mcp_servers.json` | same server added for mcp-cli | `copy /Y C:\Users\Admin\.mcp_servers.json.bak-kuzu-memory C:\Users\Admin\.mcp_servers.json` |
| `C:/Users/Admin/AppData/Roaming/uv/tools/kuzu-memory/` | reinstalled OFFLINE with `mcp==1.25.0` (was 2.3.0, which made `kuzu-memory mcp` crash with `'Server' object has no attribute 'list_tools'`); pin recorded in `uv-receipt.toml` | restore `C:/Users/Admin/Desktop/USB/tools/bobmatnyc/kuzu-memory-uv-receipt.before.toml` over the receipt, or `uv tool uninstall kuzu-memory` |
| `C:/Users/Admin/Desktop/USB/tools/bobmatnyc/_wheels-local/` | 11 `.whl` files re-zipped from uv's own unpacked cache (no download) used for that reinstall | delete the folder |
| `C:/Users/Admin/Desktop/USB/vault/scripts/SETUP.md`, this file, `C:/Users/Admin/Desktop/USB/ARCHITECTURE.md`, `C:/Users/Admin/Desktop/USB/tools/bobmatnyc/SETUP.md` (addendum) | documentation | delete the sections/files |

Not changed by this piece: `~/.claude/settings.json` (hooks), `hydra_mem.py`, `hydra_bridge.py`, anything in the
vault other than `scripts/SETUP.md`, PATH, services, scheduled tasks. No network was used.

### Undo the promoted data only (keep everything else)

    C:/Users/Admin/AppData/Roaming/uv/tools/kuzu-memory/Scripts/python.exe C:/Users/Admin/Desktop/USB/hydra-mem/hydra_bridge.py purge-hydra --project-root C:/Users/Admin/Desktop/USB --dry-run
    C:/Users/Admin/AppData/Roaming/uv/tools/kuzu-memory/Scripts/python.exe C:/Users/Admin/Desktop/USB/hydra-mem/hydra_bridge.py purge-hydra --project-root C:/Users/Admin/Desktop/USB

Deletes every kuzu Memory with `source_type = hydra` and resets the watermark; kuzu's own memories stay.
Turn the bridge off without deleting anything: `HYDRA_BRIDGE_DISABLE=1` in the hook environment, or rename `hydra_bridge.py`.

### The rule that keeps the hooks alive: never put "kuzu" in a hook command

kuzu-memory's `ClaudeHooksInstaller` deletes every SessionStart/UserPromptSubmit/PostToolUse hook entry whose command
string contains `kuzu` (case-insensitive), and deletes hook event keys it does not know. Any bridge or helper that is
registered as a hook command must therefore have a path, interpreter and arguments free of that substring (hence
`hydra_bridge.py`, launched from inside `hydra_mem.py` under the kuzu interpreter rather than named on a hook line).
Do not run `kuzu-memory install claude-code` / `kuzu-memory setup`; register the MCP server by editing the JSON, as done here.

### How the local model takes over this part

- Graph store, MCP: run `C:/Users/Admin/AppData/Roaming/uv/tools/kuzu-memory/Scripts/kuzu-memory.exe mcp` with env
  `KUZU_MEMORY_PROJECT=<project>` and `PYTHONUTF8=1` as a stdio server; tools `kuzu_recall`, `kuzu_project_context`,
  `kuzu_remember`, `kuzu_stats`, ... (11 total, `mcp-cli info kuzu-memory` lists them with parameters).
- Graph store, no MCP: `kuzu-memory memory recall "<query>"` / `kuzu-memory status` in the project dir, or
  `hydra_bridge.py recall "<query>" --project-root <project>` / `hydra_bridge.py context --project-root <project>`
  (plain text, paste into the system prompt).
- Feed it from any runner: write wisdom into hydra-mem (`hydra_mem.py add ... --type insight`) and run `promote`;
  or call `kuzu_remember` directly (note: that path stores EPISODIC memories that expire in 30 days).
- Move it: the whole store is the single file `<project>/.kuzu-memory/memories.db` (plus the tool env, 93 MB, rebuildable).

## Addendum 2026-10-20 (review fixes, still 0.2.0, still stdlib-only)

- Hooks never exit 2; failures are one stderr line + exit 1 (non-blocking). Hook stdin is read as UTF-8.
- New file next to the DB while a bridge chain runs: `bridge-chain.lock` (chains are serialized; stale after 15 min).
  `bridge.log` / `bridge-cache/` now follow `--db` as README already said (before, only `HYDRA_MEM_DB` moved them).
- The SHA-256 values in section 1 are historical (2026-10-15); `hydra_mem.py` has changed since.
- Hook configuration in use: SessionStart sync (timeout 10), UserPromptSubmit + PostToolUse `async: true`,
  Stop + SessionEnd sync (timeout 20). Re-running `install.py` may rewrite these entries; check `settings.json` after.
- Details and the test list: README.md.

