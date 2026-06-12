"""Flow-audit core — mechanical verdicts on a run's FLOW invariants, read
from what the run records (events.jsonl, the content-addressed prompts store,
the pipeline log). Shared by `tools/flow_audit.py` (CLI), the pipeline's
--audit self-audit, and the monitor's audit row. See tools/flow_audit.py for
the check catalogue. Foundational module (log.py pattern): outside the layer
contract, stdlib-only, read-only."""

from __future__ import annotations

import json
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
    corrupt_event_lines: int = 0  # unparseable events.jsonl lines (torn writes)

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
    corrupt = 0
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                corrupt += 1
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
                   log_lines=log_lines, since=since,
                   corrupt_event_lines=corrupt)


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


def check_cluster_coverage(run: RunData) -> Verdict:
    """C7: no task0c cluster pass may end at 0% coverage. A 0% cluster means
    its augmentation was skipped wholesale (e.g. broken harness) — the test
    oracle for that cluster's cfg-gated code is then missing, silently
    weakening FR-018."""
    done = run.task_done("task0c")
    if not done:
        return Verdict("C7 cluster-coverage", SKIP, "no task0c completions in run")
    zeros = []
    for e in done:
        d = e.get("data") or {}
        pct = float(d.get("coverage_pct", -1))
        if pct == 0.0:
            zeros.append(f"{e.get('cluster', '?')}/{e.get('module', '?')}"
                         + (f" ({d.get('blocked_reason')})" if d.get("blocked_reason") else ""))
    if zeros:
        return Verdict("C7 cluster-coverage", FAIL,
                       f"{len(zeros)} task0c pass(es) at 0%: {', '.join(zeros[:4])}")
    return Verdict("C7 cluster-coverage", PASS,
                   f"{len(done)} task0c completion(s), none at 0%")


def check_harness_fix_effect(run: RunData) -> Verdict:
    """C8 (intent-vs-effect): every CLAIMED harness fix attempt must have
    acted — sent at least one prompt. A claimed attempt with 0 prompts is a
    no-op loop: the log says "sending compile error to LLM" but every broken
    file was filtered out (the class behind the nightly-cluster 0%)."""
    attempts = run.named("task0c.harness_fix.attempt")
    if not attempts:
        return Verdict("C8 harness-fix-effect", SKIP, "no harness fix attempts in run")
    noops = [e for e in attempts
             if int((e.get("data") or {}).get("files_prompted", 0)) == 0]
    if noops:
        d = noops[0].get("data") or {}
        return Verdict("C8 harness-fix-effect", FAIL,
                       f"{len(noops)}/{len(attempts)} claimed attempt(s) sent 0 prompts "
                       f"(first: module={d.get('module')} attempt={d.get('attempt')} "
                       f"broken_files={d.get('broken_files')})")
    return Verdict("C8 harness-fix-effect", PASS,
                   f"{len(attempts)} attempt(s), all sent ≥1 prompt")


def check_success_evidence(run: RunData) -> Verdict:
    """C9 (intent-vs-effect): a 'SUCCESS: all tests passed' claim in the log
    must be backed by the LAST task3 task.done event having passed=true —
    i.e. the per-cluster strict verdict, not a re-parse of some other
    command's output. Catches the 2026-06-12 microcrate false SUCCESS:
    task3 failed per-cluster (21/24), the post-task3 fix loop validated with
    the single default-cluster command, and the run printed SUCCESS and
    exited 0 with zero fix events in between."""
    claims = [ln for ln in run.log_lines if "SUCCESS: all tests passed" in ln]
    if not claims:
        return Verdict("C9 success-evidence", SKIP, "no success claim in log")
    t3 = run.task_done("task3")
    if not t3:
        return Verdict("C9 success-evidence", FAIL,
                       "log claims success but no task3 completion recorded")
    last = t3[-1].get("data") or {}
    if not last.get("passed"):
        return Verdict("C9 success-evidence", FAIL,
                       f"log claims success but last task3 has passed="
                       f"{last.get('passed')} (pass={last.get('pass_count')}, "
                       f"fail={last.get('fail_count')})")
    return Verdict("C9 success-evidence", PASS,
                   f"success claim backed by task3 pass "
                   f"({last.get('pass_count')}/{last.get('expected_count')} expected)")


def check_parallel_stream_integrity(run: RunData) -> Verdict:
    """C10: concurrent LLM calls must each own a distinctly-named thread.

    Per-thread stream files (model_interact._stream_path_for_thread) and the
    event `thread` field both key on the thread NAME. Two live calls sharing a
    name (e.g. two parallel clusters each spawning 'per-fn-worker-0') silently
    truncate/interleave one stream file. From events.jsonl that collision is
    visible as broken request→response alternation within one thread name:
    a real thread blocks for the duration of its call, so back-to-back
    llm.request events under one name with no llm.response/llm.error between
    them mean two threads wrote under that name (or a call vanished — also a
    failure). Torn events.jsonl lines (concurrent appends) are reported too.
    SKIPs when the run never overlapped two calls — no parallelism exercised,
    nothing verified."""
    calls = [e for e in run.events
             if e.get("event") in ("llm.request", "llm.response", "llm.error")]
    if not calls:
        return Verdict("C10 parallel-streams", SKIP, "no llm events in run")
    if not any(e.get("thread") for e in calls):
        return Verdict("C10 parallel-streams", SKIP,
                       "events carry no thread field (pre-parallelism pipeline)")
    open_threads: set = set()
    violations: list[str] = []
    max_concurrent = 0
    for e in calls:
        t = e.get("thread", "?")
        if e["event"] == "llm.request":
            if t in open_threads:
                violations.append(f"{t} @ {e.get('ts', '?')}")
            open_threads.add(t)
            max_concurrent = max(max_concurrent, len(open_threads))
        else:
            open_threads.discard(t)
    if violations:
        return Verdict("C10 parallel-streams", FAIL,
                       f"{len(violations)} same-thread overlap(s) — stream file "
                       f"collision (first: {violations[0]})")
    if run.corrupt_event_lines:
        return Verdict("C10 parallel-streams", WARN,
                       f"no stream collisions, but {run.corrupt_event_lines} "
                       f"torn events.jsonl line(s)")
    if max_concurrent < 2:
        return Verdict("C10 parallel-streams", SKIP,
                       "no concurrent llm calls observed (parallelism not exercised)")
    return Verdict("C10 parallel-streams", PASS,
                   f"{len(calls)} llm event(s), max {max_concurrent} concurrent, "
                   f"every overlap on a distinct thread")


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
        check_cluster_coverage(run),
        check_harness_fix_effect(run),
        check_success_evidence(run),
        check_parallel_stream_integrity(run),
    ]


