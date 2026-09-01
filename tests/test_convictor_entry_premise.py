"""The write-side entry premise for behavioural retrospectives.

On the endgame tree `blocked_on` is empty on essentially every new record —
`BlockedOnStubs.at_conviction` reads STUB callees and one stub remained in
209 fillable units — so every behavioural lesson was retired as
`no_tree_premise` on its first read: 53 such retirements against 9 honours
in the current log, and 0 of 19 `src/error.rs` prompts on 2026-09-01
carried MODEL_NOTES. The stamp records the bodies each convicting test
measurably entered at conviction time (the owner map already holds that
reading), so the lesson stands while the execution it explains stands, and
`convictor_entry_moved` expires it when a later fill redirects the test —
the same shape as `convictors_never_execute`, one measurement later.
"""
from __future__ import annotations

from pipeline_core.domain.coverage_slice import (
    CoverageSlice,
    bind,
    measured_entry_sets,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.retro_gate import (
    CONVICTOR_ENTRY,
    CONVICTOR_ENTRY_MOVED,
    NO_TREE_PREMISE,
    BlockedOnStubs,
    ConvictionEvidence,
    RetroProvenance,
    convictor_entry_moved,
    honoured_guidance_line,
    retired_guidance_line,
    screen_guidance,
    stamp_conviction,
    tree_premise_absent,
)

_A = FnLocator("src/alpha.rs", "build")
_B = FnLocator("src/alpha.rs", "render")
_C = FnLocator("src/beta.rs", "emit")

_LAYOUT = "pub struct Widget { pub label: String }\n"

_GUIDANCE = ("The `render` body must delegate to the stored writer instead "
             "of formatting a fresh one.")


def _record(**over):
    rec = {"category": "WRONG_TYPE_CHOICE", "guidance": _GUIDANCE,
           "error_count": 0, "failing_test_count": 1}
    rec.update(over)
    return rec


def _stamp(rec, tests=frozenset({"t_widget"})):
    return stamp_conviction(
        rec, RetroProvenance.current("stable", _LAYOUT),
        ConvictionEvidence(frozenset(tests)),
        BlockedOnStubs(frozenset()))


def _bind(owners):
    bind(CoverageSlice(owners=owners))


def _unbind():
    bind(None)


def test_stamp_records_the_measured_entry_sets() -> None:
    """A behavioural record written under a bound reading carries the bodies
    its convicting test entered — and that premise is a tree premise, so the
    ninth rule no longer retires the lesson on sight."""
    _bind({"t_widget": frozenset({_A, _B})})
    try:
        rec = _stamp(_record())
    finally:
        _unbind()
    assert rec[CONVICTOR_ENTRY] == {
        "t_widget": ["src/alpha.rs::build", "src/alpha.rs::render"]}
    assert not tree_premise_absent(rec)
    assert "convictor_entry[1]" in honoured_guidance_line(
        "src/alpha.rs::render::Widget", rec, _GUIDANCE)


def test_unbound_stamp_falls_back_to_the_ninth_rule() -> None:
    # Control (the pre-stamp behaviour): with no reading bound the record
    # carries no premise and screen retires it as before.
    rec = _stamp(_record())
    assert CONVICTOR_ENTRY not in rec
    assert tree_premise_absent(rec)
    verdict = screen_guidance(
        rec, _LAYOUT, evidence=ConvictionEvidence(frozenset({"t_widget"})))
    assert verdict.retired_by == NO_TREE_PREMISE


def test_a_compile_taught_record_is_not_stamped() -> None:
    """The premise is about executions; a lesson learned from the compiler
    (`error_count` > 0) is answerable to the compiler and keeps its existing
    lifecycle."""
    _bind({"t_widget": frozenset({_A})})
    try:
        rec = _stamp(_record(error_count=2))
    finally:
        _unbind()
    assert CONVICTOR_ENTRY not in rec


def test_screen_honours_the_premise_while_the_reading_stands() -> None:
    owners = {"t_widget": frozenset({_A, _B})}
    _bind(owners)
    try:
        rec = _stamp(_record())
        verdict = screen_guidance(
            rec, _LAYOUT,
            evidence=ConvictionEvidence(frozenset({"t_widget"})))
    finally:
        _unbind()
    assert verdict.retired_by == ""
    assert verdict.text


def test_screen_retires_when_the_reading_moves() -> None:
    """A fill that redirects the convicting test's execution supersedes the
    lesson: the record's exhaustion counted attempts judged on an execution
    the tree no longer produces."""
    _bind({"t_widget": frozenset({_A, _B})})
    try:
        rec = _stamp(_record())
    finally:
        _unbind()
    _bind({"t_widget": frozenset({_A, _C})})
    try:
        assert convictor_entry_moved(rec)
        verdict = screen_guidance(
            rec, _LAYOUT,
            evidence=ConvictionEvidence(frozenset({"t_widget"})))
    finally:
        _unbind()
    assert verdict.retired_by == CONVICTOR_ENTRY_MOVED
    line = retired_guidance_line("src/alpha.rs::render::Widget", rec, verdict)
    assert CONVICTOR_ENTRY_MOVED in line
    assert "t_widget" in line


def test_an_unmeasured_test_decides_nothing() -> None:
    """Conservative on ignorance, same rule as `convictors_never_enter`: a
    test the current sweep did not profile (absent) or whose binary crashed
    (empty owner set) refutes nothing, and an unbound loop behaves as before
    the stamp existed."""
    _bind({"t_widget": frozenset({_A, _B})})
    try:
        rec = _stamp(_record())
    finally:
        _unbind()
    assert not convictor_entry_moved(rec)                    # unbound
    _bind({"t_other": frozenset({_C})})
    try:
        assert not convictor_entry_moved(rec)                # absent
    finally:
        _unbind()
    _bind({"t_widget": frozenset()})
    try:
        assert not convictor_entry_moved(rec)                # crashed profile
    finally:
        _unbind()


def test_measured_entry_sets_positive_evidence_only() -> None:
    _bind({"t1": frozenset({_A}), "t2": frozenset()})
    try:
        got = measured_entry_sets(["t1", "t2", "t3"])
    finally:
        _unbind()
    assert got == {"t1": ("src/alpha.rs::build",)}
    assert measured_entry_sets(["t1"]) == {}                 # unbound
