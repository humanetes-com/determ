"""Coverage instruction slot: dead body / dead region on the opener, not a section.

Unbound (the walk / virgin-cluster case) every helper returns empty so a
builder that concatenates this next to behaviour_must stays byte-identical.
"""
from __future__ import annotations

from pipeline_core.domain.coverage_slice import (
    DEAD_BODY,
    CountedBody,
    CoverageSlice,
    bind,
    instruction_for,
    instruction_opener,
    instructions_for,
    region_sentence,
    slice_from_counts,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.prompts.context import PerFnSections
from pipeline_core.prompts.fix_prompts import BlockFillPlan, _plan_line
from pipeline_core.prompts.per_fn import build_per_fn_prompt

_CFA = FnLocator("src/error.rs", "construct_from_adhoc")
_DEBUG = FnLocator("src/fmt.rs", "debug")
_REGION = "Lines 19–32 of the body you are editing are never reached."


def _unbind() -> None:
    bind(None)


def test_unbound_instruction_is_empty() -> None:
    _unbind()
    assert instruction_for("src/error.rs", "construct_from_adhoc") == ""
    assert instructions_for("src/error.rs", ["construct_from_adhoc"]) == ""
    assert instruction_opener("src/error.rs", ["construct_from_adhoc"]) == ""


def test_dead_body_sentence_is_the_plan_line() -> None:
    try:
        bind(CoverageSlice(dead=frozenset([_CFA])))
        assert instruction_for(_CFA.file, _CFA.fn_name) == DEAD_BODY
        assert "executed by no test" in DEAD_BODY
        assert instruction_for("src/lib.rs", "format_err") == ""
        opener = instruction_opener(_CFA.file, [_CFA.fn_name])
        assert opener == DEAD_BODY + "\n\n"
    finally:
        _unbind()


def test_dead_region_sentence_attaches_to_a_body_that_ran() -> None:
    try:
        bind(CoverageSlice(dead_regions={_DEBUG: _REGION}))
        assert instruction_for(_DEBUG.file, _DEBUG.fn_name) == _REGION
        assert instruction_for(_CFA.file, _CFA.fn_name) == ""
    finally:
        _unbind()


def test_plan_line_appends_coverage_must_on_the_instruction_slot() -> None:
    """Not a tagged evidence section — the same slot behaviour_must uses."""
    bare = BlockFillPlan("impl Error", (), ("construct_from_adhoc",))
    with_cov = BlockFillPlan(
        "impl Error", (), ("construct_from_adhoc",),
        coverage_must=DEAD_BODY)
    assert DEAD_BODY not in _plan_line(bare)
    line = _plan_line(with_cov)
    assert DEAD_BODY in line
    assert "<COVERAGE>" not in line


def test_per_fn_opener_carries_the_dead_body_sentence_when_bound() -> None:
    empty = PerFnSections(
        macros="", notes="", container="impl Error", types="",
        invariants="", siblings="", crate_idioms="", bound_methods="")
    unbound = build_per_fn_prompt(
        file_text="fn construct_from_adhoc() { unimplemented!() }",
        fn_name="construct_from_adhoc",
        sig_text="fn construct_from_adhoc() -> Self",
        test_blocks=[],
        module_name="anyhow", cluster_name="stable",
        file_rel="src/error.rs", fr_path=None,
        sections=empty)
    try:
        bind(CoverageSlice(dead=frozenset([_CFA])))
        bound = build_per_fn_prompt(
            file_text="fn construct_from_adhoc() { unimplemented!() }",
            fn_name="construct_from_adhoc",
            sig_text="fn construct_from_adhoc() -> Self",
            test_blocks=[],
            module_name="anyhow", cluster_name="stable",
            file_rel="src/error.rs", fr_path=None,
            sections=empty)
    finally:
        _unbind()
    assert DEAD_BODY not in unbound
    assert DEAD_BODY in bound
    sig_at = bound.index("fn construct_from_adhoc() -> Self")
    gotchas_at = bound.index("<RUST_GOTCHAS>")
    assert DEAD_BODY in bound[sig_at:gotchas_at]
    assert "<COVERAGE>" not in bound


_BT = FnSite("src/error.rs", "backtrace", "impl Error")
_FMT = FnSite("src/error.rs", "fmt", "impl Display for Error")
_FMT_DBG = FnSite("src/error.rs", "fmt", "impl Debug for Error")
_FMT_IMPL = FnSite("src/error.rs", "fmt", "impl Debug for ErrorImpl")
_FMT_DISP_IMPL = FnSite("src/error.rs", "fmt", "impl Display for ErrorImpl")
_MSG = FnSite("src/error.rs", "msg", "impl Error")
_DEBUG_SITE = FnSite("src/fmt.rs", "debug", "impl ErrorImpl")


def test_locator_is_dead_only_when_every_same_name_body_is_dead() -> None:
    """The plan's ambiguous-key case at the contract that writes the dead set.

    error.rs::backtrace is live at 389 (Error method) and dead at 918
    (ErrorImpl). Collapsing by file+name without this check sinks the
    earned frontier unit and puts the dead-body sentence on its prompt.
    """
    live = CountedBody(_BT, start=389, count=12, span_lines=8)
    dead_impl = CountedBody(
        FnSite("src/error.rs", "backtrace", "impl ErrorImpl"),
        start=918, count=0, span_lines=4)
    mixed = slice_from_counts([live, dead_impl])
    assert FnLocator(_BT.file, _BT.fn_name) not in mixed.dead
    assert mixed.dead_regions == {}
    both_dead = slice_from_counts([
        CountedBody(_BT, start=389, count=0, span_lines=8),
        dead_impl,
    ])
    assert FnLocator(_BT.file, _BT.fn_name) in both_dead.dead


def test_dead_region_omitted_for_an_ambiguous_locator() -> None:
    """error.rs::fmt names four bodies; a first-wins sentence names a line
    outside the body being edited. Until the site key, omit the sentence."""
    bodies = [
        CountedBody(_FMT, start=667, count=4,
                    zero_lines=(678,), span_lines=10),
        CountedBody(_FMT_DBG, start=690, count=4, span_lines=8),
        CountedBody(_FMT_IMPL, start=710, count=2, span_lines=6),
        CountedBody(_FMT_DISP_IMPL, start=730, count=1, span_lines=6),
    ]
    slice_ = slice_from_counts(bodies)
    assert FnLocator(_FMT.file, _FMT.fn_name) not in slice_.dead
    assert FnLocator(_FMT.file, _FMT.fn_name) not in slice_.dead_regions


def test_cfg_one_liner_below_dead_fraction_has_no_sentence() -> None:
    """msg's None arm of if cfg(feature = std) is one unreachable line of
    twenty. Reading 3 selected by fraction (lowest 20 percent); this sits
    below that and must not steer a unit the ranking wants aimed well."""
    slice_ = slice_from_counts([
        CountedBody(_MSG, start=80, count=20,
                    zero_lines=(95,), span_lines=20),
    ])
    assert FnLocator(_MSG.file, _MSG.fn_name) not in slice_.dead
    assert FnLocator(_MSG.file, _MSG.fn_name) not in slice_.dead_regions


def test_high_dead_fraction_names_contiguous_runs_not_a_spanning_range() -> None:
    """fmt.rs::debug at 72 percent dead is the missing-delegation class.
    Non-contiguous zeros must not render as a min-max that spans reached
    lines."""
    zeros = (19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 32)
    slice_ = slice_from_counts([
        CountedBody(_DEBUG_SITE, start=10, count=5,
                    zero_lines=zeros, span_lines=15),
    ])
    sentence = slice_.dead_regions[_DEBUG]
    assert "19–29" in sentence
    assert "32" in sentence
    assert "19–32" not in sentence
    assert region_sentence((10, 12, 13, 14)) == (
        "Lines 10, 12–14 of the body you are editing are never reached.")
    assert region_sentence((95,)) == (
        "Line 95 of the body you are editing is never reached.")
    assert FnLocator(_DEBUG_SITE.file, _DEBUG_SITE.fn_name) not in slice_.dead

