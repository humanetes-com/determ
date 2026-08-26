"""ClusterAugmentor (R2.2) — the TASK 0c compiler-cluster augmentation phase,
driven through its role ports with a single in-memory fake adapter. No cargo, no
LLM: the fake scripts task0c_augment_module's coverage returns and records every
sentinel/commit/active-cluster side effect."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.cluster_augment import AugmentContext, ClusterAugmentor
from pipeline_core.domain.models import BuildCluster


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
        self.cluster_sentinels: dict[tuple[str, str], float] = {}
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

    def mark_cluster_augmented(self, pd: Path, c: str, m: str, cov: float) -> None:
        self.cluster_sentinels[(c, m)] = cov

    def cluster_augmented_value(self, pd: Path, c: str, m: str) -> float:
        return self.cluster_sentinels.get((c, m), 0.0)

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
         behaviour_target_pct: float = 0.0) -> AugmentContext:
    return AugmentContext(
        project_dir=Path("/proj"),
        clusters=clusters,
        modules=modules,
        module_coverage=module_coverage or dict.fromkeys(modules, 0.0),
        module_dirs={m: Path("/proj") for m in modules},
        module_types=dict.fromkeys(modules, "lib"),
        src_hash="abc",
        project_context="",
        coverage_strategy="",
        target_pct=100.0,
        tarpaulin_bin=_TruthyPath(),  # type: ignore[arg-type]
        behaviour_target_pct=behaviour_target_pct,
    )


def _augmentor(fake: FakeAdapter) -> ClusterAugmentor:
    return ClusterAugmentor(telemetry=fake, scope=fake, store=fake,
                            engine=fake, gate=fake)


def test_single_cluster_module_reaches_target_marks_both_sentinels() -> None:
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(100.0, 10, False, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    # last cluster → both cluster + main sentinel written at the target.
    assert fake.cluster_sentinels[("stable", "anyhow")] == 100.0
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
    assert fake.cluster_sentinels[("stable", "anyhow")] == 100.0


def test_blocked_reason_accepts_without_target() -> None:
    # Below target but task0c reports a hard block → accepted, not retried forever.
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(62.0, 6, False, "protected test broken")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.cluster_sentinels[("stable", "anyhow")] == 62.0
    assert fake.module_sentinels["anyhow"] == 62.0


def test_ceiling_after_confirmed_passes_accepts() -> None:
    # Two consecutive at_ceiling reports below target → 'ceiling' status accepted.
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(90.0, 9, True, ""), (90.0, 9, True, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"]))
    assert fake.cluster_sentinels[("stable", "anyhow")] == 90.0
    assert fake.module_sentinels["anyhow"] == 90.0


def test_pre_check_failure_zeros_all_modules() -> None:
    fake = FakeAdapter(augment_script={}, pre_check=False)
    _augmentor(fake).run(_ctx(fake, [_cluster("nightly")], ["a", "b"]))
    assert fake.cluster_sentinels == {("nightly", "a"): 0.0, ("nightly", "b"): 0.0}
    assert fake.module_sentinels == {"a": 0.0, "b": 0.0}  # last cluster → main sentinels too


def test_non_last_cluster_skips_main_sentinel() -> None:
    fake = FakeAdapter(augment_script={
        ("stable", "anyhow"): [(100.0, 10, False, "")],
        ("nightly", "anyhow"): [(100.0, 10, False, "")]})
    _augmentor(fake).run(_ctx(fake, [_cluster("stable"), _cluster("nightly")], ["anyhow"]))
    # main sentinel only written by the LAST cluster (nightly).
    assert fake.cluster_sentinels[("stable", "anyhow")] == 100.0
    assert fake.cluster_sentinels[("nightly", "anyhow")] == 100.0
    assert fake.module_sentinels["anyhow"] == 100.0
    assert fake.active == ["stable", "nightly", None]


def test_resume_recovers_missing_main_sentinel_on_last_cluster() -> None:
    fake = FakeAdapter(augment_script={})
    # cluster already done from a prior run, but the crash left no main sentinel.
    fake.cluster_sentinels[("stable", "anyhow")] = 95.0
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
