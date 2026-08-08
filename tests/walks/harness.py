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
  printed no `cargo test rc=` line. The replay serves it a sweep that holds the
  previous score over a SHRUNKEN oracle, which is what a crashed binary is and
  what the retrigger fires on.
* *Test identity.* The oracle is `t1..tN` here, not the crate's real test names.
  A walk asserts on counts and decisions; nothing in it turns on which test.

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


def build_sweeps(walk: dict[str, Any]) -> list[str]:
    """One cargo output per LOOP iteration, in order.

    Index 0 is the synthetic entry sweep at `entry_best`; the walk's own
    iterations follow, so loop iteration N+1 replays walk iteration N.
    """
    inventory = oracle_size(walk)
    sweeps = [render_sweep(walk["entry_best"], inventory)]
    held = walk["entry_best"]
    for it in walk["iterations"]:
        if "passed" in it:
            held = it["passed"]
            sweeps.append(render_sweep(it["passed"], it["passed"] + it["failed"]))
        else:
            # The iteration whose emit a retrigger swallowed: a crashed binary
            # holding the previous score over a short oracle.
            sweeps.append(render_sweep(min(held, inventory - 1), inventory - 1))
    return sweeps


def retrigger_iteration(walk: dict[str, Any]) -> int | None:
    """The LOOP iteration the recorded retrigger fired in (1-based), or None."""
    for offset, it in enumerate(walk["iterations"], start=2):
        if "retrigger" in it:
            return offset
    return None


def scripted_retrigger(src: Path, walk: dict[str, Any]):
    """A retrigger that fires once and re-stubs the recorded number of bodies.

    It re-stubs by rewriting the tree, exactly as the real one does: the point
    of the walk is that the loop's own destruction invalidates its high-water,
    and a retrigger that fired without reverting anything invalidates nothing.
    """
    from pipeline_core.application.retrigger_layout import RetriggerResult
    fired: list[int] = []
    recorded = next((it["retrigger"] for it in walk["iterations"]
                     if "retrigger" in it), None)

    def _retrig(*a: Any, **k: Any) -> RetriggerResult:
        if recorded is None or fired:
            return RetriggerResult(fired=False, reason="capped")
        fired.append(1)
        (src / "a.rs").write_text("RESTUBBED", encoding="utf-8")
        return RetriggerResult(fired=True, succeeded=True,
                               reverted_count=int(recorded["reverted"]),
                               cause=str(recorded["cause"]))
    return _retrig


def replay(walk: dict[str, Any], monkeypatch, tmp_path: Path, *,
           events: list[tuple[str, dict]], max_stalls: str = "3",
           ) -> list[str]:
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

    sweeps = build_sweeps(walk)
    inventory = oracle_size(walk)
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
    # One-fn frontier: without a raised cap the behavioural re-fill limit
    # exhausts it two iterations in and every later iteration skips the fill
    # phase, starving the re-admission gate of the "fills landed" half of its
    # rule for reasons that have nothing to do with the walk.
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")

    cluster_name = walk["cluster"]
    run_phase_c_tdd(
        _driver_deps(_cargo, lambda pd, cl, stems, lib=False: (inventory, names),
                     events, scripted_retrigger(src, walk), logs=logs,
                     frontier={FnLocator("src/a.rs", "f"): {"t2"}},
                     fills_per_iter=2),
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
