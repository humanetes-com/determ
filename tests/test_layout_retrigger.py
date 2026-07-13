"""Pins for the t1.5 re-trigger classifier (F11 M3)."""
from __future__ import annotations

from pipeline_core.domain.layout_retrigger import (
    LayoutRetriggerCause,
    classify_layout_retrigger,
    should_retrigger,
)
from pipeline_core.domain.starvation import StarvationCause

_PRIV = frozenset({"ErrorImpl", "ErrorVTable"})


def test_design_conflict_always_retriggers():
    c = classify_layout_retrigger(
        starvation_cause=StarvationCause.DESIGN_CONFLICT, fills_exhausted=False,
        error_text="", private_type_names=_PRIV)
    assert c is LayoutRetriggerCause.DESIGN_CONFLICT
    assert should_retrigger(c)


def test_layout_exhaustion_when_errors_hit_private_types():
    err = "error[E0609]: no field `vtable` on type `ErrorImpl`"
    c = classify_layout_retrigger(
        starvation_cause=StarvationCause.UNKNOWN, fills_exhausted=True,
        error_text=err, private_type_names=_PRIV)
    assert c is LayoutRetriggerCause.LAYOUT_EXHAUSTION


def test_no_retrigger_when_errors_are_not_layout_attributable():
    # exhausted, but the error names no authored private type → body problem.
    err = "error[E0308]: mismatched types: expected String, found &str"
    c = classify_layout_retrigger(
        starvation_cause=StarvationCause.UNKNOWN, fills_exhausted=True,
        error_text=err, private_type_names=_PRIV)
    assert c is LayoutRetriggerCause.NONE
    assert not should_retrigger(c)


def test_no_retrigger_when_not_exhausted():
    err = "error[E0599]: ErrorImpl has no method foo"
    c = classify_layout_retrigger(
        starvation_cause=StarvationCause.UNKNOWN, fills_exhausted=False,
        error_text=err, private_type_names=_PRIV)
    assert c is LayoutRetriggerCause.NONE


def test_empty_private_names_never_layout_exhaustion():
    err = "error[E0609]: no field on ErrorImpl"
    c = classify_layout_retrigger(
        starvation_cause=StarvationCause.UNKNOWN, fills_exhausted=True,
        error_text=err, private_type_names=frozenset())
    assert c is LayoutRetriggerCause.NONE
