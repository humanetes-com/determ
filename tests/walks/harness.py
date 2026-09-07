"""Replay a distilled walk through the real TDD control loop.

A walk fixture (`tests/fixtures/walks/*.json`, written by
`tools/distill_walk.py`) is one cluster's iterations as a recorded run printed
them: pass/fail/oracle, the frontier and exhausted counts, and the decisions the
loop took — a retrigger firing and how many bodies it reverted, a withheld
ratchet breach, a re-admission granted or refused. This module feeds that
sequence back through `run_phase_c_tdd` with cargo and the model stubbed, so a
nine-hour deadlock is decided in milliseconds.

**What is recorded and what is modelled.** The pass/fail counts, the oracle
sizes, the retrigger's iteration and its reverted-body count are read from the
log and replayed verbatim. Three things are not, and each is a deliberate model
stated here rather than hidden in the fixture:

* *The entry high-water.* The live run reached 81 by resuming a persisted floor
  from an earlier process. Reproducing the resume would mean satisfying
  `resume_floor`'s tree-sha/oracle-size/code-id guard, which is machinery this
  walk is not about. Instead the replay opens with one synthetic sweep AT the
  recorded entry best, so the high-water is earned by a measurement. The
  lineage the walk asserts on — a high-water, a deliberate re-stub below it,
  a re-climb — is identical either way.
* *The iteration the retrigger swallowed.* Every retrigger path `continue`s
  before the loop's own iteration emit, so the iteration a crash fired in
  printed no `cargo test rc=` line. When the distiller recovered that
  iteration's reading from its `binaries:` breakdown the fixture carries it as
  `crash_passed` and the replay serves THAT score over a SHRUNKEN oracle —
  run `58819-ee78a1d1`'s 79 -> 74 destruction lineage exists only in those
  readings. Without one, the replay falls back to holding the previous score
  over the shrunken oracle, which is what a crashed binary is and what the
  retrigger fires on.
* *Test identity.* The oracle is `t1..tN` here, not the crate's real test names.
  A walk asserts on counts and decisions; nothing in it turns on which test.
* *Chronic vanished names.* `oracle_size` reads the largest oracle any
  iteration REPORTED, which under-counts a run whose crash never heals: run
  `635043-5f7c61f5` reported 101 on every intact sweep because 5 of the
  106-test inventory vanished on EVERY sweep (the deterministic
  `test_downcast` fault, which an earlier reading had called "capped at
  101 by design"). Replaying
  that run at inventory 101 serves sweeps with nothing vanished, so the
  crash machinery under test is never armed. A walk about that machinery
  passes `inventory=` (the true count) and `chronic_vanished=` (how many
  names never report) to `replay`; recorded pass/fail rows are still served
  verbatim — the model only adds the names that never appear.
* *The failing tests' words.* Each FAILED test gets a synthetic libtest
  stdout section with a one-line panic, so the loop's own evidence
  extraction (`failure_evidence_by_test` → `FailureFrontier.evidence`)
  carries an entry per failing test — the seam the pair-evidence walk
  asserts on. The text is synthetic exactly as the names are; nothing in a
  walk turns on what it says, only on it being present.

Past the first iteration where the fix changes what the loop does, the recording
stops describing anything real — the pipeline would have filled different fns
and scored differently. So a walk may asserts on DECISIONS reachable from the
prefix, never on a score the recording cannot predict.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "walks"


def load_walk(name: str) -> dict[str, Any]:
    """Read the fixture `name` (with or without the .json suffix)."""
    path = FIXTURES / (name if name.endswith(".json") else f"{name}.json")
    return json.loads(path.read_text(encoding="utf-8"))


def render_sweep(passed: int, named: int) -> str:
    """A cargo run where `named` tests reported and `passed` of them passed.

    `named` below the inventory is a crashed binary: the tests that never ran
    are absent from the output rather than reported as failures, which is the
    shape the oracle reconciliation reads a shrink from.
    """
    lines = [f"test t{i} ... {'ok' if i <= passed else 'FAILED'}"
             for i in range(1, named + 1)]
    for i in range(passed + 1, named + 1):
        lines.append(f"---- t{i} stdout ----")
        lines.append(f"thread 't{i}' panicked at tests/walk.rs:1:1:")
        lines.append(f"assertion failed in t{i}")
        lines.append("")
    verdict = "ok" if passed == named else "FAILED"
    lines.append(f"test result: {verdict}. {passed} passed; "
                 f"{named - passed} failed; 0 ignored")
    return "\n".join(lines)


def oracle_size(walk: dict[str, Any]) -> int:
    """The full test inventory: the largest oracle any iteration reported.

    Taken as a maximum because a crashed sweep reports fewer — the inventory is
    what the cluster HAS, and a shrink is measured against it.
    """
    return max((it["oracle"] for it in walk["iterations"] if "oracle" in it),
               default=0)


def build_sweeps(walk: dict[str, Any], *, inventory: int | None = None,
                 chronic_vanished: int = 0) -> list[str]:
    """One cargo output per LOOP iteration, in order.

    Index 0 is the synthetic entry sweep at `entry_best`; the walk's own
    iterations follow, so loop iteration N+1 replays walk iteration N.

    `inventory`/`chronic_vanished` are the chronic-crash model (see the
    module docstring): recorded rows are served verbatim — a recorded
    `passed`/`failed` pair already reflects the chronic shrink — while the
    synthetic entry sweep and the swallowed crash iterations are rendered
    against the reporting ceiling `inventory - chronic_vanished`.
    """
    inv = inventory if inventory is not None else oracle_size(walk)
    reporting = inv - chronic_vanished
    sweeps = [render_sweep(min(walk["entry_best"], reporting), reporting)]
    held = walk["entry_best"]
    for it in walk["iterations"]:
        if "passed" in it:
            held = it["passed"]
            sweeps.append(render_sweep(it["passed"], it["passed"] + it["failed"]))
        elif "crash_passed" in it:
            # The iteration whose emit a retrigger swallowed, with its reading
            # recovered from the recorded `binaries:` breakdown: the recorded
            # score over a short oracle.
            held = it["crash_passed"]
            sweeps.append(render_sweep(min(held, reporting - 1), reporting - 1))
        else:
            # The iteration whose emit a retrigger swallowed: a crashed binary
            # holding the previous score over a short oracle.
            sweeps.append(render_sweep(min(held, reporting - 1), reporting - 1))
    return sweeps


def retrigger_iteration(walk: dict[str, Any]) -> int | None:
    """The LOOP iteration the recorded retrigger fired in (1-based), or None."""
    for offset, it in enumerate(walk["iterations"], start=2):
        if "retrigger" in it:
            return offset
    return None


def scripted_retrigger(src: Path, walk: dict[str, Any], logs: list[str]):
    """A retrigger that fires each RECORDED fire, at its recorded iteration.

    It re-stubs by rewriting the tree, exactly as the real one does: the point
    of the walk is that the loop's own destruction invalidates its high-water,
    and a retrigger that fired without reverting anything invalidates nothing.

    Fires are consumed in recorded order, each no earlier than its recorded
    LOOP iteration (walk iteration + 1, per `build_sweeps`'s synthetic entry).
    The iteration key is what keeps a multi-fire walk on the recording: run
    `58819-ee78a1d1` fired at walk iters 3 and 5 with an unrecorded REJECTED
    fire (no destruction, no fixture line) absorbing the earlier crash sweeps
    — a stub firing on the first call would land walk iter 3's destruction one
    iteration early and drift every baseline after it. `>=` rather than `==`
    tolerates a call arriving a grace-iteration late; a call before the next
    recorded fire's iteration is simply not armed, which is also what the
    recorded run's rejected/capped walks amounted to."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    queue = [(offset, it["retrigger"])
             for offset, it in enumerate(walk["iterations"], start=2)
             if "retrigger" in it]

    def _cur_iter() -> int:
        for line in reversed(logs):
            if "running cargo test" in line and "iter " in line:
                return int(line.split("iter ", 1)[1].split(":", 1)[0])
        return 0

    def _retrig(*a: Any, **k: Any) -> RetriggerResult:
        if not queue or _cur_iter() < queue[0][0]:
            return RetriggerResult(fired=False, reason="not armed")
        _, recorded = queue.pop(0)
        (src / "a.rs").write_text("RESTUBBED", encoding="utf-8")
        return RetriggerResult(fired=True, succeeded=True,
                               reverted_count=int(recorded["reverted"]),
                               cause=str(recorded["cause"]))
    return _retrig


def replay(walk: dict[str, Any], monkeypatch, tmp_path: Path, *,
           events: list[tuple[str, dict]], max_stalls: str = "3",
           refill_max: str = "99", reset_returns: list[str] | None = None,
           retrigger=None, localize=None, escalate=None, reverse_map=None,
           fill=None, inventory: int | None = None,
           chronic_vanished: int = 0, gpu_guard: str = "0",
           no_verdict: str = "0", flat_window: str = "5",
           flat_require_traversal: str = "0", no_frontier_stop: str = "2",
           reset_cap: str = "2", backstop: str = "10000",
           fill_phase_minutes: str = "0",
           dispatch_cap: str = "0", floor: dict | None = None,
           frontier: dict | None = None, enumerate_fns=None,
           coverage=None, attribute=None, recorded=None) -> list[str]:
    """Drive `run_phase_c_tdd` over the walk. Returns the loop's log lines.

    The cargo stub reads the loop's own `iter N: running cargo test` line to
    decide which sweep to serve, so the ratchet's confirm-before-revert
    re-sweep re-serves its iteration's reading instead of consuming the next
    one. Scripting per cargo CALL instead would shift every reading after the
    first breach and quietly change which iteration measured what.
    """
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster, FnLocator

    # Import here rather than at module scope: `tests.walks.harness` is
    # imported by collection, and the driver deps helper lives in a test module.
    from tests.test_tdd_ratchet import _driver_deps

    sweeps = build_sweeps(walk, inventory=inventory,
                          chronic_vanished=chronic_vanished)
    inventory = inventory if inventory is not None else oracle_size(walk)
    names = frozenset(f"t{i}" for i in range(1, inventory + 1))
    logs: list[str] = []
    cur = [0]

    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        for line in reversed(logs):
            if "running cargo test" in line and "iter " in line:
                cur[0] = int(line.split("iter ", 1)[1].split(":", 1)[0])
                break
        return 101, sweeps[min(max(cur[0], 1) - 1, len(sweeps) - 1)]

    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    # The flat-score GPU guard is OFF by default here, and that is a property of
    # what a walk IS: a fixed observation sequence recorded from a run that had
    # no guard. Left on, it would stop most replays at their second identical
    # reading and every assertion after that point would be about a decision the
    # recorded run never faced. A walk ABOUT the guard turns it on.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", gpu_guard)
    # ...and so is every OTHER stop the loop reads, for the same reason and
    # because leaving one ambient makes a walk's decision sequence depend on an
    # UNTRACKED file. `.env` on this box carries TDD_NO_VERDICT_SWEEPS=0, so
    # every walk here ran with the no-verdict clock off; a bare checkout gets
    # the production default 3, and on 2026-09-03 CI both assertions of the
    # empty-frontier walk failed with the iteration-kind vector
    # ["no_frontier", "no_frontier", "no_frontier_readmit", "no_frontier",
    # "no_verdict_stop"] — the clock stopped the replay at iteration 5, before
    # the second re-admission the walk exists to pin and long before the
    # backstop its negative control asserts. Reproduced byte-identically here
    # with `TDD_NO_VERDICT_SWEEPS=3 pytest tests/walks/`. The flat window and
    # its traversal requirement are the same latency: inert while
    # TDD_FLAT_SCORE_REPEATS is 0, and read from `.env` (window 10, not the
    # default 5) the moment a walk turns the guard on. So the harness pins the
    # whole set, and a walk ABOUT one of them passes it as an argument.
    monkeypatch.setenv("TDD_NO_VERDICT_SWEEPS", no_verdict)
    monkeypatch.setenv("TDD_FLAT_SCORE_WINDOW", flat_window)
    monkeypatch.setenv("TDD_FLAT_SCORE_REQUIRE_TRAVERSAL",
                       flat_require_traversal)
    monkeypatch.setenv("TDD_NO_FRONTIER_STOP", no_frontier_stop)
    monkeypatch.setenv("RETRO_RESET_MAX_ATTEMPTS", reset_cap)
    monkeypatch.setenv("PHASE_C_TDD_BACKSTOP", backstop)
    monkeypatch.setenv("TDD_FILL_PHASE_MAX_MINUTES", fill_phase_minutes)
    # The dispatch cap is OFF here for the identical reason, and leaving it at
    # its production default was an asymmetry rather than a decision: a walk is
    # a fixed observation sequence recorded from runs whose iterations spent the
    # whole frontier, so a cap that withholds part of it makes every later
    # assertion describe a dispatch the recorded run never made. It happens to
    # be inert today — every walk drives the one-fn frontier below, where the
    # batch always completes the pass — which is exactly why it has to be
    # PINNED: the first walk to widen its frontier would otherwise change replay
    # silently. A walk ABOUT the cap sets it.
    monkeypatch.setenv("PHASE_C_TDD_MAX_DISPATCH", dispatch_cap)
    # One-fn frontier: without a raised cap the behavioural re-fill limit
    # exhausts it two iterations in and every later iteration skips the fill
    # phase, starving the re-admission gate of the "fills landed" half of its
    # rule for reasons that have nothing to do with most walks. A walk that IS
    # about the cap (the two-store re-admission) lowers it via `refill_max`.
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", refill_max)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")

    cluster_name = walk["cluster"]
    run_phase_c_tdd(
        _driver_deps(_cargo, lambda pd, cl, stems, lib=False: (inventory, names),
                     events,
                     retrigger if retrigger is not None
                     else scripted_retrigger(src, walk, logs), logs=logs,
                     frontier=(frontier if frontier is not None
                               else {FnLocator("src/a.rs", "f"): {"t2"}}),
                     enumerate_fns=enumerate_fns,
                     fills_per_iter=2, reset_returns=reset_returns,
                     localize=localize, escalate=escalate,
                     reverse_map=reverse_map, fill=fill,
                     coverage=coverage, attribute=attribute,
                     recorded=recorded,
                     # A shared mutable dict models the on-disk floor file: a
                     # walk about the process BOUNDARY replays two processes
                     # against the same dict (`tests/walks/
                     # test_walk_escalation_lock.py`); None keeps every other
                     # walk floor-less, exactly as before.
                     floor=floor),
        project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name=cluster_name, env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag=f"[{cluster_name}]")
    return logs


def refusals_against(logs: list[str], best: int) -> list[str]:
    """Log lines refusing a re-admission because the score has not passed
    `best` — the deadlock's own signature, and what a walk's negative control
    reproduces."""
    return [ln for ln in logs
            if "NOT re-admitting" in ln and f"no further than {best}" in ln]
