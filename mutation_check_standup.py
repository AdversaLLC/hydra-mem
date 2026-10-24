"""Mutation check for the standup command: breaks one rule at a time in a throwaway copy of hydra_mem.py and runs the standup tests against it.
A mutant must be killed (some test fails). Copies go to the system temp folder; the real files are never touched.
    python3 -I mutation_check_standup.py [name,name]
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(tempfile.mkdtemp(prefix="hydra-mem-mut-"))
CLASSES = ["StandupHelpers", "StandupBuild", "StandupCli"]
MUTANTS = [
    ("weekend_not_skipped", "    while d.weekday() >= 5:", "    while False:"),
    ("day_is_the_utc_day", "    local = naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()      # no tz: the system's own rules for that date",
     "    local = naive.replace(tzinfo=timezone.utc)"),
    ("leading_cd_not_skipped", '        if _program(toks[0]) != "cd":', "        if True:"),
    ("test_found_by_the_word", "        if prog in _TEST_RUNNERS:", '        if "test" in body.lower():'),
    ("time_is_the_span_not_the_gaps", "if (b - a).total_seconds() <= STANDUP_GAP_MINUTES * 60)", "if True)"),
    ("gap_of_exactly_ten_is_a_break", "<= STANDUP_GAP_MINUTES * 60)", "< STANDUP_GAP_MINUTES * 60)"),
    ("small_projects_not_folded", "        if not project and len(sessions) < 2 and not wisdom_rows:", "        if False:"),
    ("named_project_gets_folded", "        if not project and len(sessions) < 2 and not wisdom_rows:", "        if len(sessions) < 2 and not wisdom_rows:"),
    ("no_wisdom_listed", 'STANDUP_WISDOM = ("decision", "failed_path", "correction")', "STANDUP_WISDOM = ()"),
    ("summary_rows_count_as_activity", "AND type != 'summary'", ""),
    ("output_not_redacted", '    return clip(redact(text or ""), max(n - 2, 4))', '    return clip(text or "", max(n - 2, 4))'),
    ("caveat_dropped", '"caveat": STANDUP_CAVEAT', '"caveat": ""'),
    ("question_rule_off", '        if p["left_off"] and p["left_off"].rstrip().endswith("?"):', "        if False:"),
    ("no_test_rule_off", '        if p["edited_total"] and not p["test_runs"]:', "        if False:"),
    ("quiet_rule_off", '        if p["tool_rows"] >= STANDUP_QUIET_ROWS and not p["wisdom_total"]:', "        if False:"),
    ("default_day_is_yesterday_even_on_monday", "    return previous_working_day(now.date())", "    return now.date() - timedelta(days=1)"),
    ("summary_goes_to_the_first_project", '            ended_in[r["session"]] = (r["ts"], r["project"] or "(none)")',
     '            ended_in.setdefault(r["session"], (r["ts"], r["project"] or "(none)"))'),
    ("bad_day_accepted", '            if not re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", day_arg):\n                raise ValueError(day_arg)\n', ""),
    ("missing_database_not_reported", "    if not Path(db_path).exists():", "    if False:"),
]


def main() -> int:
    only = set(sys.argv[1].split(",")) if len(sys.argv) > 1 else None
    chosen = [m for m in MUTANTS if only is None or m[0] in only]
    survivors = 0
    for name, old, new in chosen:
        d = ROOT / name
        d.mkdir()
        shutil.copy2(HERE / "hydra_mem.py", d / "hydra_mem.py")
        shutil.copy2(HERE / "test_hydra_mem.py", d / "test_hydra_mem.py")
        s = (d / "hydra_mem.py").read_text()
        if s.count(old) != 1:
            print(f"{name}: the line to break was not found exactly once ({s.count(old)}); update this script")
            return 2
        (d / "hydra_mem.py").write_text(s.replace(old, new))
        p = subprocess.run([sys.executable, "-I", str(d / "test_hydra_mem.py"), *CLASSES], capture_output=True, text=True, cwd=d, timeout=600)
        killed = [l.split()[1] for l in (p.stdout + p.stderr).splitlines() if l.startswith(("FAIL:", "ERROR:"))]
        survivors += not killed
        print(f"{name:42s} {'KILLED by ' + ', '.join(killed[:2]) if killed else 'SURVIVED'}")
    print(f"{len(chosen) - survivors} of {len(chosen)} killed; copies are in {ROOT}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
