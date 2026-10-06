"""ClusterAugmentor — the TASK 0c compiler-cluster augmentation phase,
driven through its role ports with a single in-memory fake adapter. No cargo, no
LLM: the fake scripts task0c_augment_module's coverage returns and records every
sentinel/commit/active-cluster side effect."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.cluster_augment import AugmentContext, ClusterAugmentor
from pipeline_core.domain.file_coverage_floor import FILE_ACCOUNTED, FILE_HELD, FILE_MET, FileFloor, FileFloorReading
from pipeline_core.domain.fn_execution import FnCoverageRecord
from pipeline_core.domain.models import FN_EXECUTION_CRITERION, AugmentVerdict, BuildCluster


def _cluster(name: str, **kw: Any) -> BuildCluster:
    base: dict[str, Any] = {
        "env_extra": {}, "tarpaulin_exclude_files": [], "excluded_cfg": [],
        "active_cfg": [], "prompt_nightly_rule": ""}
    base.update(kw)
    return BuildCluster(name=name, **base)


class FakeAdapter:
    """Implements every augment role structurally; in-memory sentinel store +
    a scripted per-(cluster,module) coverage sequence."""

    def __init__(self, *, augment_script: dict[tuple[str, str], list[tuple[float, int, bool, str]]],
                 pre_check: bool = True, tarpaulin: Path | None = None,
                 behaviour_readings: list[tuple[int, int]] | None = None,
                 behaviour_pass_result: bool = True) -> None:
        self._script = augment_script
        self._pre_check = pre_check
        # Scripted `(pinned, total)` readings, consumed one per behaviour_coverage
        # call; the last one repeats. Every behaviour_pass call is recorded.
        self._behaviour_readings = list(behaviour_readings or [(0, 0)])
        self._behaviour_pass_result = behaviour_pass_result
        self.behaviour_passes: list[tuple[str, str]] = []
        self.behaviour_reads: list[tuple[str, str]] = []
        self.cluster_sentinels: dict[tuple[str, str], AugmentVerdict] = {}
        # Scripted function records, one consumed per augment pass (the last
        # repeats); absent → the store has no record, as for a backend
        # without function counts.
        self.fn_scripts: dict[tuple[str, str], list[FnCoverageRecord]] = {}
        self.marked_functions: dict[tuple[str, str], FnCoverageRecord | None] = {}
        # Scripted per-file floor readings, one consumed per augment pass (the
        # last repeats); absent → no reading, as with the floor off.
        self.floor_scripts: dict[tuple[str, str], list[FileFloor]] = {}
        self.marked_floors: dict[tuple[str, str], FileFloor | None] = {}
        self.module_sentinels: dict[str, float] = {}
        self.active: list[str | None] = []
        self.commits: list[str] = []
        self.logs: list[str] = []
        self.events: list[str] = []
        self._tarpaulin = tarpaulin

    # AugmentTelemetry
    def log(self, msg: str) -> None: self.logs.append(msg)
    def emit(self, event: str, **f: Any) -> None: self.events.append(event)
    def mark_phase(self, *a: Any, **k: Any) -> None: ...
    def set_current(self, *a: Any, **k: Any) -> None: ...

    # ClusterScope
    def set_active(self, cluster: BuildCluster | None) -> None:
        self.active.append(cluster.name if cluster else None)

    # AugmentStore
    def is_cluster_augmented(self, pd: Path, c: str, m: str) -> bool:
        return (c, m) in self.cluster_sentinels

    def cluster_augmented_verdict(self, pd: Path, c: str, m: str) -> AugmentVerdict | None:
        return self.cluster_sentinels.get((c, m))

    def mark_cluster_augmented(self, pd: Path, c: str, m: str, cov: float, *,
                               reason: str = "", code_id: str = "",
                               functions: FnCoverageRecord | None = None,
                               file_floor: FileFloor | None = None) -> None:
        self.cluster_sentinels[(c, m)] = AugmentVerdict(
            cov, reason, code_id, criterion=FN_EXECUTION_CRITERION,
            fn_measured=functions is not None and functions.measured,
            file_floor=file_floor.floor if file_floor is not None else 0.0)
        self.marked_functions[(c, m)] = functions
        self.marked_floors[(c, m)] = file_floor

    def cluster_file_floor(self, pd: Path, c: str, m: str) -> FileFloor | None:
        seq = self.floor_scripts.get((c, m))
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def module_file_floors(self, pd: Path, m: str) -> dict[str, FileFloor]:
        return {c: f for (c, mm), f in self.marked_floors.items() if mm == m and f is not None}

    def cluster_fn_record(self, pd: Path, c: str, m: str) -> FnCoverageRecord | None:
        seq = self.fn_scripts.get((c, m))
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def cluster_augmented_value(self, pd: Path, c: str, m: str) -> float:
        v = self.cluster_sentinels.get((c, m))
        return v.coverage if v else 0.0

    def is_module_augmented(self, pd: Path, m: str) -> bool:
        return m in self.module_sentinels

    def mark_module_augmented(self, pd: Path, m: str, cov: float) -> None:
        self.module_sentinels[m] = cov

    def clear_module_sentinel(self, pd: Path, m: str) -> None:
        self.module_sentinels.pop(m, None)

    # AugmentEngine
    def estimate_ceiling(self, pd: Path, cluster: BuildCluster) -> tuple[float, int, int]:
        return (100.0, 0, 0)

    def pre_check(self, pd: Path, m: str, cluster: BuildCluster) -> bool:
        return self._pre_check

    def collect_test_rels(self, pd: Path) -> set[str]:
        return set()

    def augment_module(self, pd: Path, m: str, md: Path, ct: str, *,
                       existing_test_files: set[str], project_context: str,
                       coverage_strategy: str) -> tuple[float, int, bool, str]:
        seq = self._script[(self._cur_cluster, m)]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def behaviour_coverage(self, pd: Path, cluster: BuildCluster, m: str, md: Path) -> tuple[int, int]:
        self.behaviour_reads.append((cluster.name, m))
        seq = self._behaviour_readings
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def behaviour_pass(self, pd: Path, cluster: BuildCluster, m: str, md: Path, *,
                       project_context: str, coverage_strategy: str) -> bool:
        self.behaviour_passes.append((cluster.name, m))
        return self._behaviour_pass_result

    # CommitGate
    def commit_and_mark(self, pd: Path, key: str, status: str, *,
                        commit_message: str, detail: str = "",
                        source_hash: str | None = None) -> None:
        self.commits.append(commit_message)

    # test helper: the engine needs to know which cluster augment_module is for;
    # the use-case sets it via set_active, so mirror that for the script lookup.
    @property
    def _cur_cluster(self) -> str:
        return next(c for c in reversed(self.active) if c is not None)


class _TruthyPath:
    """A tarpaulin_bin stub whose .exists() is True (no real fs)."""
    def exists(self) -> bool: return True
    def __str__(self) -> str: return "/fake/cargo-tarpaulin"


def _ctx(fake: FakeAdapter, clusters: list[BuildCluster], modules: list[str], *,
         module_coverage: dict[str, float] | None = None,
         behaviour_target_pct: float = 0.0,
         file_floor_pct: float = 0.0,
         target_pct: float = 100.0,
         pass_stall_limit: int = 10,
         src_hash: str = "crate-hash",
         code_id: str = "abc") -> AugmentContext:
    return AugmentContext(
        project_dir=Path("/proj"),
        clusters=clusters,
        modules=modules,
        module_coverage=module_coverage or dict.fromkeys(modules, 0.0),
        module_dirs={m: Path("/proj") for m in modules},
        module_types=dict.fromkeys(modules, "lib"),
        src_hash=src_hash,
        code_id=code_id,
        project_context="",
        coverage_strategy="",
        target_pct=target_pct,
        tarpaulin_bin=_TruthyPath(),  # type: ignore[arg-type]
        behaviour_target_pct=behaviour_target_pct,
        file_floor_pct=file_floor_pct,
        pass_stall_limit=pass_stall_limit,
    )


def _augmentor(fake: FakeAdapter) -> ClusterAugmentor:
    return ClusterAugmentor(telemetry=fake, scope=fake, store=fake,
                            engine=fake, gate=fake)


def test_single_cluster_module_reaches_target_marks_both_sentinels() -> None:
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(100.0, 10, False, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    # last cluster → both cluster + main sentinel written at the target.
    v = fake.cluster_sentinels[("stable", "anyhow")]
    assert v.coverage == 100.0 and v.reason == "done" and v.code_id == "abc"
    assert fake.module_sentinels["anyhow"] == 100.0
    assert fake.commits == ["pipeline: task0c/stable — anyhow:100.0%"]
    # active cluster is set then cleared on exit.
    assert fake.active == ["stable", None]


def test_empty_clusters_is_noop_but_clears_active() -> None:
    fake = FakeAdapter(augment_script={})
    _augmentor(fake).run(_ctx(fake, [], ["anyhow"]))
    assert fake.cluster_sentinels == {}
    assert fake.module_sentinels == {}
    assert fake.commits == []
    assert fake.active == [None]  # set_active(None) still runs after the empty loop


def test_retry_then_done_clears_then_marks() -> None:
    # First pass below target → clear_module_sentinel + retry; second pass hits target.
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(80.0, 8, False, ""), (100.0, 10, False, "")]})
    fake.module_sentinels["anyhow"] = 80.0  # pretend a stale sentinel exists
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.module_sentinels["anyhow"] == 100.0  # final mark wins
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == 100.0


def test_blocked_reason_accepts_without_target() -> None:
    # Below target but task0c reports a hard block → accepted, not retried forever.
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(62.0, 6, False, "protected test broken")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == 62.0
    assert fake.module_sentinels["anyhow"] == 62.0


def test_ceiling_after_confirmed_passes_accepts() -> None:
    # Two consecutive at_ceiling reports below target → 'ceiling' status accepted.
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(90.0, 9, True, ""), (90.0, 9, True, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == 90.0
    assert fake.module_sentinels["anyhow"] == 90.0


def test_pre_check_failure_zeros_all_modules() -> None:
    fake = FakeAdapter(augment_script={}, pre_check=False)
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["a", "b"]))
    assert {k: v.coverage for k, v in fake.cluster_sentinels.items()} == {
        ("nightly", "a"): 0.0, ("nightly", "b"): 0.0}
    assert fake.module_sentinels == {"a": 0.0, "b": 0.0}  # last cluster → main sentinels too


def test_non_last_cluster_skips_main_sentinel() -> None:
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(100.0, 10, False, "")],
        ("nightly", "anyhow"): [(100.0, 10, False, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable"), _cluster("nightly")], ["anyhow"]))
    # main sentinel only written by the LAST cluster (nightly).
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == 100.0
    assert fake.cluster_sentinels[("nightly", "anyhow")].coverage == 100.0
    assert fake.module_sentinels["anyhow"] == 100.0
    assert fake.active == ["stable", "nightly", None]


def test_resume_recovers_missing_main_sentinel_on_last_cluster() -> None:
    fake = FakeAdapter(augment_script={})
    # cluster already done from a prior run, but the crash left no main sentinel.
    fake.cluster_sentinels[("stable", "anyhow")] = AugmentVerdict(
        95.0, "done", "src-hash", criterion=FN_EXECUTION_CRITERION)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.module_sentinels["anyhow"] == 95.0  # recovered from the cluster value


# --- behaviour rounds after the coverage loop ---------------------------------
#
# Line coverage is the loop's only stop; a module at target can still have
# pinned none of the harvested observables. With a behaviour target the
# augmentor reads pinned/total per module and runs bounded passes until the
# share is reached, a pass admits nothing, or three rounds are spent.

_DONE = {("stable", "anyhow"): [(100.0, 10, False, "")]}


def test_behaviour_target_zero_runs_no_behaviour_round() -> None:
    fake = FakeAdapter(augment_script=dict(_DONE), behaviour_readings=[(0, 4)])
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.behaviour_reads == [] and fake.behaviour_passes == []
    assert "task0c.behaviour_coverage" not in fake.events
    assert fake.commits == ["pipeline: task0c/stable — anyhow:100.0%"]


def test_behaviour_rounds_stop_once_the_target_is_reached() -> None:
    """Readings 1/4, 1/4, 3/4 against a 50 % target: two passes, then the
    75 % reading stops the loop before a third."""
    fake = FakeAdapter(augment_script=dict(_DONE), behaviour_readings=[(1, 4), (1, 4), (3, 4)])
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], behaviour_target_pct=50.0))
    assert fake.behaviour_passes == [("stable", "anyhow"), ("stable", "anyhow")]
    assert fake.events.count("task0c.behaviour_coverage") == 3
    assert any("behaviour coverage 3/4 (75%) target 50%" in m for m in fake.logs)
    # the rounds run BEFORE the cluster commit, so the pinning blocks ship with it
    assert fake.commits == ["pipeline: task0c/stable — anyhow:100.0%"]


def test_behaviour_rounds_are_bounded_at_three_passes() -> None:
    fake = FakeAdapter(augment_script=dict(_DONE), behaviour_readings=[(1, 4)])
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], behaviour_target_pct=50.0))
    assert len(fake.behaviour_passes) == ClusterAugmentor.BEHAVIOUR_ROUNDS_MAX == 3
    assert any("3 behaviour round(s) spent" in m for m in fake.logs)


def test_behaviour_rounds_stop_when_a_pass_admits_nothing() -> None:
    fake = FakeAdapter(augment_script=dict(_DONE), behaviour_readings=[(1, 4)],
                       behaviour_pass_result=False)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], behaviour_target_pct=50.0))
    assert len(fake.behaviour_passes) == 1
    assert any("admitted no block" in m for m in fake.logs)


def test_behaviour_rounds_skip_a_suite_with_nothing_pinnable_and_a_dead_cluster() -> None:
    fake = FakeAdapter(augment_script=dict(_DONE), behaviour_readings=[(0, 0)])
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], behaviour_target_pct=50.0))
    assert fake.behaviour_passes == []
    dead = FakeAdapter(augment_script={}, pre_check=False, behaviour_readings=[(0, 4)])
    _augmentor(dead).run(_ctx(dead, [_cluster("nightly")], ["a"], behaviour_target_pct=50.0))
    assert dead.behaviour_reads == [] and dead.behaviour_passes == []


# --- a give-up verdict is a claim about the pipeline, not about the tree ------
#
# Run 2881435-34981ab0 (2026-08-27, code=3264439950bf) sealed anyhow's `stable`
# cluster at 83.9% on a ceiling its own post-filter counts contradicted (119
# actionable uncovered lines across 8 files). 1566274-2e1d805c (code=4a6d8d3f3e95)
# then skipped it: the sentinel was a bare number, and the only remover is the
# full --clean workspace wipe. What is asserted here is the plan's sentence — a
# give-up from a pipeline that is no longer on disk does not skip the work —
# keyed on `code_identity`, not the crate source_hash. The pre-fix sentinel
# shape is the negative control.

_LIVE_CEILING_COV = 83.9   # tmp/anyhow/.cluster_augmented/stable__anyhow, 2026-08-27
_LIVE_CODE_ID = "3264439950bf"     # 2881435-34981ab0
_LATER_CODE_ID = "4a6d8d3f3e95"    # 1566274-2e1d805c


def test_a_give_up_verdict_from_other_code_re_augments_instead_of_skipping() -> None:
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(97.8, 780, False, "")]})
    fake.cluster_sentinels[("stable", "anyhow")] = AugmentVerdict(
        _LIVE_CEILING_COV, "ceiling", _LIVE_CODE_ID)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"],
                             code_id=_LATER_CODE_ID))
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == 97.8
    assert fake.cluster_sentinels[("stable", "anyhow")].code_id == _LATER_CODE_ID
    assert any("re-augmenting instead of inheriting it" in m for m in fake.logs)
    assert "cluster.sentinel.reopened" in fake.events


def test_a_give_up_does_not_bind_when_only_the_crate_hash_matches() -> None:
    # The identity the first draft recorded was the crate source_hash. A
    # pipeline-only extractor fix leaves that hash identical, so a give-up
    # keyed on it cannot reopen the cluster. Same crate, new pipeline: re-augment.
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(97.8, 780, False, "")]})
    fake.cluster_sentinels[("stable", "anyhow")] = AugmentVerdict(
        _LIVE_CEILING_COV, "ceiling", _LIVE_CODE_ID)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"],
                             src_hash="unchanged-crate", code_id=_LATER_CODE_ID))
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == 97.8
    assert "cluster.sentinel.reopened" in fake.events


def test_negative_control_the_same_verdict_under_its_own_code_still_skips() -> None:
    # The pre-fix rule was `path.exists()`; the post-fix rule is pipeline
    # provenance, so a verdict recorded under the CURRENT code_id must still
    # skip — otherwise every resumed cluster would re-augment forever. A
    # different crate hash must not reopen it.
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(97.8, 780, False, "")]})
    fake.cluster_sentinels[("stable", "anyhow")] = AugmentVerdict(
        _LIVE_CEILING_COV, "ceiling", _LIVE_CODE_ID)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"],
                             src_hash="a-different-crate", code_id=_LIVE_CODE_ID))
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == _LIVE_CEILING_COV
    assert any("already done — skipping" in m for m in fake.logs)
    assert "cluster.sentinel.reopened" not in fake.events


def test_an_unattributed_legacy_sentinel_does_not_bind() -> None:
    # The shape actually on disk on 2026-08-27: a bare number, no reason, no
    # code_id. Unattributable, so it is re-measured rather than inherited.
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(97.8, 780, False, "")]})
    fake.cluster_sentinels[("stable", "anyhow")] = AugmentVerdict(_LIVE_CEILING_COV)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.cluster_sentinels[("stable", "anyhow")].coverage == 97.8


def test_a_done_verdict_binds_across_a_code_change() -> None:
    # A reached target is a claim about the TREE: the tests are on disk and a
    # pipeline edit does not un-write them. Re-augmenting here would re-measure
    # every finished cluster on every commit.
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(100.0, 10, False, "")]})
    fake.cluster_sentinels[("stable", "anyhow")] = AugmentVerdict(
        100.0, "done", _LIVE_CODE_ID, criterion=FN_EXECUTION_CRITERION)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"],
                             code_id=_LATER_CODE_ID))
    assert any("already done — skipping" in m for m in fake.logs)
    assert "cluster.sentinel.reopened" not in fake.events


def test_a_line_coverage_done_reopens_under_the_function_criterion() -> None:
    # The sentinel run 330242-0be2c3e1 left on `nightly`: done at 95.2 %, no
    # criterion. It must re-augment, not skip.
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [(95.2, 793, False, "")]})
    fake.cluster_sentinels[("nightly", "anyhow")] = AugmentVerdict(95.2, "done", "4b293605efff")
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0))
    assert "cluster.sentinel.reopened" in fake.events
    assert not any("already done — skipping" in m for m in fake.logs)
    assert fake.cluster_sentinels[("nightly", "anyhow")].criterion == FN_EXECUTION_CRITERION


def _pending_record(n: int) -> FnCoverageRecord:
    from pipeline_core.domain.fn_execution import FnTarget, PendingFn
    from pipeline_core.domain.models import FnSite
    return FnCoverageRecord(
        measured=True, candidates=10,
        pending=tuple(PendingFn(FnTarget(FnSite("src/w.rs", f"f{i}", ""), 1, 3, True))
                      for i in range(n)))


def test_line_target_met_with_unexecuted_functions_is_not_done() -> None:
    # Pass 1 meets the line target with two functions still inside their
    # attempt budget → retry; pass 2 has them accounted → done. Coverage does
    # not move between the passes, and the stall clock must not charge that.
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [
        (95.2, 793, False, ""), (95.2, 793, False, "")]})
    fake.fn_scripts[("nightly", "anyhow")] = [_pending_record(2), _pending_record(0)]
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0))
    assert any("meets 90% but 2 function(s) no test executes" in m for m in fake.logs)
    v = fake.cluster_sentinels[("nightly", "anyhow")]
    assert v.reason == "done"
    assert fake.events.count("task.done") == 2


def test_negative_control_the_line_only_done_rule_stops_on_pass_one(monkeypatch: Any) -> None:
    # Same scripted inputs, pre-fix `status`: the line target alone decides, so
    # pass 1 — two functions no test executes — is recorded done and pass 2
    # never runs.
    from pipeline_core.domain.models import ModuleAugmentState

    def _line_only(self: ModuleAugmentState, crate_type: str) -> str:
        if self.last_pct >= self.target_pct or crate_type == "proc-macro":
            return "done"
        if self.ceiling_passes >= self.CEILING_CONFIRM:
            return "ceiling"
        return "hard_block" if self.stall_passes >= self.stall_limit else "retry"

    monkeypatch.setattr(ModuleAugmentState, "status", _line_only)
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [
        (95.2, 793, False, ""), (95.2, 793, False, "")]})
    fake.fn_scripts[("nightly", "anyhow")] = [_pending_record(2), _pending_record(0)]
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0))
    assert fake.cluster_sentinels[("nightly", "anyhow")].reason == "done"
    assert fake.events.count("task.done") == 1                # pass 2 never ran
    assert not any("function(s) no test executes" in m for m in fake.logs)


def test_reaching_the_target_below_a_hundred_records_done() -> None:
    # Live nightly 1566274-2e1d805c: 95.2% against COVERAGE_AUGMENTATION_TARGET=90.
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [(95.2, 793, False, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0))
    v = fake.cluster_sentinels[("nightly", "anyhow")]
    assert v.coverage == 95.2 and v.reason == "done" and v.code_id == "abc"


def test_an_unreachable_target_hard_blocks_after_the_configured_stalled_passes() -> None:
    # A module stuck at 97.0% against a 100 target: pass 1 sets the best,
    # passes 2 and 3 add no line. At pass_stall_limit=2 the third pass is the
    # last one, and the module is recorded hard_block rather than re-entered.
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(97.0, 97, False, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], pass_stall_limit=2))
    assert fake.events.count("task.done") == 3
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "hard_block"
    assert any("stall 2/2" in m for m in fake.logs)


def test_one_new_covered_line_restarts_the_stalled_pass_count() -> None:
    # 97 → 97 (stall 1) → 98 (new best, stall 0) → 98 → 98: five passes at
    # pass_stall_limit=2, not three.
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [
        (97.0, 97, False, ""), (97.0, 97, False, ""), (98.0, 98, False, ""),
        (98.0, 98, False, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], pass_stall_limit=2))
    assert fake.events.count("task.done") == 5
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "hard_block"


def test_the_terminating_status_is_what_gets_recorded() -> None:
    # ceiling / hard_block / done must be distinguishable on disk — the whole
    # point of the field is that only 'done' is sticky.
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(90.0, 9, True, ""), (90.0, 9, True, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "ceiling"
    assert fake.cluster_sentinels[("stable", "anyhow")].code_id == "abc"

    fake = FakeAdapter(augment_script={}, pre_check=False)
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["a"]))
    assert fake.cluster_sentinels[("nightly", "a")].reason == "precheck_failed"


# --- the per-file floor -------------------------------------------------

def _floor(verdict: str) -> FileFloor:
    return FileFloor(90.0, (
        FileFloorReading("src/w.rs", 17, 20, 17, 20, verdict),
        FileFloorReading("src/e.rs", 380, 380, 380, 380, FILE_MET)))


def test_a_held_file_keeps_a_met_module_open_until_it_is_accounted() -> None:
    # Pass 1 meets the aggregate with src/w.rs held under the floor → retry;
    # pass 2 reads it accounted → done, and the sentinel names it below.
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [
        (97.5, 805, False, ""), (97.5, 805, False, "")]})
    fake.floor_scripts[("nightly", "anyhow")] = [_floor(FILE_HELD), _floor(FILE_ACCOUNTED)]
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0,
                              file_floor_pct=90.0))
    assert any("meets 90% but 1 file(s) are below the 90% floor" in m for m in fake.logs)
    assert fake.events.count("task.done") == 2
    v = fake.cluster_sentinels[("nightly", "anyhow")]
    assert v.reason == "done" and v.file_floor == 90.0
    assert any("1 file(s) below the 90% floor on reachable lines (0 held)" in m
               for m in fake.logs)


def test_negative_control_the_floor_off_reads_no_floor_and_stops_on_pass_one() -> None:
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [
        (97.5, 805, False, ""), (97.5, 805, False, "")]})
    fake.floor_scripts[("nightly", "anyhow")] = [_floor(FILE_HELD), _floor(FILE_ACCOUNTED)]
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0))
    assert fake.events.count("task.done") == 1
    assert fake.cluster_sentinels[("nightly", "anyhow")].file_floor == 0.0


def test_a_done_written_without_the_floor_reopens_under_it() -> None:
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [(97.5, 805, False, "")]})
    fake.cluster_sentinels[("nightly", "anyhow")] = AugmentVerdict(
        97.5, "done", "abc", criterion=FN_EXECUTION_CRITERION, fn_measured=True)
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0,
                              file_floor_pct=90.0))
    assert "cluster.sentinel.reopened" in fake.events
    assert any("per-file floor of 0%, now 90%" in m for m in fake.logs)
    # Control: the same sentinel under the floor off still binds.
    fake = FakeAdapter(augment_script={("nightly", "anyhow"): [(97.5, 805, False, "")]})
    fake.cluster_sentinels[("nightly", "anyhow")] = AugmentVerdict(
        97.5, "done", "abc", criterion=FN_EXECUTION_CRITERION, fn_measured=True)
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["anyhow"], target_pct=90.0))
    assert any("already done — skipping" in m for m in fake.logs)
