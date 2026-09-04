"""Exhaustion charging must not outrun dispatch (TDD driver).

On 2026-08-11/12 every A/B/C cluster run (micro and hardcrate, three model
arms) plateaued below its oracle on the same impl-block members: the impl
route deferred itself at iteration 1 behind free-fn blockers, the per-fn route
never takes impl members, and the driver then charged an exhaustion strike to
every frontier key still a stub after the pass — including the members no
route had attempted. From iteration 2 their block read "0 impl block(s)
viable" and the loop spent its stall budget on an empty frontier while
logging `candidates(pre-exhaust-filter)=3`. Post-merge, the identical members
filled on attempt 1.

The fix: the engine reports `ClusterFillResult.dispatched_fns` (fns a route
genuinely attempted); the driver charges exhaustion, counts re-fill attempts
and records the stall-exit conviction window only for reported keys. `None`
(engine did not report) preserves the legacy frontier==dispatched fallback —
the negative control below pins that the old burn still reproduces under it,
which is what proves the gate keys on the report.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.domain.models import ClusterFillResult, FnLocator
from pipeline_core.domain.stub_inventory import StubInventory
from tests.test_tdd_ratchet import _driver_deps

_KEY = FnLocator("src/a.rs", "f")

_FAILING = ("test t1 ... ok\ntest t2 ... FAILED\n"
            "test result: FAILED. 1 passed; 1 failed; 0 ignored")


def _run(monkeypatch, tmp_path: Path, events, logs, *,
         dispatched: tuple[tuple[str, str], ...] | None,
         max_stalls: str = "2",
         cluster_stubs: list[FnLocator] | None = None,
         answer_lost: tuple[tuple[str, str], ...] = (),
         reverted: tuple[tuple[str, str], ...] = (),
         as_stub: bool = True) -> None:
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    # The flat-score GPU guard would stop these runs at their second
    # identical reading, which is exactly what it is for and exactly what
    # this module is not about: the mechanisms under test here sit BELOW
    # it in the same iteration (the plateau budget, the ratchet, the
    # conviction rules), so the guard is off and their controls keep
    # measuring what they were written to measure. `tests/test_tdd_gpu_guard.py`
    # owns the guard itself.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")

    def _cargo(*a: Any, **k: Any):
        return 101, _FAILING

    def _fill(**kw: Any) -> ClusterFillResult:
        # A pass that lands nothing and reports its dispatch honestly.
        return ClusterFillResult(attempts=1, fns_filled=0,
                                 dispatched_fns=dispatched,
                                 answer_lost_fns=answer_lost,
                                 reverted_fns=reverted)

    _stubs = (cluster_stubs if cluster_stubs is not None
              else ([_KEY] if as_stub else []))
    deps = _driver_deps(
        _cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        events, None, frontier={_KEY: {"t2"}}, fill=_fill, logs=logs,
        stubs=_stubs)
    # The charge site's `key in cur_stubs` arm (first-fill left a stub) vs
    # the refill-attempts arm (already filled). Default is the stub arm.
    deps = deps.__class__(**{
        **{f: getattr(deps, f) for f in deps.__dataclass_fields__},
        "scan_unimplemented_functions": (
            lambda *a, **k: StubInventory.of_units([_KEY] if as_stub else [])),
    })
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path,
        tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None,
        fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def _exhaustion_verdicts(events) -> list[dict]:
    return [d for ev, d in events
            if ev == "phase_c.verdict" and d.get("disposition") == "exhaustion"
            and d.get("unit") == "src/a.rs::f"]


def test_undispatched_stub_is_spared_and_stays_admitted(
        tmp_path: Path, monkeypatch) -> None:
    # Engine reports: nothing was dispatched. The stub must stay admitted —
    # no per-iter exhaustion, no stall-exit conviction, and the spare is loud.
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs, dispatched=())
    assert not _exhaustion_verdicts(events)
    assert any(ev == "phase_c.charge.undispatched_spared" and
               d.get("fns") == ["src/a.rs::f"] for ev, d in events)
    assert any("dispatched by NO route this iter — left admitted" in ln
               for ln in logs)
    # The frontier never empties: the fn stays eligible on every iteration
    # (pre-fix, the charge emptied it and iter 2 read "no actionable
    # frontier" — the A/B/C signature).
    assert not any("no actionable frontier" in ln for ln in logs)
    # Stall exit names the spare rather than convicting it.
    assert any("0 of 1 frontier fn(s) were dispatched" in ln and
               "1 never dispatched, left admitted" in ln for ln in logs)


def test_a_cfg_inactive_stub_is_not_reported_as_a_routing_gap(
        tmp_path: Path, monkeypatch) -> None:
    """The line conflated two populations with opposite meanings, and the
    benign one is 8 of its 10 instances on the retained record.

    `scan_unimplemented_functions` takes no cluster and evaluates no cfg, so a
    stub this cluster's cfg-set excludes is in `cur_stubs` — and being cfg-dead
    is exactly WHY no route dispatched it. Across the retained logs the line
    named `src/nightly.rs::fmt` in cluster `stable` eight times (with the per-fn
    route's own membership report naming it cfg-inactive on the line directly
    above), `src/ensure.rs::__dispatch_ensure` once, and the genuine case — four
    impl-member producers no route claimed — once. A census class keyed on the
    raw line would be ~90% benign, and an escalation keyed on it would spend
    model calls re-prompting a fn this cluster cannot build.

    Here the frontier stub is absent from the CLUSTER-scoped scan, i.e. cfg-dead:
    it must be spared exactly as before (no exhaustion charge) and must NOT be
    reported as undispatched."""
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs, dispatched=(),
         cluster_stubs=[FnLocator("src/other.rs", "live")])
    assert not _exhaustion_verdicts(events), "sparing must be unchanged"
    assert not any("dispatched by NO route this iter" in ln for ln in logs), (
        "a cfg-dead stub is the cfg filter working, not a routing gap")
    assert not any(ev == "phase_c.charge.undispatched_spared" and d.get("fns")
                   for ev, d in events)


def test_negative_control_a_live_stub_is_still_reported(
        tmp_path: Path, monkeypatch) -> None:
    """Without this the case above proves nothing: a subtraction that silenced
    the line unconditionally would pass it too. Same run, the stub present in
    the cluster-scoped scan — the line must still fire and still name it."""
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs, dispatched=(),
         cluster_stubs=[_KEY])
    assert any(ev == "phase_c.charge.undispatched_spared" and
               d.get("fns") == ["src/a.rs::f"] for ev, d in events)


def test_an_empty_cluster_scan_fails_open_rather_than_silencing_the_line(
        tmp_path: Path, monkeypatch) -> None:
    """A filter with input and no output is a contradiction to locate, not a
    verdict that everything is dead — the failure mode
    `ClusterMembershipReport.emptied` names. An extractor-absent build must not
    convert a diagnostic into a guarantee of silence."""
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs, dispatched=(), cluster_stubs=[])
    assert any("dispatched by NO route this iter" in ln for ln in logs)


def test_dispatched_stub_is_still_charged(tmp_path: Path, monkeypatch) -> None:
    # Same run, but the engine reports it DID attempt the fn: the existing
    # charge must be unchanged — attempted and left a stub → exhausted.
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs,
         dispatched=(("src/a.rs", "f"),))
    assert not any(ev == "phase_c.charge.undispatched_spared"
                   for ev, _ in events)
    # Charged at iter 1 → the frontier is empty from iter 2 on.
    assert any("no actionable frontier" in ln for ln in logs)


def test_a_lost_answer_is_not_an_exhaustion_charge(
        tmp_path: Path, monkeypatch) -> None:
    """The dispatch stamp is written before the model call, so a file-first
    reply that came back with nothing to route still reports its units
    dispatched. The FLAT arm subtracts `answer_lost_fns`; this charger
    still keyed on the raw stamp. Run `347397-01e2463b` iter 1 (2026-08-31):
    src/ensure.rs replied with 0 FN tags, `__dispatch_ensure` and `render`
    were graded as tried, and a second empty reply would have hit the
    refill cap (default 2) and locked `render` — a free-fn refill whose
    only planner is file-first — out of the next-iteration retry.

    Here the engine reports the stub dispatched AND lost: it stays
    admitted, writes no exhaustion, and does not join the "NO route" spare
    line (a route opened it; the answer was lost). NEGATIVE CONTROL: the
    identical replay with the loss unreported is
    `test_dispatched_stub_is_still_charged` — frontier empty from iter 2."""
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs,
         dispatched=(("src/a.rs", "f"),),
         answer_lost=(("src/a.rs", "f"),))
    assert not _exhaustion_verdicts(events)
    assert not any(ev == "phase_c.charge.undispatched_spared"
                   for ev, _ in events)
    assert not any("no actionable frontier" in ln for ln in logs)


def test_a_lost_refill_does_not_count_toward_the_cap(
        tmp_path: Path, monkeypatch) -> None:
    """The live instance was a refill, not a stub: `render` was already
    filled. Two lost file-first calls would hit `TDD_REFILL_MAX_ATTEMPTS`
    and `permanently_exhausted` would refuse the next-iteration retry.
    Here the engine reports the filled unit dispatched AND lost, cap 2,
    stall budget 4: it is never cap-refused.

    NEGATIVE CONTROL: the same replay with the loss unreported hits the
    cap (disposition `cap_refusal`)."""
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "2")
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs,
         dispatched=(("src/a.rs", "f"),),
         answer_lost=(("src/a.rs", "f"),),
         as_stub=False, max_stalls="4")
    assert not any(d.get("disposition") == "cap_refusal"
                   for _, d in events)

    control = tmp_path / "ctrl"
    control.mkdir()
    control_events: list[tuple[str, dict]] = []
    control_logs: list[str] = []
    _run(monkeypatch, control, control_events, control_logs,
         dispatched=(("src/a.rs", "f"),),
         as_stub=False, max_stalls="4")
    assert any(d.get("disposition") == "cap_refusal"
               for _, d in control_events), (
        "unreported, the lost refill is charged — the fabricated strike")


def test_a_reverted_refill_does_not_count_toward_the_cap(
        tmp_path: Path, monkeypatch) -> None:
    """A peel restores the snapshot; that is not a judged try of the body
    the model returned. Run 451330-7e8a7ca9 (2026-08-31): src/fmt.rs::debug
    was peeled twice, both tries charged, cap_refusal at TDD_REFILL_MAX_ATTEMPTS
    2. Here the engine reports the filled unit dispatched AND reverted, cap
    2: it is never cap-refused.

    NEGATIVE CONTROL: the same replay with the revert unreported hits the
    cap (disposition cap_refusal)."""
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "2")
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs,
         dispatched=(("src/a.rs", "f"),),
         reverted=(("src/a.rs", "f"),),
         as_stub=False, max_stalls="4")
    assert not any(d.get("disposition") == "cap_refusal"
                   for _, d in events)

    control = tmp_path / "ctrl"
    control.mkdir()
    control_events: list[tuple[str, dict]] = []
    control_logs: list[str] = []
    _run(monkeypatch, control, control_events, control_logs,
         dispatched=(("src/a.rs", "f"),),
         as_stub=False, max_stalls="4")
    assert any(d.get("disposition") == "cap_refusal"
               for _, d in control_events), (
        "unreported, the peeled refill is charged — the fabricated strike")


def test_legacy_engine_without_report_reproduces_the_old_burn(
        tmp_path: Path, monkeypatch) -> None:
    # NEGATIVE CONTROL — dispatched_fns=None (an engine that does not report,
    # e.g. every stub in the older tests): the legacy frontier==dispatched
    # fallback must still charge, reproducing the pre-fix behaviour. This is
    # what proves the sparing keys on the engine's report and not on some
    # unrelated condition.
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path, events, logs, dispatched=None)
    assert not any(ev == "phase_c.charge.undispatched_spared"
                   for ev, _ in events)
    # The pre-fix burn: charged undispatched, frontier empty from iter 2 on.
    assert any("no actionable frontier" in ln for ln in logs)
