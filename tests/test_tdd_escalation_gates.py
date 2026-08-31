"""Futility-escalation gates in the TDD driver.

Two defects measured on the 2026-08-11/12 A/B/C night:

* `display_formats_error` escalated past the still-stubbed accessor
  (`Display for MathError::fmt`) to the already-correct producer
  (`checked_div`); three arms rewrote the producer for zero movement (172-293 s
  each) while the culprit sat unfilled. An accessor that is still a stub was
  never served by any rewrite — it is the target, and escalation is withheld.

* Arm A's hardcrate escalated `display_formats_error` to `checked_div` AFTER
  the exhaustion filter had already retired it, so the added key was silently
  swallowed and the file-first route never carried it — A's stable cluster
  stopped at 46/52 where B/C reached 47/52. An escalation naming an exhausted
  fn is new evidence about it and re-admits it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.domain.models import ClusterFillResult, FnLocator
from tests.test_tdd_ratchet import _driver_deps

_ACC = FnLocator("src/a.rs", "fmt")       # the accessor
_PROD = FnLocator("src/a.rs", "producer")  # the escalation target

_FAILING = ("test t1 ... ok\ntest t2 ... FAILED\n"
            "test result: FAILED. 1 passed; 1 failed; 0 ignored")


def _run(monkeypatch, tmp_path: Path, events, logs, *,
         escalate, cluster_stubs, fn_stubs,
         dispatched=None) -> None:
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")

    def _cargo(*a: Any, **k: Any):
        return 101, _FAILING

    def _fill(**kw: Any) -> ClusterFillResult:
        return ClusterFillResult(attempts=1, fns_filled=0,
                                 dispatched_fns=dispatched)

    deps = _driver_deps(
        _cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        events, None, frontier={_ACC: {"t2"}}, fill=_fill, logs=logs,
        stubs=cluster_stubs)
    deps = deps.__class__(**{
        **{f: getattr(deps, f) for f in deps.__dataclass_fields__},
        "scan_unimplemented_functions": lambda *a, **k: list(fn_stubs),
        "escalate_to_producers": escalate,
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


def test_escalation_withheld_while_the_accessor_is_still_a_stub(
        tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    calls: list[str] = []

    def _escalate(src_dir, tname, units, producers_out=None):
        calls.append(tname)
        return {_PROD: {tname}}

    # The accessor was opened (in dispatched_fns) and left a stub. That is
    # service, so the two-sweep rule fires; withheld then keeps the stub as
    # the target instead of escalating to the producer. An empty
    # dispatched_fns used to mark the unit served anyway — the same lie the
    # FLAT arm told on run 9933-efe68f37 — and this test keyed on it. Empty
    # dispatch now parks as never-served (escalation_deferred), which is
    # also "do not escalate" but is not this gate.
    _run(monkeypatch, tmp_path, events, logs, escalate=_escalate,
         cluster_stubs=[_ACC], fn_stubs=[_ACC], dispatched=(_ACC,))
    assert calls == []  # escalation never invoked
    assert any(ev == "phase_c.attribution.escalation_withheld" and
               d.get("stub_accessors") == ["src/a.rs::fmt"]
               for ev, d in events)
    assert any("NOT escalating" in ln and "the stub is the target" in ln
               for ln in logs)


def test_escalation_defers_when_the_accessor_was_never_opened(
        tmp_path: Path, monkeypatch) -> None:
    # NEGATIVE CONTROL of the withheld pin above: the engine reported an
    # empty dispatched_fns, so _served_at does not include the accessor.
    # The two-sweep identity is not service — that is the parked-under-cap
    # rule, not "the stub is the target".
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    calls: list[str] = []

    def _escalate(src_dir, tname, units, producers_out=None):
        calls.append(tname)
        return {_PROD: {tname}}

    _run(monkeypatch, tmp_path, events, logs, escalate=_escalate,
         cluster_stubs=[_ACC], fn_stubs=[_ACC], dispatched=())
    assert calls == []
    assert any(ev == "phase_c.attribution.escalation_deferred" and
               d.get("unserved") == ["src/a.rs::fmt"]
               for ev, d in events)
    assert not any(ev == "phase_c.attribution.escalation_withheld"
                   for ev, d in events)


def test_escalation_fires_when_no_accessor_is_a_stub(
        tmp_path: Path, monkeypatch) -> None:
    # NEGATIVE CONTROL: accessor filled (not in any stub scan) and its
    # unit-set repeats → the escalation fires exactly as before the gate.
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    calls: list[str] = []

    def _escalate(src_dir, tname, units, producers_out=None):
        calls.append(tname)
        return {_PROD: {tname}}

    _run(monkeypatch, tmp_path, events, logs, escalate=_escalate,
         cluster_stubs=[], fn_stubs=[], dispatched=None)
    assert calls, "escalation should have fired for the repeating unit-set"
    assert any(ev == "phase_c.attribution.escalated" for ev, _ in events)
    assert not any(ev == "phase_c.attribution.escalation_withheld"
                   for ev, _ in events)


def test_escalation_re_admits_an_exhausted_target(
        tmp_path: Path, monkeypatch) -> None:
    # The producer is exhausted by the legacy charge at iter 1 (it is a stub
    # the engine "dispatched" and left unfilled); when the escalation later
    # names it, it must be released rather than silently filtered.
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _escalate(src_dir, tname, units, producers_out=None):
        return {_PROD: {tname}}

    # _PROD enters the frontier from iter 1 via attribution, gets charged
    # exhausted (legacy dispatched=None + still a stub in
    # scan_unimplemented_functions), and the accessor's unit-set repeats.
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")

    def _cargo(*a: Any, **k: Any):
        return 101, _FAILING

    deps = _driver_deps(
        _cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        events, None, frontier={_ACC: {"t2"}, _PROD: {"t2"}},
        fill=lambda **kw: ClusterFillResult(attempts=1), logs=logs)
    deps = deps.__class__(**{
        **{f: getattr(deps, f) for f in deps.__dataclass_fields__},
        "scan_unimplemented_functions": lambda *a, **k: [_PROD],
        "escalate_to_producers": _escalate,
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
    esc = [d for ev, d in events if ev == "phase_c.attribution.escalated"]
    assert esc and any("producer" in x for x in esc[0].get("re_admitted", []))
    assert any("re-admitted on the new evidence" in ln for ln in logs)


def test_escalation_releases_the_stored_verdict_on_its_target(
        tmp_path: Path, monkeypatch) -> None:
    """The store is the second gate. Run `1246060-2339497b` (2026-08-21)
    re-merged `StdErrorforContextError<C,Error>` into the frontier on every
    sweep (`escalation_sustained` ×1599) and the impl route skipped it at
    every dispatch on its disk `fully_exhausted` flag — the process-local
    re-admission above never reached that flag. The escalation's evidence
    must open both, through the capped reset."""
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    released_with: list[tuple[frozenset, str]] = []

    def _escalate(src_dir, tname, units, producers_out=None):
        return {_PROD: {tname}}

    def _release(pd, sd, targets, reason):
        released_with.append((frozenset(targets), reason))
        return ["src/a.rs::*impl*::Producer"]

    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    from pipeline_core.domain.models import BuildCluster
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "1")
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.rs").write_text("orig", encoding="utf-8")

    def _cargo(*a: Any, **k: Any):
        return 101, _FAILING

    deps = _driver_deps(
        _cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        events, None, frontier={_ACC: {"t2"}, _PROD: {"t2"}},
        fill=lambda **kw: ClusterFillResult(attempts=1), logs=logs)
    deps = deps.__class__(**{
        **{f: getattr(deps, f) for f in deps.__dataclass_fields__},
        "scan_unimplemented_functions": lambda *a, **k: [_PROD],
        "escalate_to_producers": _escalate,
        "release_escalated_convictions": _release,
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
    assert released_with, "the escalation never reached the store"
    targets, reason = released_with[0]
    assert targets == frozenset({_PROD})
    assert "escalation of t2" in reason
    esc = [d for ev, d in events if ev == "phase_c.attribution.escalated"]
    assert esc and esc[0].get("disk_released") == ["src/a.rs::*impl*::Producer"]
    assert any("released 1 stored exhaustion verdict(s)" in ln for ln in logs)
