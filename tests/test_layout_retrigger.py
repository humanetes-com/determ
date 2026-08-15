"""Pins for the layout re-trigger DOMAIN (`domain/layout_retrigger.py`): which
cause a stalled or crashed cluster classifies as, how strongly an observed
fault implicates one component (`BlameGrade`), and the attempt, payoff,
mechanical and rejection budgets the walk spends. The ACTION that walks the
components lives one layer up and is pinned in `test_retrigger_layout.py`."""
from __future__ import annotations

from pipeline_core.domain.layout_retrigger import (
    PAYOFF_HIGH_KEY,
    PAYOFF_ZERO_KEY,
    CompileEvidence,
    CrashEvidence,
    LayoutRetriggerCause,
    classify_compile_retrigger,
    classify_layout_retrigger,
    find_fns_referencing,
    inflight_key,
    mark_retrigger_pending,
    mechanical_budget_spent,
    mechanical_key,
    payoff_budget_spent,
    reconcile_interrupted_attempts,
    settle_retrigger_payoff,
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


def test_find_fns_referencing_never_reverts_the_oracle():
    """A test body names the very types a re-author replaces. Reverting one
    stubs the suite that judges the re-author, so the oracle is excluded before
    the name test — including the un-attributed fixture items sharing its file,
    and whatever the (empty) cfg_predicates of a relocated sibling say."""
    records = [
        FnRecord(file="src/error.rs", fn_name="fmt",
                 sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                 body_text="let e: &ErrorImpl<()> = self.inner.by_ref();"),
        FnRecord(file="src/error.gen_tests_4.rs", fn_name="test_realloc",
                 is_test=True, sig_text="fn test_realloc()",
                 body_text="let e = ErrorImpl::new();"),
        # fixture impl inside the same sibling: no #[test], no cfg, still oracle
        FnRecord(file="src/error.gen_tests_4.rs", fn_name="fmt",
                 sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                 body_text="ErrorImpl::describe(f)"),
        # inline test that never reached a sibling — the cfg still disqualifies
        FnRecord(file="src/ptr.rs", fn_name="test_inline",
                 cfg_predicates=("test",), sig_text="fn test_inline()",
                 body_text="ErrorImpl::probe()"),
    ]
    assert find_fns_referencing(records, frozenset({"ErrorImpl"})) == [
        FnLocator("src/error.rs", "fmt")]


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


def test_crash_blame_decides_and_excludes_components_the_fault_never_touched():
    """The regression that pinned five sessions at ~111: a vanished test reaches
    most of the crate, so reverse-map suspects implicate several components at
    once and the caller took whichever was recorded first — firing at `Quoted`
    while every ASAN sweep blamed `src/error.rs::downcast`. When the localizer
    named the faulting fn, that blame decides; reachability is not consulted."""
    from pipeline_core.domain.layout_retrigger import (
        CrashEvidence,
        classify_crash_retrigger,
    )
    records = [
        FnRecord(file="src/error.rs", fn_name="downcast",
                 sig_text="fn downcast<E>(self) -> Result<E, Self>",
                 body_text="unsafe { (vtable(self.inner.ptr).object_downcast)(x) }"),
        FnRecord(file="src/fmt.rs", fn_name="quoted_write",
                 sig_text="fn quoted_write(&self)",
                 body_text="Quoted::new(self).write()"),
    ]
    blamed = frozenset({FnLocator("src/error.rs", "downcast")})
    # Both components are statically reachable from the same vanished test.
    reachable = frozenset({FnLocator("src/error.rs", "downcast"),
                           FnLocator("src/fmt.rs", "quoted_write")})
    vtable_comp = frozenset({"ErrorVTable", "object_downcast", "vtable"})
    quoted_comp = frozenset({"Quoted"})

    # Reachability alone cannot separate them — BOTH classify, which is what let
    # record order pick the wrong one.
    reach_only = CrashEvidence(vanished=frozenset({"test_downcast"}),
                               suspect_fns=reachable)
    assert classify_crash_retrigger(
        reach_only, records, vtable_comp) is LayoutRetriggerCause.RUNTIME_CRASH
    assert classify_crash_retrigger(
        reach_only, records, quoted_comp) is LayoutRetriggerCause.RUNTIME_CRASH

    # With blame, only the component the fault touches remains a candidate, so
    # the caller has nothing to fall through TO once that one is capped.
    with_blame = CrashEvidence(vanished=frozenset({"test_downcast"}),
                               suspect_fns=reachable, blamed_fns=blamed)
    assert classify_crash_retrigger(
        with_blame, records, vtable_comp) is LayoutRetriggerCause.RUNTIME_CRASH
    assert classify_crash_retrigger(
        with_blame, records, quoted_comp) is LayoutRetriggerCause.NONE

    # Blame reaches a component through a REFERENCE, not only by membership:
    # `downcast` is not a member of the vtable component, it uses its names.
    assert "downcast" not in vtable_comp

    # No blame (localization skipped, or no native fault found) → unchanged.
    assert classify_crash_retrigger(
        CrashEvidence(vanished=frozenset({"test_downcast"}),
                      suspect_fns=reachable, blamed_fns=frozenset()),
        records, quoted_comp) is LayoutRetriggerCause.RUNTIME_CRASH
    # Blame with no vanished test is not crash evidence at all.
    assert classify_crash_retrigger(
        CrashEvidence(vanished=frozenset(), suspect_fns=frozenset(),
                      blamed_fns=blamed),
        records, vtable_comp) is LayoutRetriggerCause.NONE
    # Blame fires even when the reverse map reached nothing: a stack frame is
    # evidence on its own, and static reachability is what missed `downcast`.
    assert classify_crash_retrigger(
        CrashEvidence(vanished=frozenset({"test_downcast"}),
                      suspect_fns=frozenset(), blamed_fns=blamed),
        records, vtable_comp) is LayoutRetriggerCause.RUNTIME_CRASH


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


# --- an attempt a process kill spent, and never measured ----------------------
_MEGA = "Backtrace|Error|ErrorImpl"


def test_interrupted_attempt_is_refunded_and_counted_as_mechanical():
    # The checkpoint charges the attempt BEFORE the authoring call, so a kill
    # anywhere in that window leaves a marker and a charge with no outcome.
    settled, refunded = reconcile_interrupted_attempts(
        {_MEGA: 2, inflight_key(_MEGA): 2})
    assert refunded == (_MEGA,)
    assert settled[_MEGA] == 1              # the model attempt is given back
    assert settled[mechanical_key(_MEGA)] == 1   # but the burn is still counted
    assert inflight_key(_MEGA) not in settled    # settled once, not every sweep


def test_reconcile_is_a_no_op_when_every_attempt_reached_an_outcome():
    attempts = {_MEGA: 2, mechanical_key(_MEGA): 1}
    settled, refunded = reconcile_interrupted_attempts(attempts)
    assert refunded == ()
    assert settled == attempts


def test_repeated_interruptions_still_exhaust_the_mechanical_bound():
    # The refund must not become a budget nothing can spend: an authoring call
    # that reliably kills the process would otherwise re-fire for ever.
    attempts: dict[str, int] = {}
    for _ in range(2):
        attempts[_MEGA] = attempts.get(_MEGA, 0) + 1
        attempts[inflight_key(_MEGA)] = attempts[_MEGA]
        attempts, _ = reconcile_interrupted_attempts(attempts)
    assert attempts[_MEGA] == 0
    assert mechanical_budget_spent(attempts, _MEGA)


def test_refund_never_drives_the_counter_negative():
    # A marker whose charge was already rolled back by a mechanical refund.
    settled, refunded = reconcile_interrupted_attempts(
        {_MEGA: 0, inflight_key(_MEGA): 1})
    assert settled[_MEGA] == 0
    assert refunded == (_MEGA,)


def test_bookkeeping_rows_are_never_mistaken_for_a_component():
    # `mech:` and `inflight:` share the file with the component counters; a
    # marker naming one of them must not refund a row that is not an attempt.
    settled, refunded = reconcile_interrupted_attempts(
        {mechanical_key(_MEGA): 1,
         inflight_key(mechanical_key(_MEGA)): 1})
    assert refunded == ()
    assert settled == {mechanical_key(_MEGA): 1}


def test_dropped_members_in_use_convicts_an_emptied_layout() -> None:
    """The 2026-08-01 `anyhow`/nightly incident, in miniature. A re-author cut
    `ErrorVTable` from seven fn-pointer fields to one; the skeleton compiled
    (its bodies are all `unimplemented!()`, so none reads a field) and the
    cluster went from 93 passing tests to a tree that cannot dispatch."""
    from pipeline_core.domain.layout_retrigger import dropped_members_in_use
    current = (
        "struct ErrorVTable {\n"
        "    object_drop: unsafe fn(Own<ErrorImpl>),\n"
        "    object_ref: unsafe fn(Ref<ErrorImpl>) -> Ref<dyn StdError>,\n"
        "    object_downcast: unsafe fn(Ref<ErrorImpl>, TypeId) -> Option<Ref<()>>,\n"
        "}"
    )
    authored = (
        "pub(crate) struct ErrorVTable {\n"
        "    object_drop: unsafe fn(core::ptr::NonNull<ErrorImpl>),\n"
        "}"
    )
    bodies = (
        "unsafe { (vtable(p).object_ref)(e) }\n"
        "let f = vtable(p).object_downcast;\n"
        "unsafe { (vtable(p).object_downcast)(e, target) }\n"
    )
    assert dropped_members_in_use(current, authored, bodies) == {
        "object_ref": 1, "object_downcast": 2}


def test_dropped_members_in_use_ignores_a_member_nothing_reads() -> None:
    """`object_backtrace` was dropped by the same re-author and is gated behind
    a cfg this build does not compile, so no body reads it. A member the crate
    is not using is not evidence against a redesign — the whole point of the
    re-trigger is that the model may drop what it does not need."""
    from pipeline_core.domain.layout_retrigger import dropped_members_in_use
    current = "struct V { kept: u8, object_backtrace: unsafe fn(u8) -> u8 }"
    authored = "struct V { kept: u8 }"
    assert dropped_members_in_use(current, authored, "let x = v.kept;") == {}


def test_dropped_members_in_use_passes_an_unchanged_definition() -> None:
    from pipeline_core.domain.layout_retrigger import dropped_members_in_use
    same = "struct ErrorImpl<E = ()> { vtable: &'static V, backtrace: B, _error: E }"
    bodies = "let a = x.vtable; let b = x.backtrace; let c = x._error;"
    assert dropped_members_in_use(same, same, bodies) == {}


def test_filled_body_text_excludes_stubs_and_the_oracle() -> None:
    """A stub reads no field by construction — that is exactly why a scaffold of
    them compiles against any layout, and why counting one as evidence would
    make the check as blind as the compile gate it exists to backstop."""
    from pipeline_core.domain.layout_retrigger import filled_body_text
    records = [
        FnRecord(file="src/error.rs", fn_name="object_ref",
                 body_text="unsafe { (vtable(p).object_ref)(e) }"),
        FnRecord(file="src/error.rs", fn_name="construct",
                 body_text=" unimplemented!() ", has_unimplemented_body=True),
        FnRecord(file="tests/test_downcast.rs", fn_name="test_downcast",
                 body_text="let _ = err.backtrace;", is_test=True),
    ]
    text = filled_body_text(records)
    assert "object_ref" in text
    assert "unimplemented" not in text
    assert "err.backtrace" not in text


def test_dropped_members_in_use_allows_a_redesign_that_trades_slots() -> None:
    """A re-trigger reverts every fn referencing the component so those fns can
    be rewritten against the new shape, so a filled body reading a renamed
    member is the NORMAL state of a genuine redesign. Convicting on that alone
    would refuse nearly every one and turn the escape hatch into a no-op."""
    from pipeline_core.domain.layout_retrigger import dropped_members_in_use
    current = "struct ErrorImpl { vtable: u8 }"
    authored = "struct ErrorImpl { msg: String }"
    assert dropped_members_in_use(current, authored, "let v = x.vtable;") == {}


def test_dropped_members_in_use_allows_a_named_to_tuple_respelling() -> None:
    from pipeline_core.domain.layout_retrigger import dropped_members_in_use
    current = "pub struct Error { ptr: Own<ErrorImpl> }"
    authored = "#[repr(transparent)]\npub struct Error(NonNull<ErrorImpl>);"
    bodies = "let p = self.ptr; drop(other.ptr);"
    assert dropped_members_in_use(current, authored, bodies) == {}


# --- the walk across components, and what each fire bought ---------------------
def _fire(attempts: dict[str, int], best_pass: int) -> dict[str, int]:
    """One retrigger decision point that FIRES, as `retrigger_layout` does it:
    settle what the previous fire bought, then mark this one pending."""
    return mark_retrigger_pending(settle_retrigger_payoff(attempts, best_pass),
                                  best_pass)


def test_a_walk_that_never_moves_the_score_stops_walking() -> None:
    """The 2026-08-02 `anyhow`/nightly cycle. The per-component cap held — the
    35-name mega component was refused every iter — but the loop walked past it
    to `Adhoc|AdhocKind` (twice) and `Boxed|BoxedKind` (twice). Each re-author
    re-stubbed 2-3 bodies for pass 93 -> 79 and each refilled back to 93: iters
    4, 8, 12, 16, four fires, four returns to the same number, with 14 further
    components holding 28 more attempts."""
    attempts: dict[str, int] = {}
    attempts = _fire(attempts, 93)          # iter 4  — nothing to settle yet
    assert not payoff_budget_spent(attempts)
    attempts = _fire(attempts, 93)          # iter 8  — iter 4 bought nothing
    assert not payoff_budget_spent(attempts)
    # iter 12 — iter 8 bought nothing either, and the budget is spent
    settled = settle_retrigger_payoff(attempts, 93)
    assert payoff_budget_spent(settled)


def test_a_fire_that_pays_clears_the_streak() -> None:
    """The budget bounds a walk, not the mechanism. One re-author that actually
    moves the cluster gives every remaining component its attempts back."""
    attempts = _fire(_fire({}, 93), 93)     # one zero-payoff fire on the books
    assert attempts[PAYOFF_ZERO_KEY] == 1
    # The re-fills under the second fire's layout land, and the sweep beats 93.
    settled = settle_retrigger_payoff(attempts, 97)
    assert settled[PAYOFF_ZERO_KEY] == 0
    assert settled[PAYOFF_HIGH_KEY] == 97
    assert not payoff_budget_spent(settled)
    # …and the streak it cleared cannot be resurrected by a later equal sweep:
    # the high-water mark moved, so 97 is no longer an improvement but the two
    # fires that preceded it have been forgiven.
    assert not payoff_budget_spent(settle_retrigger_payoff(settled, 97))


def test_one_fire_is_charged_once_however_many_call_sites_ask() -> None:
    """The TDD driver asks at three sites in one iter (build, crash, stall).
    Settling is idempotent — the pending mark is consumed when read — so one
    fire can never be charged as three."""
    attempts = _fire({}, 93)
    once = settle_retrigger_payoff(attempts, 93)
    twice = settle_retrigger_payoff(once, 93)
    thrice = settle_retrigger_payoff(twice, 93)
    assert once[PAYOFF_ZERO_KEY] == 1
    assert thrice == once


def test_an_unmeasured_sweep_convicts_nothing() -> None:
    """A sweep whose pass_count the caller could not read must not charge a
    retrigger that may well have paid — the floor's unset value is -1."""
    attempts = _fire({}, 93)
    assert settle_retrigger_payoff(attempts, None) == attempts
    assert settle_retrigger_payoff(attempts, -1) == attempts


def test_payoff_rows_are_never_mistaken_for_a_component() -> None:
    """`payoff:` shares the attempts file with the component counters, on the
    same rule as `mech:` and `inflight:`. A marker naming a payoff row must not
    refund it as though it were an attempt — which would drive the streak
    DOWN and hand the walk its budget back."""
    settled, refunded = reconcile_interrupted_attempts(
        {PAYOFF_ZERO_KEY: 2, inflight_key(PAYOFF_ZERO_KEY): 1})
    assert refunded == ()
    assert settled == {PAYOFF_ZERO_KEY: 2}
    assert payoff_budget_spent(settled)
    # …and an ordinary fire's own rows survive a reconcile untouched.
    attempts = _fire({}, 93)
    assert reconcile_interrupted_attempts(attempts) == (attempts, ())


def _stderr_collision_records() -> list[FnRecord]:
    """The live shape of cluster `stable` on 2026-08-08. Two unrelated symbols
    spell `StdError`: the crate's alias for `std::error::Error`, named in the
    signature of nearly every fn in `src/error.rs`, and a five-line private
    helper trait declared inside `mod ext` in `src/context.rs`."""
    return [
        FnRecord(file="src/error.rs", fn_name="object_reallocate_boxed",
                 sig_text=("unsafe fn object_reallocate_boxed(e: Own<ErrorImpl>)"
                           " -> Box<dyn StdError + Send + Sync + 'static>"),
                 body_text="let unerased = e.cast::<ErrorImpl<E>>().boxed();"),
        FnRecord(file="src/context.rs", fn_name="source",
                 container="impl<C, E> StdError for ContextError<C, E>",
                 sig_text="fn source(&self) -> Option<&(dyn StdError + 'static)>",
                 body_text="Some(&self.error)"),
    ]


def test_a_blamed_fn_a_component_declares_outranks_one_that_only_spells_its_name():
    """The trap this pins: ASAN localized a heap-use-after-free to
    `src/error.rs::object_reallocate_boxed`, a declared member of the 34-name
    Error/ErrorImpl/ErrorVTable component at registry index 13. The walk fired
    at index 3 instead — `StdError`, the private trait in `src/context.rs` —
    because the blamed fn's return type spells that token, and 29 bodies that
    also spell it were reverted. 49 passing became 24."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        CrashEvidence,
        classify_crash_retrigger,
        crash_candidate_grade,
        top_blame_tier,
    )
    records = _stderr_collision_records()
    ev = CrashEvidence(
        vanished=frozenset({"test_reallocate_into_boxed_dyn_error_without_backtrace_io"}),
        suspect_fns=frozenset(),
        blamed_fns=frozenset({FnLocator("src/error.rs",
                                        "object_reallocate_boxed")}))
    vtable_comp = frozenset({"Error", "ErrorImpl", "ErrorVTable", "Own",
                             "object_reallocate_boxed", "vtable"})
    stderr_comp = frozenset({"StdError"})

    # Both are admitted — that much is unchanged, and is why registry order
    # could decide.
    assert classify_crash_retrigger(
        ev, records, vtable_comp) is LayoutRetriggerCause.RUNTIME_CRASH
    assert classify_crash_retrigger(
        ev, records, stderr_comp) is LayoutRetriggerCause.RUNTIME_CRASH
    # The grade separates them, and the top tier holds only the declarer.
    assert crash_candidate_grade(ev, records, vtable_comp) is BlameGrade.OWNS
    assert crash_candidate_grade(
        ev, records, stderr_comp) is BlameGrade.REFERENCES
    assert top_blame_tier(
        [BlameGrade.REFERENCES, BlameGrade.OWNS]) is BlameGrade.OWNS
    assert top_blame_tier([]) is BlameGrade.NONE
    assert top_blame_tier([BlameGrade.NONE]) is BlameGrade.NONE


def test_a_method_is_owned_by_its_self_type_not_by_the_trait_it_implements():
    """`impl StdError for ContextError` declares a method OF `ContextError`.
    Reading the trait side too would hand every `impl Trait for T` in the crate
    to whichever component owns `Trait`, which for this crate is the same
    `src/context.rs` helper — the collision one level down."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        CrashEvidence,
        crash_candidate_grade,
        self_type_names,
    )
    records = _stderr_collision_records()
    ev = CrashEvidence(vanished=frozenset({"test_context_source"}),
                       suspect_fns=frozenset(),
                       blamed_fns=frozenset({FnLocator("src/context.rs",
                                                       "source")}))
    assert crash_candidate_grade(
        ev, records, frozenset({"ContextError"})) is BlameGrade.OWNS
    assert crash_candidate_grade(
        ev, records, frozenset({"StdError"})) is BlameGrade.REFERENCES

    assert self_type_names("impl<C, E> StdError for ContextError<C, E>") == \
        frozenset({"ContextError", "C", "E"})
    assert self_type_names("impl Error") == frozenset({"Error"})
    assert self_type_names("trait CastTo") == frozenset({"CastTo"})
    assert self_type_names("") == frozenset()


def test_a_fault_no_component_declares_still_reaches_the_ones_it_references():
    """The tier must not make cause C weaker where it was working. `downcast`
    is declared by no component; the vtable component's names appear in its
    body, and that is the whole of the evidence available."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        CrashEvidence,
        crash_candidate_grade,
        top_blame_tier,
    )
    records = [
        FnRecord(file="src/error.rs", fn_name="downcast",
                 sig_text="fn downcast<E>(self) -> Result<E, Self>",
                 body_text="unsafe { (vtable(self.inner.ptr).object_downcast)(x) }"),
    ]
    ev = CrashEvidence(vanished=frozenset({"test_downcast"}),
                       suspect_fns=frozenset(),
                       blamed_fns=frozenset({FnLocator("src/error.rs",
                                                       "downcast")}))
    comp = frozenset({"ErrorVTable", "object_downcast", "vtable"})
    assert crash_candidate_grade(ev, records, comp) is BlameGrade.REFERENCES
    assert top_blame_tier([BlameGrade.REFERENCES]) is BlameGrade.REFERENCES
    # A component nothing implicates stays out of the tier entirely.
    assert crash_candidate_grade(
        ev, records, frozenset({"Quoted"})) is BlameGrade.NONE


# --- cause C's own memory: signature refutation and signature payoff -----------
# The recorded instance (run `58819-ee78a1d1`, 2026-08-13, iters 3-6):
# `Quoted` landed attempt 1 against the deterministic `test_downcast` UAF, the
# identical crash reproduced two iterations later, and the walk re-picked
# `Quoted` on registry order — nothing remembered the crash had already
# survived its re-author. Pass oscillated 79/74/79/74 (~2.1h per cycle).


def _uaf_evidence(blamed: frozenset[FnLocator] = frozenset()) -> CrashEvidence:
    return CrashEvidence(
        vanished=frozenset({"test_large_alignment",
                            "test_no_backtrace_fallback"}),
        suspect_fns=frozenset({FnLocator("src/error.rs", "downcast")}),
        blamed_fns=blamed)


def test_the_signature_is_stable_across_fires_of_the_same_crash() -> None:
    from pipeline_core.domain.layout_retrigger import crash_signature
    # `consecutive_sweeps` and `suspect_fns` are excluded on purpose: the
    # first is a counter, the second widens as fills land — neither means the
    # crash changed.
    a = _uaf_evidence()
    b = CrashEvidence(vanished=a.vanished, suspect_fns=frozenset(),
                      consecutive_sweeps=3)
    assert crash_signature(a) == crash_signature(b)


def test_a_new_localizer_blame_changes_the_signature() -> None:
    from pipeline_core.domain.layout_retrigger import crash_signature
    # An origin-path blame arriving is NEW evidence; the refutations recorded
    # against the blameless signature must not carry over.
    assert crash_signature(_uaf_evidence()) != crash_signature(
        _uaf_evidence(frozenset({FnLocator("src/error.rs", "object_drop")})))


def test_a_landed_fire_refutes_its_component_for_that_signature_only() -> None:
    from pipeline_core.domain.layout_retrigger import (
        crash_component_refuted,
        crash_signature,
        record_crash_fire,
    )
    sig = crash_signature(_uaf_evidence())
    attempts = record_crash_fire({}, sig, "Quoted")
    assert crash_component_refuted(attempts, sig, "Quoted")
    assert not crash_component_refuted(attempts, sig, "Backtrace|Error")
    other = crash_signature(_uaf_evidence(
        frozenset({FnLocator("src/error.rs", "object_drop")})))
    assert not crash_component_refuted(attempts, other, "Quoted")


def test_the_identical_crash_back_settles_the_fire_as_zero_payoff() -> None:
    from pipeline_core.domain.layout_retrigger import (
        crash_payoff_budget_spent,
        crash_signature,
        record_crash_fire,
        settle_crash_payoff,
    )
    sig = crash_signature(_uaf_evidence())
    # Fire 1 lands (Quoted), crash reproduces; fire 2 lands (another
    # component), crash reproduces again: two zero-payoff fires and the walk
    # stops — the recorded cycle's projected iter-7 mega-component fire is
    # exactly what this refuses.
    attempts = settle_crash_payoff(record_crash_fire({}, sig, "Quoted"), sig)
    assert not crash_payoff_budget_spent(attempts)
    attempts = settle_crash_payoff(
        record_crash_fire(attempts, sig, "Backtrace|Error"), sig)
    assert crash_payoff_budget_spent(attempts)


def test_a_changed_signature_clears_the_streak_even_after_refusals() -> None:
    from pipeline_core.domain.layout_retrigger import (
        crash_payoff_budget_spent,
        crash_signature,
        record_crash_fire,
        settle_crash_payoff,
    )
    sig = crash_signature(_uaf_evidence())
    attempts = settle_crash_payoff(record_crash_fire({}, sig, "Quoted"), sig)
    attempts = settle_crash_payoff(
        record_crash_fire(attempts, sig, "Backtrace|Error"), sig)
    assert crash_payoff_budget_spent(attempts)
    # Budget spent -> later walks refuse WITHOUT firing, so no pending claim
    # exists any more. The identical crash keeps the budget spent...
    attempts = settle_crash_payoff(attempts, sig)
    assert crash_payoff_budget_spent(attempts)
    # ...and a CHANGED crash clears it — the reset keys on the last-seen
    # signature, never on a pending claim a refused walk cannot have opened.
    new_sig = crash_signature(_uaf_evidence(
        frozenset({FnLocator("src/error.rs", "object_drop")})))
    attempts = settle_crash_payoff(attempts, new_sig)
    assert not crash_payoff_budget_spent(attempts)


def test_settlement_is_idempotent_per_fire() -> None:
    from pipeline_core.domain.layout_retrigger import (
        PAYOFF_CRASH_ZERO_KEY,
        crash_signature,
        record_crash_fire,
        settle_crash_payoff,
    )
    sig = crash_signature(_uaf_evidence())
    once = settle_crash_payoff(record_crash_fire({}, sig, "Quoted"), sig)
    twice = settle_crash_payoff(once, sig)
    assert once.get(PAYOFF_CRASH_ZERO_KEY) == 1
    assert twice.get(PAYOFF_CRASH_ZERO_KEY) == 1


def test_sig_rows_are_never_mistaken_for_a_component() -> None:
    """`sig:` shares the attempts file with the component counters, on the
    same rule as `mech:` and `payoff:` — an interrupted-attempt marker naming
    a sig row must not be refunded as if it were a component."""
    from pipeline_core.domain.layout_retrigger import (
        crash_signature,
        reconcile_interrupted_attempts,
        record_crash_fire,
    )
    sig = crash_signature(_uaf_evidence())
    attempts = record_crash_fire({}, sig, "Quoted")
    attempts[f"inflight:sig:{sig}:Quoted"] = 1
    settled, refunded = reconcile_interrupted_attempts(attempts)
    assert refunded == ()
    assert f"inflight:sig:{sig}:Quoted" not in settled


# --- chain-derived blame: the third evidence grade ----------------------------
# Synthesized for the frame-blind crash class (run `635043-5f7c61f5`,
# 2026-08-14): a deterministic memory fault whose every ASAN stack resolves to
# std/test frames, so no localizer blame can ever land and the closed walk's
# own unlock — "a new localizer blame changes the signature" — was
# unsatisfiable by construction.


def _chain_evidence(chain: frozenset[FnLocator]) -> CrashEvidence:
    base = _uaf_evidence()
    return CrashEvidence(vanished=base.vanished,
                         suspect_fns=base.suspect_fns,
                         chain_blamed=chain)


def test_chain_blame_changes_the_signature_as_a_marker_not_a_list() -> None:
    from pipeline_core.domain.layout_retrigger import crash_signature
    bare = _uaf_evidence()
    chain_a = _chain_evidence(
        frozenset({FnLocator("src/error.rs", "construct_from_std")}))
    chain_b = _chain_evidence(
        frozenset({FnLocator("src/error.rs", "new"),
                   FnLocator("src/ptr.rs", "own")}))
    # The unlock: refutations and the zero-payoff streak were booked under
    # the blameless signature, and the chain blame landing must clear them.
    assert crash_signature(bare) != crash_signature(chain_a)
    # A marker, never the fn list: the chain includes producers matched
    # against live body text, so the set shifts as fills land — hashing it
    # would re-open the walk every iteration on a crash that never changed.
    assert crash_signature(chain_a) == crash_signature(chain_b)
    # A chain blame must not masquerade as a frame blame of the same fns.
    framed = _uaf_evidence(
        frozenset({FnLocator("src/error.rs", "construct_from_std")}))
    assert crash_signature(chain_a) != crash_signature(framed)


def test_legacy_signatures_unchanged_by_the_chain_field() -> None:
    import hashlib

    from pipeline_core.domain.layout_retrigger import crash_signature
    # Attempts stores persisted by earlier runs key refutations on the old
    # format; a payload with no chain blame must hash byte-identically.
    ev = _uaf_evidence(frozenset({FnLocator("src/error.rs", "object_drop")}))
    vanished = ",".join(sorted(ev.vanished))
    blamed = ",".join(sorted(
        f"{b.file}::{b.fn_name}" for b in ev.blamed_fns))
    legacy = hashlib.sha256(
        f"v={vanished};b={blamed}".encode()).hexdigest()[:12]
    assert crash_signature(ev) == legacy


def test_chain_blame_grades_above_suspects_and_below_frames() -> None:
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_candidate_grade,
    )
    records = [
        FnRecord(file="src/error.rs", fn_name="construct_from_std",
                 sig_text="fn construct_from_std<E>(e: E) -> Error",
                 body_text="unsafe { vtable install }"),
    ]
    comp = frozenset({"ErrorVTable", "construct_from_std", "vtable"})
    # Above suspects: the chain names the producer the mega component OWNS,
    # which reachability alone (empty here) never admits.
    chain_ev = CrashEvidence(
        vanished=frozenset({"test_drop"}), suspect_fns=frozenset(),
        chain_blamed=frozenset({FnLocator("src/error.rs",
                                          "construct_from_std")}))
    assert crash_candidate_grade(chain_ev, records, comp) is BlameGrade.OWNS
    # Below frames: an observed frame that IMPLICATES this component decides
    # ALONE — the chain is not consulted even where it would grade higher.
    # (This pin used to use a frame implicating NOTHING and assert NONE; that
    # case is the 2026-08-15 defect and now falls through to the chain, which
    # `test_a_frame_that_implicates_nothing_no_longer_silences_the_chain`
    # pins. What "a frame decides alone" means is unchanged wherever the
    # frame has anything to say about this component.)
    records = records + [
        FnRecord(file="src/fmt.rs", fn_name="fmt",
                 container="impl Display for Indented",
                 sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
                 body_text="let v = vtable(self.ptr); write!(f, \"{:?}\", v)"),
    ]
    both = CrashEvidence(
        vanished=frozenset({"test_drop"}), suspect_fns=frozenset(),
        blamed_fns=frozenset({FnLocator("src/fmt.rs", "fmt")}),
        chain_blamed=frozenset({FnLocator("src/error.rs",
                                          "construct_from_std")}))
    assert crash_candidate_grade(both, records, comp) is BlameGrade.REFERENCES


def test_members_in_use_names_what_the_veto_will_measure() -> None:
    """The prompt half of `dropped_members_in_use`, from the same two texts.
    Only members a filled body actually reads are named: a note that lists
    every declared member teaches the model nothing about which ones carry
    data."""
    from pipeline_core.domain.layout_retrigger import members_in_use
    current = "pub struct Error { ptr: Own<ErrorImpl>, spare: u8 }"
    bodies = "let p = self.ptr; drop(other.ptr);"
    assert members_in_use(current, bodies) == {"ptr": 2}


def test_members_in_use_text_is_empty_before_anything_is_filled() -> None:
    """The t1.5 bake authors against a tree with no bodies in it, so the note
    must cost the initial authoring nothing."""
    from pipeline_core.domain.layout_retrigger import (
        members_in_use,
        members_in_use_text,
    )
    current = "pub struct Error { ptr: Own<ErrorImpl> }"
    assert members_in_use(current, "") == {}
    assert members_in_use_text({"Error": {}}) == ""


def test_members_in_use_text_permits_the_redesign_it_constrains() -> None:
    """The veto is calibrated to slot COUNT, so the note must not tell the
    model to keep member identity — that would refuse the re-shaping the
    re-author exists to obtain."""
    from pipeline_core.domain.layout_retrigger import members_in_use_text
    text = members_in_use_text({"Error": {"ptr": 29}})
    assert "`ptr` (29 reads)" in text
    assert "FEWER storage slots" in text
    assert "re-shape" in text and "re-spell" in text


def test_a_rejection_streak_from_an_older_prompt_does_not_convict() -> None:
    """Run `929616-ee20d540` stood at rejection 2/3, both for dropping `ptr`,
    earned under a prompt that stated the member-eviction rule nowhere but in
    a list of past violations. Spending the last attempt under a prompt that
    now states it up front would retire the crashed family's only structural
    escape on evidence about a different prompt."""
    from pipeline_core.domain.layout_retrigger import (
        REJECTION_CONTRACT_EPOCH,
        contract_key,
        rejected_key,
        rejection_budget_spent,
        rejection_streak_is_stale,
    )
    legacy = {rejected_key("Error|ptr"): 2}
    assert rejection_streak_is_stale(legacy, "Error|ptr")
    assert not rejection_budget_spent(legacy, "Error|ptr")
    # Earned under THIS constraint set, the cap still bites — the staleness
    # rule releases a streak once, it does not remove the budget.
    current = {rejected_key("Error|ptr"): 3,
               contract_key("Error|ptr"): REJECTION_CONTRACT_EPOCH}
    assert not rejection_streak_is_stale(current, "Error|ptr")
    assert rejection_budget_spent(current, "Error|ptr")


# --- the aim: a frame that implicates NOBODY must not silence the chain --------
# Run `263217-d41a5bb5` (2026-08-15), cluster `stable`, both iterations
# identical: `unittests src/lib.rs` died of `AddressSanitizer: SEGV`, the
# origin was unattributed, and the only two frames in the whole report were
# the crashing TEST's own — a fn and its closure in the generated
# `src/error.gen_tests_4.rs`. The driver handed those two locators to the aim
# as `blamed_fns` (the crash evidence keys minus the chain keys), they graded
# NONE against every one of the cluster's 16 non-GATED components, and the
# grade returned on `blamed_fns` before the 67-fn chain — which grades OWNS
# on the construct/vtable component — could be read. 308 minutes, zero
# `[layout-retrigger]` lines.

# The live component key, verbatim from
# `tmp/anyhow/.skeleton/_layout_retrigger_attempts_stable.json` (2026-08-15).
_MEGA_KEY = (
    "Backtrace|CastTo|Error|ErrorImpl|ErrorVTable|Mut|Own|Ref|backtrace|chain|"
    "construct|construct_from_adhoc|construct_from_boxed|"
    "construct_from_context|construct_from_display|construct_from_std|"
    "context_backtrace|context_chain_downcast|context_chain_drop_rest|"
    "context_downcast|context_drop_rest|debug|display|erase|error|error_mut|"
    "no_backtrace|object_boxed|object_downcast|object_drop|object_drop_front|"
    "object_reallocate_boxed|object_ref|vtable")
_MEGA_NAMES = frozenset(_MEGA_KEY.split("|"))

# The two frames the localizer reported, and the residue the pre-fix
# subtraction left in `blamed_fns`.
_RESIDUE = frozenset({
    FnLocator("src/error.gen_tests_4.rs",
              "test_reallocate_into_boxed_dyn_error_without_backtrace"),
    FnLocator("src/error.gen_tests_4.rs", "{closure#0}"),
})
# One fn out of the 67 the chain synthesis emitted. `construct` is the one
# that decides: the mega component DECLARES it, so `crash_blame_grade`'s first
# clause grades OWNS — and the ASAN fault is a reallocate-into-boxed-dyn-error.
_CHAIN_CONSTRUCT = FnLocator("src/error.rs", "construct")


def _aim_records() -> list[FnRecord]:
    """Enough of the live tree to grade: the declared `construct`, and a
    `src/fmt.rs::fmt` that only SPELLS a component name (its self type is
    `Indented`, which the mega component does not hold)."""
    return [
        FnRecord(file="src/error.rs", fn_name="construct",
                 sig_text=("unsafe fn construct<E>(error: E, vtable: &'static "
                           "ErrorVTable, backtrace: Option<Backtrace>) -> Self"),
                 body_text=("let inner = Box::new(ErrorImpl { vtable, "
                            "backtrace, _object: error });")),
        FnRecord(file="src/fmt.rs", fn_name="fmt",
                 container="impl Display for Indented",
                 sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
                 body_text="let v = vtable(self.ptr); write!(f, \"{:?}\", v)"),
    ]


def _aim_evidence(blamed: frozenset[FnLocator],
                  chain: frozenset[FnLocator] = frozenset(),
                  suspects: frozenset[FnLocator] = frozenset()) -> CrashEvidence:
    """The run's payload shape: 19 vanished names, reduced to the one the
    ASAN report is about."""
    return CrashEvidence(
        vanished=frozenset(
            {"test_reallocate_into_boxed_dyn_error_without_backtrace"}),
        suspect_fns=suspects, blamed_fns=blamed, chain_blamed=chain)


def test_a_frame_that_implicates_nothing_no_longer_silences_the_chain() -> None:
    """Variant A→B of the 2026-08-15 probe. Same evidence, same records, same
    component: with the gen_tests residue in `blamed_fns` the aim graded NONE
    and emptied the candidate list; consulting the chain grades OWNS and puts
    the construct/vtable component back in the walk."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        classify_crash_retrigger,
        crash_blame_grade,
        crash_candidate_grade,
        top_blame_tier,
    )
    records = _aim_records()
    ev = _aim_evidence(_RESIDUE, frozenset({_CHAIN_CONSTRUCT}))
    assert crash_candidate_grade(ev, records, _MEGA_NAMES) is BlameGrade.OWNS
    assert top_blame_tier([BlameGrade.OWNS]) is BlameGrade.OWNS
    assert classify_crash_retrigger(
        ev, records, _MEGA_NAMES) is LayoutRetriggerCause.RUNTIME_CRASH

    # NEGATIVE CONTROL — the pre-fix decision, which was exactly
    # `return crash_blame_grade(evidence.blamed_fns, …)` the moment
    # `blamed_fns` was non-empty. On this fixture that is NONE, which is the
    # 308-minute silence in one line: no candidate, no tier, no log.
    assert crash_blame_grade(
        ev.blamed_fns, records, _MEGA_NAMES) is BlameGrade.NONE
    # …and it was NONE for every component, not just this one, which is why
    # `top_blame_tier` came back NONE and the walk fell to its silent exit.
    assert crash_blame_grade(
        ev.blamed_fns, records,
        frozenset({"Indented", "Quoted"})) is BlameGrade.NONE


def test_an_observed_frame_still_decides_alone_when_it_implicates_anything(
) -> None:
    """The ranking contract is unchanged wherever the frame has something to
    say: a REFERENCES frame beats an OWNS chain, and an OWNS frame is not
    diluted by a chain that grades NONE. Only a frame implicating NOTHING
    falls through."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_candidate_grade,
    )
    records = _aim_records()
    # Frame REFERENCES (`fmt` spells `vtable`), chain OWNS: the frame wins.
    weaker_frame = _aim_evidence(
        frozenset({FnLocator("src/fmt.rs", "fmt")}),
        frozenset({_CHAIN_CONSTRUCT}))
    assert crash_candidate_grade(
        weaker_frame, records, _MEGA_NAMES) is BlameGrade.REFERENCES
    # Frame OWNS, chain grades NONE here: still OWNS, chain never read.
    owning_frame = _aim_evidence(
        frozenset({_CHAIN_CONSTRUCT}),
        frozenset({FnLocator("src/nowhere.rs", "unknown")}))
    assert crash_candidate_grade(
        owning_frame, records, _MEGA_NAMES) is BlameGrade.OWNS


def test_a_frame_grading_none_with_no_chain_still_decides_and_skips_suspects(
) -> None:
    """The fall-through is scoped to "a chain inference exists". With no
    chain, a frame that implicates nothing keeps deciding — NONE, suspects
    unread — which is the rule `classify_crash_retrigger` has always stated:
    a component the observed fault does not touch is not a candidate."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_candidate_grade,
    )
    ev = _aim_evidence(_RESIDUE, frozenset(),
                       suspects=frozenset({_CHAIN_CONSTRUCT}))
    assert crash_candidate_grade(ev, _aim_records(), _MEGA_NAMES) is BlameGrade.NONE
    # The same suspects DO decide once no blame of any kind exists.
    bare = _aim_evidence(frozenset(), frozenset(),
                         suspects=frozenset({_CHAIN_CONSTRUCT}))
    assert crash_candidate_grade(bare, _aim_records(), _MEGA_NAMES) is BlameGrade.OWNS


def test_the_cause_c_signature_migrates_once_and_only_where_the_payload_moved(
) -> None:
    """`crash_signature` hashes `blamed_fns` verbatim, so changing WHAT the
    driver puts there moves the fingerprint. Three transitions, each asserted
    so the release of the live store's four `sig:` rows is a tested fact:
    frame-blind (residue → empty), frame-derived (all frames → origin only),
    and the class that does not move (already empty)."""
    from pipeline_core.domain.layout_retrigger import crash_signature
    # (a) frame-blind: the residue leaves, the `;c=chain` marker stays.
    chain = frozenset({_CHAIN_CONSTRUCT})
    pre = _aim_evidence(_RESIDUE, chain)
    post = _aim_evidence(frozenset(), chain)
    assert crash_signature(pre) != crash_signature(post)
    vanished = ",".join(sorted(post.vanished))
    import hashlib
    assert crash_signature(post) == hashlib.sha256(
        f"v={vanished};b=;c=chain".encode()).hexdigest()[:12]

    # (b) the unchanged class: a payload whose `blamed_fns` was already empty
    # pre-fix — no site carried a frame and no chain was synthesized — hashes
    # what it always hashed, so the attempts stores that class wrote keep
    # their refutations across this edit. Pinned by VALUE on purpose: an
    # assertion comparing the payload to a second copy of itself proves only
    # that `crash_signature` is a function of its fields, which no migration
    # could break, and would stay green through a change that moved this
    # class too.
    assert crash_signature(_aim_evidence(frozenset())) == hashlib.sha256(
        f"v={vanished};b=".encode()).hexdigest()[:12]

    # (c) frame-derived: the old payload carried every frame — the origin AND
    # the access frames `crash_evidence_by_locator` also keys — so narrowing
    # to the origin locator moves this signature too. Deliberate: the aim gets
    # sharper, and the refutation it releases was booked against a blame that
    # named an innocent caller (`ext_context` of the `object_drop` fault,
    # 2026-08-09/10).
    origin = FnLocator("src/error.rs", "object_drop")
    access = FnLocator("src/context.rs", "ext_context")
    assert crash_signature(_aim_evidence(frozenset({origin, access}))) != \
        crash_signature(_aim_evidence(frozenset({origin})))
