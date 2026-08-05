"""SourceSnapshot capture/restore + retry verdict logic (P3.D), pure."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.per_fn_retry import (
    _prior_effort_notes,
)
from pipeline_core.domain.cluster_plan import merged_view_cluster
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.retry_outcome import (
    BodyVerdict,
    ClusterReading,
    RetryMode,
    RetryVerdict,
    TreeReading,
    body_verdict,
    build_verdict,
    failing_test_verdict,
    failure_metric,
)
from pipeline_core.domain.source_snapshot import SourceSnapshot
from pipeline_core.domain.test_results import rank_repair_candidates


def test_snapshot_restores_edits(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "lib.rs"
    f.write_text("ORIGINAL\n")
    snap = SourceSnapshot.capture(src)
    assert snap
    f.write_text("CORRUPTED\n")
    snap.restore()
    assert f.read_text() == "ORIGINAL\n"


def test_snapshot_absent_root_is_falsy(tmp_path: Path) -> None:
    assert not SourceSnapshot.capture(tmp_path / "nope")


def test_build_verdict() -> None:
    assert build_verdict(initial_errors=3, final_errors=5, has_snapshot=True) is RetryVerdict.REVERT
    assert build_verdict(initial_errors=3, final_errors=5, has_snapshot=False) is RetryVerdict.ESCALATE
    assert build_verdict(initial_errors=3, final_errors=1, has_snapshot=True) is RetryVerdict.RECURSE
    assert build_verdict(initial_errors=3, final_errors=3, has_snapshot=True) is RetryVerdict.ESCALATE


def test_test_verdict_broke_build_reverts() -> None:
    # ran before, doesn't run now → strictly worse → revert even though
    # final_failures (0, no FAILED lines) looks "better".
    v = failing_test_verdict(passed=False, initial_failures=2, final_failures=0,
                     initially_ran=True, finally_ran=False, has_snapshot=True)
    assert v is RetryVerdict.REVERT


def test_test_verdict_keeps_partial_and_passes() -> None:
    assert failing_test_verdict(passed=True, initial_failures=2, final_failures=0,
                        initially_ran=True, finally_ran=True, has_snapshot=True) is RetryVerdict.PASS
    assert failing_test_verdict(passed=False, initial_failures=2, final_failures=1,
                        initially_ran=True, finally_ran=True, has_snapshot=True) is RetryVerdict.KEEP_PARTIAL


def test_retry_mode_values() -> None:
    assert RetryMode.BUILD == "build" and RetryMode.TEST == "test"


# ── the per-body accept gate's decision, and the ran-state metric ─────────────

def _r(**per: tuple[bool, int]) -> TreeReading:
    return TreeReading(per_cluster=tuple(
        ClusterReading(cluster=k, ran=ran, failures=f) for k, (ran, f) in per.items()))


def test_a_body_that_fixes_one_cluster_and_breaks_another_is_refused() -> None:
    """Nets zero on the total and must still be reverted — which is why the
    comparison is per cluster and not on the sum."""
    assert body_verdict(
        before=_r(nightly=(True, 2), stable=(True, 1)),
        after=_r(nightly=(True, 1), stable=(True, 2)),
    ) is BodyVerdict.REVERT_REGRESSED


def test_a_body_that_stops_a_cluster_reporting_is_refused() -> None:
    assert body_verdict(
        before=_r(nightly=(True, 2), stable=(True, 1)),
        after=_r(nightly=(False, 0), stable=(True, 1)),
    ) is BodyVerdict.REVERT_BROKE_BUILD


def test_a_body_that_makes_a_dead_cluster_report_again_is_kept() -> None:
    """Its first real failure count is not a regression from the 0 a
    non-compiling cluster reports — reading it as one would revert exactly the
    body that fixed the build."""
    assert body_verdict(
        before=_r(nightly=(False, 0), stable=(True, 1)),
        after=_r(nightly=(True, 3), stable=(True, 1)),
    ) is BodyVerdict.KEEP


def test_a_neutral_body_is_kept() -> None:
    same = _r(nightly=(True, 2), stable=(True, 1))
    assert body_verdict(before=same, after=same) is BodyVerdict.KEEP


def test_a_cluster_absent_from_the_baseline_cannot_convict() -> None:
    assert body_verdict(before=_r(nightly=(True, 1)),
                        after=_r(nightly=(True, 1), stable=(True, 9))) is BodyVerdict.KEEP


def test_a_tree_with_no_verdicts_scores_worse_than_any_failure_count() -> None:
    """2026-08-05: a 14-body batch broke the build and the loop recorded
    `metric_new: 0, improved: true, metric_best: 0` — the best score possible,
    on a tree that compiled nothing."""
    assert failure_metric(ran=False, failures=0) > failure_metric(ran=True, failures=999)
    assert failure_metric(ran=True, failures=3) == 3


def test_tree_reading_aggregates_ran_and_failures() -> None:
    r = _r(nightly=(True, 2), stable=(True, 1))
    assert r.ran is True and r.failures == 3
    assert _r(nightly=(True, 2), stable=(False, 0)).ran is False
    assert TreeReading().ran is False


# ── which cluster the retry repairs through ──────────────────────────────────

def test_the_repair_cluster_is_chosen_by_evidence_not_by_name() -> None:
    """2026-08-05: `nightly` was adopted over `stable` because "nightly" <
    "stable" in the iteration order. `nightly` fails `ui` (a trybuild
    compile-fail harness no body rewrite can satisfy) plus `test_boxed_str`;
    `stable` fails only `test_boxed_str`."""
    order = rank_repair_candidates(
        [("nightly", frozenset({"ui", "test_boxed_str"})),
         ("stable", frozenset({"test_boxed_str"}))],
        unrepairable=frozenset({"ui"}))
    assert order[0] == "stable"


def test_a_cluster_whose_every_failure_is_unrepairable_goes_last() -> None:
    order = rank_repair_candidates(
        [("nightly", frozenset({"ui"})),
         ("stable", frozenset({"a", "b", "c"}))],
        unrepairable=frozenset({"ui"}))
    assert order == ["stable", "nightly"]


def test_the_choice_is_reproducible_when_the_evidence_ties() -> None:
    order = rank_repair_candidates(
        [("stable", frozenset({"x"})), ("nightly", frozenset({"y"}))])
    assert order == ["nightly", "stable"]


# ── the merged view's cfg identity ───────────────────────────────────────────

def _cluster(name: str, active: list[str], excluded: list[str]) -> BuildCluster:
    return BuildCluster(name=name, env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=excluded, active_cfg=active,
                        prompt_nightly_rule="")


def test_the_merged_view_asserts_only_what_every_cluster_agrees_on() -> None:
    """The post-merge fill passed `cluster=None` under the name "merged", which
    matches no baked cluster — so no cfg-conditioned prompt section could be
    computed for the whole stage. A cfg the clusters DISAGREE on
    (`error_generic_member_access` on this crate) must land in neither set."""
    merged = merged_view_cluster([
        _cluster("stable", [], ["error_generic_member_access", "miri"]),
        _cluster("nightly", ["error_generic_member_access"], ["miri"]),
    ])
    assert merged is not None
    assert merged.name == "merged"
    assert merged.active_cfg == []
    assert merged.excluded_cfg == ["miri"]
    assert "stable" in merged.prompt_nightly_rule
    assert "nightly" in merged.prompt_nightly_rule


def test_no_clusters_means_no_merged_identity() -> None:
    assert merged_view_cluster([]) is None


# ── evidence carried across the checkpoint ───────────────────────────────────

def test_an_exhausted_clusters_effort_reaches_the_loop_that_inherits_it(tmp_path) -> None:
    """`nightly` was checkpointed 2026-08-04 at 100/104 with four tests still
    failing. Clusters seed forward only and a checkpoint is never re-opened, so
    everything it learned while failing stayed behind — and the loop that
    inherited two of those tests started from a bare failing-test name."""
    import json
    skel = tmp_path / ".skeleton"
    skel.mkdir()
    (skel / "_cluster_evidence.json").write_text(json.dumps({
        "nightly": {"failed": ["test_boxed_str", "ui"], "ran": [],
                    "exit": "exhausted",
                    "exhausted_fns": ["error.rs::downcast_ref"]},
        "stable": {"failed": [], "ran": [], "exit": "done",
                   "exhausted_fns": ["error.rs::from"]},
    }))
    notes = _prior_effort_notes(tmp_path)
    assert "error.rs::downcast_ref" in notes
    assert "nightly" in notes["error.rs::downcast_ref"]
    assert "test_boxed_str" in notes["error.rs::downcast_ref"]
    # A cluster that FINISHED spent no budget failing on its fns.
    assert "error.rs::from" not in notes


def test_a_missing_or_corrupt_evidence_store_is_not_an_error(tmp_path) -> None:
    assert _prior_effort_notes(tmp_path) == {}
    (tmp_path / ".skeleton").mkdir()
    (tmp_path / ".skeleton" / "_cluster_evidence.json").write_text("{not json")
    assert _prior_effort_notes(tmp_path) == {}
