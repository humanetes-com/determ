"""Pins for the t1.5 re-trigger classifier (F11 M3)."""
from __future__ import annotations

from pipeline_core.domain.layout_retrigger import (
    CompileEvidence,
    LayoutRetriggerCause,
    classify_compile_retrigger,
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


def test_crash_retrigger_fires_on_member_or_referencing_suspect():
    from pipeline_core.domain.layout_retrigger import (
        CrashEvidence,
        classify_crash_retrigger,
    )
    records = [
        FnRecord(file="src/error.rs", fn_name="downcast_ref",
                 sig_text="fn downcast_ref<T>(&self) -> Option<&T>",
                 body_text="unsafe { (vtable(self.inner.ptr).object_downcast)(x) }"),
        FnRecord(file="src/chain.rs", fn_name="next",
                 sig_text="fn next(&mut self) -> Option<u8>", body_text="None"),
    ]
    comp = frozenset({"ErrorVTable", "object_downcast", "vtable"})
    # Suspect fn is a component MEMBER by name → fires.
    ev = CrashEvidence(vanished=frozenset({"test_boxed_str"}),
                       suspect_fns=frozenset({FnLocator("src/error.rs", "object_downcast")}))
    assert classify_crash_retrigger(ev, records, comp) is LayoutRetriggerCause.RUNTIME_CRASH
    # Suspect fn merely REFERENCES a component name in its body → fires.
    ev2 = CrashEvidence(vanished=frozenset({"test_boxed_str"}),
                        suspect_fns=frozenset({FnLocator("src/error.rs", "downcast_ref")}))
    assert classify_crash_retrigger(ev2, records, comp) is LayoutRetriggerCause.RUNTIME_CRASH
    # Suspect unrelated to the component → no fire.
    ev3 = CrashEvidence(vanished=frozenset({"test_iter"}),
                        suspect_fns=frozenset({FnLocator("src/chain.rs", "next")}))
    assert classify_crash_retrigger(ev3, records, comp) is LayoutRetriggerCause.NONE
    # No vanished tests / no suspects → never fires.
    assert classify_crash_retrigger(
        CrashEvidence(frozenset(), frozenset()), records, comp,
    ) is LayoutRetriggerCause.NONE


def test_crash_evidence_confounded_thresholds():
    from pipeline_core.domain.layout_retrigger import crash_evidence_confounded
    from pipeline_core.domain.models import FnRecord

    def _fn(name, stub):
        return FnRecord(file="src/error.rs", fn_name=name,
                        sig_text=f"fn {name}()", body_text="x",
                        has_unimplemented_body=stub)

    names = frozenset({"ErrorImpl", "a", "b"})
    # half or more stubbed → confounded
    assert crash_evidence_confounded([_fn("a", True), _fn("b", False)], names)
    # minority stubbed → real evidence
    assert not crash_evidence_confounded(
        [_fn("a", True), _fn("b", False), _fn("c", False)],
        frozenset({"a", "b", "c"}))
    # component with no fn members (types only) → never confounded
    assert not crash_evidence_confounded([_fn("x", True)], frozenset({"T"}))


# --- cause A from a cluster-wide build failure --------------------------------
_DIGEST = ("tests/test_autotrait.rs:5: error[E0277]: "
           "`NonNull<anyhow::error::ErrorImpl>` cannot be sent between "
           "threads safely")


def test_compile_evidence_retriggers_when_digest_names_a_private_type():
    c = classify_compile_retrigger(
        CompileEvidence(digest=_DIGEST, lib_clean=True, consecutive_sweeps=1),
        _PRIV)
    assert c is LayoutRetriggerCause.DESIGN_CONFLICT
    assert should_retrigger(c)


def test_compile_evidence_needs_no_streak():
    # A tree that cannot build reports no verdict for any test, so a second
    # broken sweep buys no information the first did not already give.
    c = classify_compile_retrigger(
        CompileEvidence(digest=_DIGEST, lib_clean=True, consecutive_sweeps=0),
        _PRIV)
    assert c is LayoutRetriggerCause.DESIGN_CONFLICT


def test_compile_evidence_with_broken_lib_never_retriggers():
    # The lib failing means a filled BODY is a live explanation; re-authoring
    # the layout would revert the whole component off one bad fill.
    c = classify_compile_retrigger(
        CompileEvidence(digest=_DIGEST, lib_clean=False), _PRIV)
    assert c is LayoutRetriggerCause.NONE
    assert not should_retrigger(c)


def test_compile_evidence_not_naming_a_private_type_does_not_retrigger():
    unrelated = ("tests/t.rs:1: error[E0425]: cannot find function `helper` "
                 "in this scope")
    c = classify_compile_retrigger(
        CompileEvidence(digest=unrelated, lib_clean=True), _PRIV)
    assert c is LayoutRetriggerCause.NONE


def test_compile_evidence_with_empty_digest_does_not_retrigger():
    # A link error or ICE parses to no diagnostic; acting then would aim the
    # re-author at nothing.
    c = classify_compile_retrigger(
        CompileEvidence(digest="   ", lib_clean=True), _PRIV)
    assert c is LayoutRetriggerCause.NONE
