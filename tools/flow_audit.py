#!/usr/bin/env python3
"""Flow auditor — checks a pipeline run's BEHAVIOR against flow invariants.

Reads what the run already recorded (events.jsonl, the content-addressed
logs/prompts/ store, the pipeline log) and issues verdicts on the properties
that unit tests structurally cannot see — "every component works but the
information doesn't flow between them" (the session-63 finding: 15 iterations
plateaued at 67/123 because t4's genesis lessons never reached a coding
prompt and every iteration wiped the fix loop's progress).

Checks
  C1 repo-spec-channel   coding prompts after a t4 genesis update carry
                         <REPOSITORY_SPECIFICATIONS>
  C2 genesis-propagation distinctive genesis.md lines appear in later prompts
  C3 floor-monotonicity  iteration pass counts never regress without a
                         ratchet.restore correcting it
  C4 plateau             N+ consecutive identical pass counts flagged
  C5 no-wipe             no "Cleaning up ... for retry" after run start
  C6 ratchet-sanity      ratchet.save strictly improves; restore only below best

Usage:
  python tools/flow_audit.py [--events logs/events.jsonl] [--prompts logs/prompts]
      [--log logs/pipeline_anyhow.log] [--project tmp/anyhow]
      [--since ISO8601] [--plateau 3]

Exit code: 1 if any check FAILs (WARN does not fail — findings, not errors).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclass
class Verdict:
    check: str
    status: str
    evidence: str


@dataclass
class RunData:
    events: list[dict] = field(default_factory=list)
    prompts_dir: Path | None = None
    log_lines: list[str] = field(default_factory=list)
    since: str = ""

    def named(self, name: str) -> list[dict]:
        return [e for e in self.events if e.get("event") == name]

    def task_done(self, task: str) -> list[dict]:
        return [e for e in self.events
                if e.get("event") == "task.done" and e.get("task") == task]

    def prompt_text(self, sha: str) -> str:
        if not self.prompts_dir or not sha:
            return ""
        p = self.prompts_dir / f"{sha}.txt"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def prompts_after(self, ts: str) -> list[tuple[str, str]]:
        """(ts, prompt_text) for every llm.request at/after `ts`."""
        out = []
        for e in self.named("llm.request"):
            if e.get("ts", "") < ts:
                continue
            sha = ((e.get("data") or {}).get("prompt") or {}).get("sha256", "")
            text = self.prompt_text(sha)
            if text:
                out.append((e["ts"], text))
        return out


def parse_run_start(log_lines: list[str]) -> str:
    """ISO timestamp of the LAST 'PIPELINE STARTED — YYYY-mm-dd HH:MM:SS UTC' line."""
    marker = "PIPELINE STARTED — "
    stamp = ""
    for line in log_lines:
        i = line.find(marker)
        if i == -1:
            continue
        raw = line[i + len(marker):].split(" UTC")[0].strip()
        # "2026-06-11 06:09:25" → ISO comparable with event ts.
        if len(raw) == 19:
            stamp = raw.replace(" ", "T")
    return stamp


def load_run(events_path: Path, prompts_dir: Path | None,
             log_path: Path | None, since: str) -> RunData:
    log_lines: list[str] = []
    if log_path and log_path.exists():
        log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not since and log_lines:
        since = parse_run_start(log_lines)
    events: list[dict] = []
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if since and rec.get("ts", "") < since:
                continue
            events.append(rec)
    # Scope the log to the run too.
    if since and log_lines:
        for i in range(len(log_lines) - 1, -1, -1):
            if "PIPELINE STARTED — " in log_lines[i]:
                log_lines = log_lines[i:]
                break
    return RunData(events=events, prompts_dir=prompts_dir,
                   log_lines=log_lines, since=since)


def iteration_series(run: RunData) -> list[tuple[str, int, int]]:
    """(ts, iteration, pass_count) per t4 completion — the per-iteration outcome."""
    out = []
    for e in run.task_done("task4"):
        d = e.get("data") or {}
        if "current_pass_count" in d:
            out.append((e.get("ts", ""), int(d.get("iteration", 0) or 0),
                        int(d["current_pass_count"])))
    return out


# --- checks -------------------------------------------------------------


def check_repo_spec_channel(run: RunData) -> Verdict:
    t4 = run.task_done("task4")
    if not t4:
        return Verdict("C1 repo-spec-channel", SKIP, "no task4 completions in run")
    if not run.prompts_dir or not run.prompts_dir.is_dir():
        return Verdict("C1 repo-spec-channel", SKIP, "prompts store not available")
    after = run.prompts_after(t4[0]["ts"])
    if not after:
        return Verdict("C1 repo-spec-channel", SKIP, "no prompts recorded after first t4")
    n_spec = sum(1 for _, text in after if "<REPOSITORY_SPECIFICATIONS>" in text)
    if n_spec == 0:
        return Verdict("C1 repo-spec-channel", FAIL,
                       f"0/{len(after)} prompts after t4 carry REPOSITORY_SPECIFICATIONS "
                       "— doc channel broken")
    return Verdict("C1 repo-spec-channel", PASS,
                   f"{n_spec}/{len(after)} prompts after first t4 carry the section")


def _is_coding_prompt(text: str) -> bool:
    """Prompts that produce code (fill/fix protocols) — excludes t4/t0b/t1
    doc prompts, which legitimately embed genesis themselves."""
    return ("<BODY>" in text or '<FN name="' in text
            or "FILE path=" in text and "corrected" in text)


def check_genesis_propagation(run: RunData, project_dir: Path | None) -> Verdict:
    t4 = run.task_done("task4")
    if not t4 or not project_dir:
        return Verdict("C2 genesis-propagation", SKIP,
                       "needs ≥1 task4 completion and --project")
    genesis = project_dir / "genesis.md"
    if not genesis.exists():
        return Verdict("C2 genesis-propagation", SKIP, "genesis.md not found")
    lines = [ln.strip() for ln in
             genesis.read_text(encoding="utf-8", errors="replace").splitlines()
             if len(ln.strip()) > 40 and not ln.strip().startswith("#")]
    probes = sorted(lines, key=len, reverse=True)[:5]
    if not probes:
        return Verdict("C2 genesis-propagation", SKIP, "genesis.md has no probe-able lines")
    after = [(ts, t) for ts, t in run.prompts_after(t4[-1]["ts"])
             if _is_coding_prompt(t)]
    if not after:
        return Verdict("C2 genesis-propagation", SKIP, "no coding prompts after last t4")
    for probe in probes:
        if any(probe in text for _, text in after):
            return Verdict("C2 genesis-propagation", PASS,
                           f"genesis line reached a coding prompt: “{probe[:60]}…”")
    return Verdict("C2 genesis-propagation", WARN,
                   f"none of {len(probes)} distinctive genesis lines found in "
                   f"{len(after)} coding prompts after last t4 (may be budget-filtered)")


def check_floor_monotonicity(run: RunData) -> Verdict:
    series = iteration_series(run)
    if len(series) < 2:
        return Verdict("C3 floor-monotonicity", SKIP,
                       f"only {len(series)} iteration(s) recorded")
    restores = run.named("ratchet.restore")
    best = series[0][2]
    for ts, it, count in series[1:]:
        if count < best:
            corrected = any(r.get("ts", "") >= ts for r in restores)
            if not corrected:
                return Verdict("C3 floor-monotonicity", FAIL,
                               f"iter {it}: pass {count} < best {best}, no ratchet.restore")
        best = max(best, count)
    return Verdict("C3 floor-monotonicity", PASS,
                   f"{len(series)} iterations, floor held at {best}")


def check_plateau(run: RunData, plateau_n: int) -> Verdict:
    series = [c for _, _, c in iteration_series(run)]
    if len(series) < plateau_n:
        return Verdict("C4 plateau", SKIP, f"fewer than {plateau_n} iterations")
    streak, worst = 1, 1
    for prev, cur in zip(series, series[1:], strict=False):
        streak = streak + 1 if cur == prev else 1
        worst = max(worst, streak)
    if worst >= plateau_n:
        return Verdict("C4 plateau", WARN,
                       f"{worst} consecutive iterations at pass={series[-1]} "
                       f"(series: {series}) — repair is not advancing")
    return Verdict("C4 plateau", PASS, f"series advances: {series}")


def check_no_wipe(run: RunData) -> Verdict:
    if not run.log_lines:
        return Verdict("C5 no-wipe", SKIP, "pipeline log not available")
    wipes = [ln for ln in run.log_lines if "generated files for retry" in ln]
    keeps = [ln for ln in run.log_lines if "tree for repair iteration" in ln]
    if wipes:
        return Verdict("C5 no-wipe", FAIL,
                       f"{len(wipes)} retry wipe(s) in this run — first: {wipes[0].strip()[:80]}")
    return Verdict("C5 no-wipe", PASS,
                   f"0 retry wipes; {len(keeps)} repair-iteration keep(s)")


def check_ratchet_sanity(run: RunData) -> Verdict:
    saves = run.named("ratchet.save")
    restores = run.named("ratchet.restore")
    if not saves and not restores:
        return Verdict("C6 ratchet-sanity", SKIP, "no ratchet events in run")
    prev = -1
    for e in saves:
        cnt = int((e.get("data") or {}).get("pass_count", -1))
        if cnt <= prev:
            return Verdict("C6 ratchet-sanity", FAIL,
                           f"ratchet.save pass_count {cnt} not above previous {prev}")
        prev = cnt
    for e in restores:
        d = e.get("data") or {}
        if int(d.get("current", -1)) >= int(d.get("best", -1)):
            return Verdict("C6 ratchet-sanity", FAIL,
                           f"ratchet.restore with current {d.get('current')} ≥ best {d.get('best')}")
    return Verdict("C6 ratchet-sanity", PASS,
                   f"{len(saves)} save(s) strictly improving, {len(restores)} restore(s) valid")


def audit(events_path: Path, prompts_dir: Path | None, log_path: Path | None,
          project_dir: Path | None, since: str, plateau_n: int) -> list[Verdict]:
    run = load_run(events_path, prompts_dir, log_path, since)
    return [
        check_repo_spec_channel(run),
        check_genesis_propagation(run, project_dir),
        check_floor_monotonicity(run),
        check_plateau(run, plateau_n),
        check_no_wipe(run),
        check_ratchet_sanity(run),
    ]


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", type=Path, default=here / "logs/events.jsonl")
    ap.add_argument("--prompts", type=Path, default=here / "logs/prompts")
    ap.add_argument("--log", type=Path, default=here / "logs/pipeline_anyhow.log")
    ap.add_argument("--project", type=Path, default=None,
                    help="workspace dir (enables genesis-propagation probe)")
    ap.add_argument("--since", default="", help="ISO ts; default: last PIPELINE STARTED in --log")
    ap.add_argument("--plateau", type=int, default=3)
    args = ap.parse_args(argv)

    verdicts = audit(args.events, args.prompts, args.log,
                     args.project, args.since, args.plateau)
    width = max(len(v.check) for v in verdicts)
    failed = False
    for v in verdicts:
        print(f"{v.check.ljust(width)}  {v.status:4s}  {v.evidence}")
        failed |= v.status == FAIL
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
