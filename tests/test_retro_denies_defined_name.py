"""A stored lesson that calls a defined name nonexistent is not injected.

The guidance is the retrospective of run `486447-5207101c` (2026-10-07),
per-fn `src/error.rs::construct`, response `386b3201…`: the replies had called
the crate's own `backtrace_if_absent!` through a path and failed E0433.
"""
from __future__ import annotations

from pipeline_core.domain.retro_gate import (
    DENIES_DEFINED_NAME,
    ConvictionEvidence,
    bind_crate_names,
    denied_defined_names,
    screen_guidance,
)

_GUIDANCE = (
    "Drop the hallucinated `crate::backtrace_if_absent!(&error)` macro "
    "invocation which does not exist and do not rebind `backtrace`; use the "
    "`backtrace: Option<Backtrace>` parameter that is already in scope to "
    "initialise `ErrorImpl { vtable, backtrace, _object: error }`.")


def test_the_live_lesson_denies_a_defined_macro() -> None:
    assert denied_defined_names(_GUIDANCE, {"backtrace_if_absent", "construct"}) == (
        "backtrace_if_absent",)
    # Negative control: a name the crate does not define is a fair denial.
    assert denied_defined_names(_GUIDANCE, {"construct"}) == ()
    # A defined name in a sentence that denies nothing is not a denial.
    assert denied_defined_names("Call `construct` with the vtable.",
                                {"construct"}) == ()


def test_screening_retires_it_only_while_names_are_bound() -> None:
    record = {"guidance": _GUIDANCE, "category": "API_HALLUCINATION",
              "error_count": 1, "retrospective_count": 1}
    ev = ConvictionEvidence(frozenset())
    try:
        bind_crate_names({"backtrace_if_absent"})
        got = screen_guidance(record, "", evidence=ev)
        assert got.text == "" and got.retired_by == DENIES_DEFINED_NAME
    finally:
        bind_crate_names(())
    after = screen_guidance(record, "", evidence=ev)
    assert after.retired_by != DENIES_DEFINED_NAME
