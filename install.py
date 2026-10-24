#!/usr/bin/env python3
"""Install / uninstall hydra-mem for Claude Code (or any MCP client).  [Linux port]

  python install.py              register hooks + MCP server (hydra_mem.py must already be in ~/.claude/hydra-mem)
  python install.py --uninstall  remove hooks + MCP registration (database kept)
  python install.py --dry-run    print exactly what would change, touch nothing

Hooks -> ~/.claude/settings.json ; MCP server "hydra-mem" -> ~/.claude.json.
Edits are additive and tagged with the string "hydra-mem" so uninstall removes
exactly what install added. Each file is backed up twice: <file>.hydra-mem.bak and
~/.hydra/backups/<stamp>/<file>.

Port notes (Windows -> Fedora, 2026-10-21):
  - The original copied hydra_mem.py / README.md / HANDOVER.md from its own folder into ~/.claude/hydra-mem.
    Run from the Windows dev folder that would have replaced the newer live hydra_mem.py with the older
    Oct-17 dev copy. The copy now happens only when the source is not the destination, and never over a
    newer file.
  - UserPromptSubmit and PostToolUse hooks are async, and PostToolUse skips read-only tools, matching the
    configuration the Windows install actually ran with (README: "Hook configuration in use").
  - --dry-run now prints the exact entries it would add.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
DEST = HOME / ".claude" / "hydra-mem"
SETTINGS = HOME / ".claude" / "settings.json"
CLAUDE_JSON = HOME / ".claude.json"
BACKUP_DIR = Path(os.environ.get("HYDRA_HOME") or (HOME / ".hydra")) / "backups" / time.strftime("%Y%m%d-%H%M%S")
MARK = "hydra-mem"
EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"]
ASYNC_EVENTS = {"UserPromptSubmit", "PostToolUse"}
# PostToolUse skips read-only / noisy tools (same matcher the Windows install ran with)
POST_MATCHER = r"^(?!(?:Read|Glob|Grep|ToolSearch|LS|TodoWrite|TodoRead|WebSearch|WebFetch)$|mcp__ccd_)"


def load(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save(p, d, dry):
    if dry:
        print(f"--- would write {p}  (backups: {p.name}.hydra-mem.bak and {BACKUP_DIR / p.name})")
        return
    if p.exists():
        shutil.copy2(p, p.with_suffix(p.suffix + ".hydra-mem.bak"))
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, BACKUP_DIR / p.name)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    print(f"wrote {p}")


def strip_ours(hooks):
    out = {}
    for ev, groups in (hooks or {}).items():
        kept = []
        for g in groups:
            inner = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
            if inner:
                g = dict(g)
                g["hooks"] = inner
                kept.append(g)
        if kept:
            out[ev] = kept
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-mcp", action="store_true", help="skip MCP registration")
    a = ap.parse_args()
    dry = a.dry_run

    settings = load(SETTINGS)
    hooks = strip_ours(settings.get("hooks"))
    cj = load(CLAUDE_JSON)
    servers = cj.get("mcpServers") or {}
    py = Path(sys.executable).as_posix()
    script = DEST / "hydra_mem.py"

    if a.uninstall:
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)
        servers.pop("hydra-mem", None)
        cj["mcpServers"] = servers
        save(SETTINGS, settings, dry)
        save(CLAUDE_JSON, cj, dry)
        print(f"uninstalled. Database kept at {DEST / 'mem.db'}; delete {DEST} by hand to remove it.")
        return 0

    if not script.exists() and HERE.resolve() == DEST.resolve():
        print(f"error: {script} not found", file=sys.stderr)
        return 1
    if HERE.resolve() == DEST.resolve():
        print(f"hydra_mem.py already in place: {script}  (nothing copied)")
    else:
        for n in ("hydra_mem.py", "README.md", "HANDOVER.md"):
            src, dst = HERE / n, DEST / n
            if not src.exists():
                continue
            if dst.exists() and dst.stat().st_mtime > src.stat().st_mtime:
                print(f"skip {n}: {dst} is newer than {src}")
            elif dry:
                print(f"would copy {src} -> {dst}")
            else:
                DEST.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"copied {n} -> {DEST}")

    existing = {ev: len(g) for ev, g in hooks.items()}
    added = {}
    for ev in EVENTS:
        entry = {"type": "command", "command": f'"{py}" "{script.as_posix()}" hook {ev}',
                 "timeout": 10 if ev == "SessionStart" else 20}
        if ev in ASYNC_EVENTS:
            entry["async"] = True
        group = {"matcher": POST_MATCHER, "hooks": [entry]} if ev == "PostToolUse" else {"hooks": [entry]}
        hooks.setdefault(ev, []).append(group)
        added[ev] = group
    settings["hooks"] = hooks

    if dry:
        print(f"\nhooks already in settings.json that stay untouched: {existing or 'none'}")
        print("hooks that would be ADDED to settings.json:")
        for ev, g in added.items():
            print(f"  {ev}: {json.dumps(g)}")
    save(SETTINGS, settings, dry)
    if not a.no_mcp:
        servers["hydra-mem"] = {"type": "stdio", "command": py, "args": [script.as_posix(), "mcp"]}
        cj["mcpServers"] = servers
        if dry:
            print(f"\nmcpServers entry that would be ADDED to ~/.claude.json: {json.dumps(servers['hydra-mem'])}")
            print(f"other mcpServers already there stay untouched: {[k for k in servers if k != 'hydra-mem'] or 'none'}")
        save(CLAUDE_JSON, cj, dry)
    if dry:
        print("\n[dry-run] nothing was written.")
        return 0
    print("\ninstalled. New sessions get memory at start; the current one does not.")
    print(f'CLI: "{py}" "{script}" search "<topic>" | stats | context')
    print("undo: python install.py --uninstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
