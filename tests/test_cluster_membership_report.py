"""Pins for the cluster cfg-filter outcome value object.

Both cluster scan routes render this line and the health detector reads it back,
so the format is a contract between three components — round-tripping it here is
what keeps them from drifting apart.
"""
from __future__ import annotations

from pipeline_core.domain.cluster_membership_report import ClusterMembershipReport


def test_kept_is_the_denominator_minus_the_skips():
    r = ClusterMembershipReport("stable", 30, ("a::f", "b::g"))
    assert r.kept == 28
    assert not r.emptied


def test_emptied_only_when_the_whole_input_was_dropped():
    assert ClusterMembershipReport("stable", 28,
                                   tuple(f"f{i}" for i in range(28))).emptied
    # nothing examined is not the same failure — no input, no contradiction
    assert not ClusterMembershipReport("stable", 0, ()).emptied


def test_round_trips_through_its_own_log_line():
    r = ClusterMembershipReport("stable", 27,
                                ("src/wrapper.rs::provide", "src/error.rs::provide"))
    back = ClusterMembershipReport.parse(r.log_line())
    assert back is not None
    assert back.cluster_name == "stable" and back.total == 27
    # rendering sorts for determinism; the identities survive either way
    assert set(back.skipped) == set(r.skipped)


def test_names_two_same_named_fns_from_different_files_separately():
    r = ClusterMembershipReport("stable", 9,
                                ("src/error.rs::provide", "src/nightly.rs::provide"))
    back = ClusterMembershipReport.parse(r.log_line())
    assert back is not None and len(back.skipped) == 2


def test_legacy_bare_count_form_is_rejected():
    """Inventing a denominator for a line that never carried one would restore
    the ambiguity this type removes."""
    assert ClusterMembershipReport.parse(
        "  [phase-c/per-fn] cluster stable: skipped 8 stub(s) not active in "
        "this cluster's cfg-set.") is None


def test_unrelated_lines_parse_to_none():
    assert ClusterMembershipReport.parse("") is None
    assert ClusterMembershipReport.parse(
        "  [phase-c/per-fn] 1/3 filled src/error.rs::object_drop") is None


def test_truncated_name_list_is_rejected_not_half_read():
    """A count that disagrees with the identities means the line was cut; a
    partial read would under-report exactly the evidence this exists to carry."""
    line = ClusterMembershipReport("stable", 9, ("a::f", "b::g", "c::h")).log_line()
    assert ClusterMembershipReport.parse(line.rsplit(",", 1)[0]) is None
