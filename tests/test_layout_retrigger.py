"""Pins for the t1.5 re-trigger classifier (F11 M3)."""
from __future__ import annotations

from pipeline_core.domain.layout_retrigger import (
    LayoutRetriggerCause,
    classify_layout_retrigger,
    find_fns_referencing,
    should_retrigger,
)
from pipeline_core.domain.models import FnLocator, FnRecord
from pipeline_core.domain.starvation import StarvationCause

_PRIV = frozenset({"ErrorImpl", "ErrorVTable"})


def test_design_conflict_retriggers_when_it_names_a_private_type():
    err = "error[E0407]: method `not` is not a member of trait `Bool` for ErrorImpl"
    c = classify_layout_retrigger(
        starvation_cause=StarvationCause.DESIGN_CONFLICT, fills_exhausted=False,
        error_text=err, private_type_names=_PRIV)
    assert c is LayoutRetriggerCause.DESIGN_CONFLICT
    assert should_retrigger(c)


def test_design_conflict_without_private_type_evidence_does_not_retrigger():
    # starvation.py's classifier returns DESIGN_CONFLICT for ANY "no field X on
    # Y" line where Y merely appears in the prompt, whether or not X is
    # genuinely absent everywhere (a receiver-type mixup reads the same way,
    # but is fixable by an ordinary retry, not a layout problem). Without the
    # private type's own name in the error text, retriggering an expensive
    # whole-cluster re-author on a plain mislabeled bug must NOT happen.
    c = classify_layout_retrigger(
        starvation_cause=StarvationCause.DESIGN_CONFLICT, fills_exhausted=False,
        error_text="", private_type_names=_PRIV)
    assert c is LayoutRetriggerCause.NONE
    assert not should_retrigger(c)


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


def test_find_fns_referencing_matches_sig_and_body():
    records = [
        FnRecord(file="src/error.rs", fn_name="fmt",
                 sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                 body_text="let e: &ErrorImpl<()> = self.inner.by_ref(); Debug::fmt(&e._object, f)"),
        FnRecord(file="src/context.rs", fn_name="context",
                 sig_text="fn context<C>(self, context: C) -> Result<T, Error>",
                 body_text="self.map_err(|e| e)"),
        FnRecord(file="src/chain.rs", fn_name="next",
                 sig_text="fn next(&mut self) -> Option<&dyn StdError>",
                 body_text="self.0.next()"),
    ]
    got = find_fns_referencing(records, frozenset({"ErrorImpl"}))
    assert got == [FnLocator("src/error.rs", "fmt")]


def test_find_fns_referencing_empty_when_no_match():
    records = [FnRecord(file="src/chain.rs", fn_name="next",
                        sig_text="fn next(&mut self) -> Option<u8>", body_text="None")]
    assert find_fns_referencing(records, frozenset({"ErrorImpl"})) == []
