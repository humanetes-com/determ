"""Which cluster's answers the merge should believe.

Clusters seed FORWARD only and a per-cluster checkpoint is permanent, so a
cluster that plateaued early keeps its bodies forever and the merge — which
gates a divergent item by the cluster it came from — keeps shipping them. On
`anyhow` that meant the merged tree's default build took `stable`'s 2026-07-30
copies and cored on `tests/test_downcast.rs` while both cluster trees were the
best they had ever been.
"""
from __future__ import annotations

from pipeline_core.domain.cluster_evidence import dominant_cluster


def test_strict_subset_of_failures_wins():
    # The measured shape: nightly fails 3 of the tests stable fails and neither
    # of the two stable additionally fails.
    failed = {
        "nightly": {"test_result_with_context_err", "test_boxed_str", "test_root_cause"},
        "stable": {"test_result_with_context_err", "test_boxed_str", "test_root_cause",
                   "test_downcast", "test_large_alignment"},
    }
    assert dominant_cluster(failed) == "nightly"


def test_incomparable_failures_have_no_winner():
    # Each fails something the other passes. Picking one here would be exactly
    # the arbitrary choice this replaces.
    failed = {"a": {"t1", "t2"}, "b": {"t2", "t3"}}
    assert dominant_cluster(failed) is None


def test_identical_failures_have_no_winner():
    failed = {"a": {"t1"}, "b": {"t1"}}
    assert dominant_cluster(failed) is None


def test_a_single_cluster_is_never_dominant():
    assert dominant_cluster({"only": set()}) is None


def test_comparison_is_restricted_to_the_tests_both_clusters_ran():
    # `nightly`'s cfg excludes 20 tests from the build, so `100/104` and
    # `112/121` measure different things and a test one cluster never ran says
    # nothing about the other's answers. Here `a` "passes" t9 only because it
    # never ran it — that must not make it dominant.
    failed = {"a": {"t1"}, "b": {"t1", "t9"}}
    ran = {"a": {"t1"}, "b": {"t1", "t9"}}
    assert dominant_cluster(failed, ran) is None
    # …and with t9 genuinely shared and passing in `a`, it is.
    assert dominant_cluster(failed, {"a": {"t1", "t9"}, "b": {"t1", "t9"}}) == "a"
