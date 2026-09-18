"""Characterization tests for FixReport.

Pin `as_record()` to the EXACT dicts `generate_fix_loop`'s return sites built,
so swapping the hand-built dicts for the typed VO is provably behaviour-
preserving (the record flows into the loop's `.get()` reads and the prompt
-history narrative).
"""

from __future__ import annotations

from pipeline_core.domain.fix_report import FixKind, FixReport


def test_abort_with_summary_matches_timeout_site() -> None:
    r = FixReport.aborted(reason="check_timeout", summary="check timed out")
    assert r.abort is True
    assert r.as_record() == {
        "abort": True, "reason": "check_timeout", "summary": "check timed out"}


def test_abort_without_summary_matches_fn_not_located_site() -> None:
    r = FixReport.aborted(reason="fn_not_located")
    assert r.as_record() == {"abort": True, "reason": "fn_not_located"}


def test_unchanged_matches_no_fix_sites() -> None:
    # no_fixes_produced / no_body / no_splice all share this shape.
    r = FixReport.unchanged(summary="no_body", error_preview="E0277 ...")
    assert r.kind is FixKind.NOOP
    assert r.as_record() == {
        "summary": "no_body", "files_changed_str": "no files changed",
        "error_preview": "E0277 ..."}


def test_changed_without_error_sig_matches_inner_splice_sites() -> None:
    # _fix_single_fn / _fix_impl_block spliced returns carry no error_sig.
    r = FixReport.changed(summary="fn a.rs::foo", files_changed_str="a.rs::foo",
                          error_preview="errs")
    assert r.kind is FixKind.APPLIED
    assert r.as_record() == {
        "summary": "fn a.rs::foo", "files_changed_str": "a.rs::foo",
        "error_preview": "errs"}


def test_changed_with_error_sig_matches_aggregate_sites() -> None:
    # _do_fix / per_fn / per_impl / tdd / behavior aggregate returns.
    r = FixReport.changed(summary="per_fn changed=2",
                          files_changed_str="a.rs::foo, b.rs::bar",
                          error_preview="errs", error_sig="errs")
    assert r.as_record() == {
        "summary": "per_fn changed=2",
        "files_changed_str": "a.rs::foo, b.rs::bar",
        "error_preview": "errs", "error_sig": "errs"}
