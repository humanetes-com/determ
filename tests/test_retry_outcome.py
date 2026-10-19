"""The per-body verdict over every cluster, pure."""
from __future__ import annotations

from pipeline_core.domain.cluster_plan import merged_view_cluster
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.retry_outcome import (
    BodyVerdict,
    ClusterReading,
    TreeReading,
    body_verdict,
)


def _r(**per: tuple) -> TreeReading:
    """`cluster=(ran, failures)` or `cluster=(ran, failures, diag_count)`."""
    return TreeReading(per_cluster=tuple(
        ClusterReading(cluster=k, ran=t[0], failures=t[1],
                       diag_count=t[2] if len(t) > 2 else 0)
        for k, t in per.items()))


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


def test_a_broken_cluster_gaining_compile_errors_is_refused() -> None:
    """Skipping a baseline-not-ran cluster entirely meant a broken cluster
    could not get worse: for 4h15m on 2026-08-10 (run `2514529-5e77dafb`)
    every accepted splice was gated on "stable didn't regress, nightly still
    says NO VERDICTS" — nightly's 2 E0605s free to become N unseen. Two broken
    readings compare on their distinct compile-diagnostic count."""
    assert body_verdict(
        before=_r(nightly=(False, 0, 2), stable=(True, 17)),
        after=_r(nightly=(False, 0, 3), stable=(True, 17)),
    ) is BodyVerdict.REVERT_REGRESSED


def test_a_broken_cluster_with_the_same_errors_stays_neutral() -> None:
    """2 → 2: the splice did not touch that cluster's break — refusing on it
    would deadlock every stable-side repair while nightly stays broken."""
    assert body_verdict(
        before=_r(nightly=(False, 0, 2), stable=(True, 17)),
        after=_r(nightly=(False, 0, 2), stable=(True, 16)),
    ) is BodyVerdict.KEEP


def test_the_broken_to_running_transition_still_keeps() -> None:
    """The deliberate skip survives the diag-count comparison: a body that
    gets a broken cluster compiling reports its first real failures, and
    reading those as a regression would revert exactly the body that fixed
    the build."""
    assert body_verdict(
        before=_r(nightly=(False, 0, 2), stable=(True, 1)),
        after=_r(nightly=(True, 5), stable=(True, 1)),
    ) is BodyVerdict.KEEP


def test_a_cluster_absent_from_the_baseline_cannot_convict() -> None:
    assert body_verdict(before=_r(nightly=(True, 1)),
                        after=_r(nightly=(True, 1), stable=(True, 9))) is BodyVerdict.KEEP


def test_tree_reading_aggregates_ran_and_failures() -> None:
    r = _r(nightly=(True, 2), stable=(True, 1))
    assert r.ran is True and r.failures == 3
    assert _r(nightly=(True, 2), stable=(False, 0)).ran is False
    assert TreeReading().ran is False


# ── which cluster the retry repairs through ──────────────────────────────────

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

