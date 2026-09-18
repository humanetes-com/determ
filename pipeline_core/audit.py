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
from typing import Any

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclass
class Verdict:
    check: str
    status: str
    evidence: str


@dataclass
class RunData:
    events: list[dict[str, Any]] = field(default_factory=list)
    prompts_dir: Path | None = None
    log_lines: list[str] = field(default_factory=list)
    since: str = ""
    corrupt_event_lines: int = 0  # unparseable events.jsonl lines (torn writes)

    def named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]

    def task_done(self, task: str) -> list[dict[str, Any]]:
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


def parse_run_id(log_lines: list[str]) -> str:
    """`run_id=<id>` of the LAST 'PIPELINE STARTED' line, or "" when
    absent (older logs). When present, `load_run` scopes events by this identity
    so a concurrent writer (e.g. an xdist worker on the SHARED events.jsonl) can
    never leak into the run's audit window."""
    marker = "PIPELINE STARTED — "
    token = "run_id="
    run_id = ""
    for line in log_lines:
        if marker not in line:
            continue
        j = line.find(token)
        if j != -1:
            run_id = line[j + len(token):].split()[0].strip()
    return run_id


def load_run(events_path: Path, prompts_dir: Path | None,
             log_path: Path | None, since: str) -> RunData:
    log_lines: list[str] = []
    if log_path and log_path.exists():
        log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not since and log_lines:
        since = parse_run_start(log_lines)
    # When the run's log carries a run_id, scope events by IDENTITY.
    # An event that carries a DIFFERENT run_id is another process (a concurrent
    # xdist worker on the shared events.jsonl) and is dropped regardless of ts;
    # an event with NO run_id (older format) falls back to the ts window.
    run_id = parse_run_id(log_lines) if log_lines else ""
    events: list[dict[str, Any]] = []
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
            rec_run = rec.get("run_id", "")
            if run_id and rec_run and rec_run != run_id:
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


#: Series keys for the loops that write no task4 completion.
POST_MERGE_SERIES = "post-merge"
TDD_SERIES_PREFIX = "tdd:"


def _t4_series(run: RunData) -> list[tuple[str, int, int]]:
    out = []
    for e in run.task_done("task4"):
        d = e.get("data") or {}
        if "current_pass_count" in d:
            out.append((e.get("ts", ""), int(d.get("iteration", 0) or 0),
                        int(d["current_pass_count"])))
    return out


def _loop_series(run: RunData, event: str, key_of: Any) -> dict[str, list[tuple[str, int, int]]]:
    out: dict[str, list[tuple[str, int, int]]] = {}
    for e in run.named(event):
        d = e.get("data") or {}
        if "pass_count" not in d:
            continue
        out.setdefault(key_of(d), []).append(
            (e.get("ts", ""), int(d.get("iter", 0) or 0), int(d["pass_count"])))
    return out


def iteration_series_by_loop(run: RunData) -> dict[str, list[tuple[str, int, int]]]:
    """Every per-iteration pass series the run recorded, keyed by the loop
    that wrote it: ``task4`` (repair-loop completions, `current_pass_count`),
    ``post-merge`` (`post_merge.fill.iter`), and ``tdd:<cluster>`` for each
    cluster's `phase_c.tdd.iter`.

    The audit used to read task4 completions only. The TDD loop emits
    `phase_c.tdd.iter` and never a task4 completion, so on a `restart_from
    task2a` run every periodic report printed `C3 … SKIP only 0
    iteration(s) recorded` / `C4 … SKIP fewer than 3 iterations` while the
    loop recorded three sweeps (run `3014175-d98ed09e`, 2026-09-08, 20
    reports over 06:37–12:00 UTC). Each cluster is its own series: clusters
    score different oracles, so one merged series would read a cluster
    switch as a floor breach."""
    out: dict[str, list[tuple[str, int, int]]] = {}
    t4 = _t4_series(run)
    if t4:
        out["task4"] = t4
    for key, series in _loop_series(
            run, "post_merge.fill.iter", lambda _d: POST_MERGE_SERIES).items():
        out[key] = series
    for key, series in _loop_series(
            run, "phase_c.tdd.iter",
            lambda d: TDD_SERIES_PREFIX + str(d.get("cluster") or "")).items():
        out[key] = series
    return out


def iteration_series(run: RunData) -> list[tuple[str, int, int]]:
    """(ts, iteration, pass_count) per iteration of the run's main loop:
    task4 completions when the run has them, else the post-merge wave's
    iterations, else the longest cluster series of the TDD loop. The
    per-loop view is :func:`iteration_series_by_loop`."""
    by_loop = iteration_series_by_loop(run)
    if "task4" in by_loop:
        return by_loop["task4"]
    if POST_MERGE_SERIES in by_loop:
        return by_loop[POST_MERGE_SERIES]
    tdd = [v for k, v in by_loop.items() if k.startswith(TDD_SERIES_PREFIX)]
    if not tdd:
        return []
    return max(tdd, key=len)


def _deliberate_drops(run: RunData) -> set[str]:
    """Timestamps of iteration events whose score describes a tree the
    loop deliberately took apart (a crash/layout retrigger's re-stub,
    `retrigger_reverted > 0` or a `retrigger_*` kind): the reading after
    such an event is a new floor, not a breach of the old one."""
    out: set[str] = set()
    for e in run.named("phase_c.tdd.iter"):
        d = e.get("data") or {}
        if int(d.get("retrigger_reverted", 0) or 0) > 0 or str(
                d.get("kind", "")).startswith("retrigger"):
            out.add(e.get("ts", ""))
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
    """Every loop's series is checked on its own; the verdict is the worst.
    A drop at an iteration the loop deliberately re-stubbed (a retrigger)
    re-bases the floor instead of breaching it — a deliberate destruction
    invalidates the measurement for every consumer, this one included."""
    by_loop = iteration_series_by_loop(run)
    checkable = {k: v for k, v in by_loop.items() if len(v) >= 2}
    if not checkable:
        n = max((len(v) for v in by_loop.values()), default=0)
        return Verdict("C3 floor-monotonicity", SKIP,
                       f"only {n} iteration(s) recorded")
    restores = run.named("ratchet.restore")
    rebased = _deliberate_drops(run)
    held: list[str] = []
    for key, series in sorted(checkable.items()):
        best = series[0][2]
        for ts, it, count in series[1:]:
            if ts in rebased:
                best = count
                continue
            if count < best:
                corrected = any(r.get("ts", "") >= ts for r in restores)
                if not corrected:
                    return Verdict("C3 floor-monotonicity", FAIL,
                                   f"{key} iter {it}: pass {count} < best {best}, "
                                   "no ratchet.restore")
            best = max(best, count)
        held.append(f"{key}: {len(series)} iterations, floor held at {best}")
    return Verdict("C3 floor-monotonicity", PASS, "; ".join(held))


def check_plateau(run: RunData, plateau_n: int) -> Verdict:
    """Per loop; WARN on the first loop whose series repeats a score
    `plateau_n` times in a row."""
    by_loop = iteration_series_by_loop(run)
    checkable = {k: [c for _, _, c in v] for k, v in by_loop.items()
                 if len(v) >= plateau_n}
    if not checkable:
        return Verdict("C4 plateau", SKIP, f"fewer than {plateau_n} iterations")
    advancing: list[str] = []
    for key, series in sorted(checkable.items()):
        streak, worst = 1, 1
        for prev, cur in zip(series, series[1:], strict=False):
            streak = streak + 1 if cur == prev else 1
            worst = max(worst, streak)
        if worst >= plateau_n:
            return Verdict("C4 plateau", WARN,
                           f"{key}: {worst} consecutive iterations at "
                           f"pass={series[-1]} (series: {series}) — repair is "
                           "not advancing")
        advancing.append(f"{key}: {series}")
    return Verdict("C4 plateau", PASS, "series advances: " + "; ".join(advancing))


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
    weakening the functional-requirements signal."""
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
    truncate/interleave one stream file. The collision REQUIRES two distinct OS
    threads under one name; an overlap on a SINGLE thread (reentrant or
    out-of-order emission — e.g. a retrospective ask logged before its own
    response) shares no stream and is benign. So a same-NAME overlap is flagged
    only when the two open requests carry a different `tid` (os thread id);
    events without `tid` (pre-instrumentation) fall back to the original
    name-only rule. Torn events.jsonl lines (concurrent appends) are reported
    too. SKIPs when the run never overlapped two distinct threads — no
    parallelism exercised, nothing verified."""
    calls = [e for e in run.events
             if e.get("event") in ("llm.request", "llm.response", "llm.error")]
    if not calls:
        return Verdict("C10 parallel-streams", SKIP, "no llm events in run")
    if not any(e.get("thread") for e in calls):
        return Verdict("C10 parallel-streams", SKIP,
                       "events carry no thread field (pre-parallelism pipeline)")
    # Per thread-NAME, the stack of currently-open request tids. A request whose
    # tid differs from one already open under its name = two OS threads on one
    # stream file = collision. Missing tid → a per-event unique sentinel, so the
    # pre-instrumentation behaviour (any same-name overlap flagged) is preserved.
    open_by_name: dict[str, list[object]] = {}
    violations: list[str] = []
    max_concurrent = 0
    for e in calls:
        t = e.get("thread", "?")
        tid: object = e["tid"] if e.get("tid") is not None else ("notid", id(e))
        if e["event"] == "llm.request":
            stack = open_by_name.setdefault(t, [])
            if any(o != tid for o in stack):
                violations.append(f"{t} @ {e.get('ts', '?')}")
            stack.append(tid)
            live = {o for lst in open_by_name.values() for o in lst}
            max_concurrent = max(max_concurrent, len(live))
        else:
            closing = open_by_name.get(t)
            if closing:
                if tid in closing:
                    closing.remove(tid)
                else:
                    closing.pop()
                if not closing:
                    open_by_name.pop(t, None)
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


