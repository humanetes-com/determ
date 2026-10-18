"""Pins for the layout re-trigger DOMAIN (`domain/layout_retrigger.py`): which
cause a stalled or crashed cluster classifies as, how strongly an observed
fault implicates one component (`BlameGrade`), and the attempt, payoff,
mechanical and rejection budgets the walk spends. The ACTION that walks the
components lives one layer up and is pinned in `test_retrigger_layout.py`."""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.layout_retrigger import (
    PAYOFF_HIGH_KEY,
    PAYOFF_ZERO_KEY,
    CompileEvidence,
    CrashEvidence,
    LayoutRetriggerCause,
    charge_mechanical_failure,
    classify_compile_retrigger,
    classify_layout_retrigger,
    code_build_stamp,
    find_fns_referencing,
    inflight_key,
    inflight_pid_key,
    mark_retrigger_pending,
    mechanical_budget_spent,
    mechanical_build_key,
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


def test_crash_retrigger_admits_on_an_observed_frame_never_on_a_suspect():
    """Reachability (`suspect_fns`) is an inference and admits nothing; the
    SAME locators handed as an observed frame admit by membership or by
    reference. The reachability aim this replaced is the one that fired at
    `Quoted` and `ChainState` in registry order for five sessions, and the
    2026-09-06 fixture below shows it grading `Quoted` OWNS again on the
    live records the moment the chain was withheld."""
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
    member = frozenset({FnLocator("src/error.rs", "object_downcast")})
    referrer = frozenset({FnLocator("src/error.rs", "downcast_ref")})
    # As suspects: a component MEMBER by name, and a fn that REFERENCES the
    # component — neither admits.
    for fns in (member, referrer):
        ev = CrashEvidence(vanished=frozenset({"test_boxed_str"}),
                           suspect_fns=fns)
        assert classify_crash_retrigger(
            ev, records, comp) is LayoutRetriggerCause.NONE
    # The same locators as an observed frame admit — membership and reference.
    for fns in (member, referrer):
        ev = CrashEvidence(vanished=frozenset({"test_boxed_str"}),
                           suspect_fns=frozenset(), blamed_fns=fns)
        assert classify_crash_retrigger(
            ev, records, comp) is LayoutRetriggerCause.RUNTIME_CRASH
    # A frame unrelated to the component → no fire.
    ev3 = CrashEvidence(vanished=frozenset({"test_iter"}),
                        suspect_fns=frozenset(),
                        blamed_fns=frozenset({FnLocator("src/chain.rs", "next")}))
    assert classify_crash_retrigger(ev3, records, comp) is LayoutRetriggerCause.NONE
    # No vanished tests / no evidence → never fires.
    assert classify_crash_retrigger(
        CrashEvidence(frozenset(), frozenset()), records, comp,
    ) is LayoutRetriggerCause.NONE


def test_crash_blame_is_filled_body_only_when_every_named_body_compiles():
    """Admission still returns RUNTIME_CRASH; this is the fire gate.
    Empty blamed_fns, a missing record, and a stub body all keep the
    layout route. The live fixture is filled object_reallocate_boxed."""
    from pipeline_core.domain.layout_retrigger import (
        CrashEvidence,
        crash_blame_is_filled_body,
    )
    loc = FnLocator("src/error.rs", "object_reallocate_boxed")
    ev = CrashEvidence(vanished=frozenset({"t"}),
                       suspect_fns=frozenset(),
                       blamed_fns=frozenset({loc}))
    filled = [FnRecord(file="src/error.rs", fn_name="object_reallocate_boxed",
                       body_text="let unerased = e.cast::<ErrorImpl<E>>().boxed();")]
    assert crash_blame_is_filled_body(ev, filled) is True
    stub = [replace(filled[0], has_unimplemented_body=True,
                    body_text="unimplemented!()")]
    assert crash_blame_is_filled_body(ev, stub) is False, (
        "negative control: a stub blamed body still opens the layout route")
    assert crash_blame_is_filled_body(
        CrashEvidence(vanished=frozenset({"t"}),
                      suspect_fns=frozenset(),
                      blamed_fns=frozenset()),
        filled) is False
    assert crash_blame_is_filled_body(ev, []) is False


def test_a_filled_recovered_origin_is_a_filled_body():
    """The retrigger reads origin-blamed locators, and they are the whole
    blame: no undeclared sibling at the same file joins the set, so a filled
    recovered origin keeps filled-body True — the gate that would re-open
    cause C if a name catalog widened it (removed 2026-10-17)."""
    from pipeline_core.domain.crash_site import (
        CrashFrame,
        CrashSite,
        blamed_locators,
        origin_blamed_locators,
    )
    from pipeline_core.domain.layout_retrigger import (
        CrashEvidence,
        crash_blame_is_filled_body,
    )
    conv = CrashFrame(
        file_rel="src/error.rs", line=0,
        fn_name="reallocate_into_boxed_dyn_error_without_backtrace",
        symbol="reallocate_into_boxed_dyn_error_without_backtrace",
        named_by=("tests/error_gen_3.rs", 32))
    site = CrashSite(binary_label="tests/error_gen_3.rs", test_name="t",
                     signal="AddressSanitizer: SEGV", frames=(),
                     origin_frames=(conv,), origin_capable=True)
    filled = [FnRecord(
        file="src/error.rs",
        fn_name="reallocate_into_boxed_dyn_error_without_backtrace",
        body_text="let own = unsafe { ptr::read(&self.inner) }; mem::forget(self);")]
    vanished = frozenset({"t"})
    origin_ev = CrashEvidence(
        vanished=vanished, suspect_fns=frozenset(),
        blamed_fns=origin_blamed_locators((site,)))
    assert blamed_locators((site,)) == origin_blamed_locators((site,))
    assert crash_blame_is_filled_body(origin_ev, filled) is True
    unfilled = CrashEvidence(
        vanished=vanished, suspect_fns=frozenset(),
        blamed_fns=origin_blamed_locators((site,))
        | {FnLocator("src/error.rs", "undeclared_sibling")})
    assert crash_blame_is_filled_body(unfilled, filled) is False, (
        "negative control: an undeclared locator in the blame is not filled")


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

    # Reachability alone cannot separate them — and so admits NEITHER. (It
    # used to admit both, which is what let record order pick the wrong one.)
    reach_only = CrashEvidence(vanished=frozenset({"test_downcast"}),
                               suspect_fns=reachable)
    assert classify_crash_retrigger(
        reach_only, records, vtable_comp) is LayoutRetriggerCause.NONE
    assert classify_crash_retrigger(
        reach_only, records, quoted_comp) is LayoutRetriggerCause.NONE

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

    # No blame (localization skipped, or no native fault found) → nothing is
    # admitted; the fill loop works the crash with its evidence pack.
    assert classify_crash_retrigger(
        CrashEvidence(vanished=frozenset({"test_downcast"}),
                      suspect_fns=reachable, blamed_fns=frozenset()),
        records, quoted_comp) is LayoutRetriggerCause.NONE
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


def test_a_deliberate_stop_refunds_without_a_mechanical_charge():
    """The 2026-08-15 lockout, in miniature. Two operator SIGTERMs during the
    34-name construct/vtable component's re-author took `mech:` to its cap of 2,
    and the run after them refused a component it had just graded OWNS off an
    in-crate ASAN origin. The charge answers "does this authoring call kill its
    own process"; a stop typed by the operator answers nothing about it."""
    attempts = {_MEGA: 1, inflight_key(_MEGA): 1, inflight_pid_key(_MEGA): 4242}
    settled, refunded = reconcile_interrupted_attempts(
        attempts, stopped_pids=frozenset({4242}))
    assert refunded == (_MEGA,)
    assert settled[_MEGA] == 0                     # the attempt is still repaid
    assert mechanical_key(_MEGA) not in settled    # …and nothing else is spent
    assert inflight_pid_key(_MEGA) not in settled  # settled once, like the marker


def test_a_death_the_record_does_not_call_deliberate_still_charges():
    """The negative control for the case above, and the case the bound was
    written for: an OOM kill or an abort leaves the same marker, and nothing may
    read it as an operator decision."""
    attempts = {_MEGA: 1, inflight_key(_MEGA): 1, inflight_pid_key(_MEGA): 4242}
    settled, _ = reconcile_interrupted_attempts(
        attempts, stopped_pids=frozenset({9999}))
    assert settled[mechanical_key(_MEGA)] == 1


def test_a_marker_with_no_recorded_pid_charges_as_before():
    # Stores written before the pid row existed: there is no process to ask
    # about, so the pre-2026-08-15 behaviour stands.
    settled, _ = reconcile_interrupted_attempts(
        {_MEGA: 1, inflight_key(_MEGA): 1}, stopped_pids=frozenset({4242}))
    assert settled[mechanical_key(_MEGA)] == 1


_BUILD_A = code_build_stamp("459594789b98")
_BUILD_B = code_build_stamp("27883c3b25e4")


def test_a_streak_from_another_build_does_not_refuse():
    """`mech:` has exactly one clear — the accepted-re-author path, which sits
    downstream of the refusal — so without this the cap is permanent for every
    later run. The live store on 2026-08-15 read `mech:<34-name key>: 2` with no
    build row at all, and that is the shape that must release."""
    live = {mechanical_key(_MEGA): 2}
    assert not mechanical_budget_spent(live, _MEGA, code_stamp=_BUILD_A)
    # The negative control is the reader that has no build to name: unstamped
    # store, unstamped reader, and the cap still refuses — which is what every
    # pre-2026-08-15 caller does and what the existing pins assert.
    assert mechanical_budget_spent(live, _MEGA)


def test_a_streak_this_build_earned_still_refuses():
    attempts = charge_mechanical_failure(
        charge_mechanical_failure({}, _MEGA, _BUILD_A), _MEGA, _BUILD_A)
    assert attempts[mechanical_key(_MEGA)] == 2
    assert mechanical_budget_spent(attempts, _MEGA, code_stamp=_BUILD_A)


def test_charging_under_a_new_build_restarts_the_streak():
    # Not "adds to it": the earlier failures were a claim about code that no
    # longer runs, and a streak mixing two builds says nothing about either.
    attempts = charge_mechanical_failure(
        {mechanical_key(_MEGA): 2, mechanical_build_key(_MEGA): _BUILD_A},
        _MEGA, _BUILD_B)
    assert attempts[mechanical_key(_MEGA)] == 1
    assert attempts[mechanical_build_key(_MEGA)] == _BUILD_B
    assert not mechanical_budget_spent(attempts, _MEGA, code_stamp=_BUILD_B)


def test_an_unstamped_charge_writes_no_build_row():
    # Build 0 is what an absent row already reads as, so an unwired caller must
    # leave the store as it found it rather than assert an unknown build.
    attempts = charge_mechanical_failure({}, _MEGA)
    assert attempts == {mechanical_key(_MEGA): 1}


def test_code_build_stamp_is_total():
    assert code_build_stamp(None) == 0
    assert code_build_stamp("") == 0
    assert code_build_stamp("not-hex-at-all") == 0
    assert code_build_stamp("459594789b98") == int("459594789b98", 16)


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


def test_an_undeclared_origin_beside_a_declared_sibling_still_declines():
    """When the origin frame names a fn the extractor has no record for,
    while a same-file sibling is a declared member of some component, that
    component must not grade OWNS on the sibling's name alone. Declining is
    the intended trade: the origin locator is present, the extractor has no
    record for it. Empty blamed_fns (unattributed) is a different decline.
    Negative control: a blame that also names the declared sibling (what a
    name catalog's widening produced, removed 2026-10-17) grades OWNS."""
    from pipeline_core.domain.crash_site import (
        CrashFrame,
        CrashSite,
        blamed_locators,
        origin_blamed_locators,
    )
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        CrashEvidence,
        crash_candidate_grade,
    )
    conv = CrashFrame(
        file_rel="src/error.rs", line=0,
        fn_name="into_boxed_dyn_error",
        symbol="into_boxed_dyn_error",
        named_by=("tests/error_gen_3.rs", 32))
    site = CrashSite(binary_label="tests/error_gen_3.rs", test_name="t",
                     signal="AddressSanitizer: SEGV", frames=(),
                     origin_frames=(conv,), origin_capable=True)
    # Sibling declared; origin name has no FnRecord.
    records = _stderr_collision_records()[:1]
    vtable_comp = frozenset({"Error", "ErrorImpl", "ErrorVTable", "Own",
                             "object_reallocate_boxed", "vtable"})
    vanished = frozenset({"t"})
    origin_ev = CrashEvidence(
        vanished=vanished, suspect_fns=frozenset(),
        blamed_fns=origin_blamed_locators((site,)))
    assert blamed_locators((site,)) == origin_blamed_locators((site,))
    assert crash_candidate_grade(
        origin_ev, records, vtable_comp) is BlameGrade.NONE
    sibling = FnLocator(records[0].file, records[0].fn_name)
    widened = CrashEvidence(
        vanished=vanished, suspect_fns=frozenset(),
        blamed_fns=origin_blamed_locators((site,)) | {sibling})
    assert crash_candidate_grade(
        widened, records, vtable_comp) is BlameGrade.OWNS, (
        "negative control: naming the declared sibling grades OWNS")


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


def test_a_shrinking_vanished_set_does_not_re_open_the_refuted_aim() -> None:
    """THE FIX. Run `1920662-40f711b2` (2026-08-16/17): the retrigger refused
    this component at iteration 1 against
    `{test_large_alignment, test_no_backtrace_fallback}`, the promoted dispatch
    then filled `object_drop_front`, `tests/test_downcast.rs` went 0/7 to 4/3
    and `vanished` became `{test_no_backtrace_fallback}` — a strict subset.
    Hash-exact, that is a different key with no row, so the aim re-opened, fired
    twice, and the second fire reverted 54 bodies for pass 83 -> 25."""
    from pipeline_core.domain.layout_retrigger import (
        crash_blame_signature,
        crash_component_refutation,
        crash_signature,
        record_crash_fire,
    )
    before = _uaf_evidence()
    after = CrashEvidence(
        vanished=frozenset({"test_no_backtrace_fallback"}),
        suspect_fns=before.suspect_fns)
    attempts = record_crash_fire(
        {}, crash_signature(before), "Error",
        blame_sig=crash_blame_signature(before), vanished=before.vanished)

    r = crash_component_refutation(
        attempts, crash_signature(after), "Error",
        blame_sig=crash_blame_signature(after), vanished=after.vanished)
    assert r.refuted, "the fix that shrank the crash re-authorised the fire"
    assert not r.exact, "this is the subset branch, not the hash-exact one"
    assert r.fixed == frozenset({"test_large_alignment"}), (
        "the refusal must be able to NAME what was repaired — the silent "
        "version of this line is what cost the night")


def test_a_new_vanished_test_outside_the_scope_still_re_opens_the_aim() -> None:
    """The subset rule must not become a blanket immunity: a binary going down
    that was not down before is new damage and new evidence."""
    from pipeline_core.domain.layout_retrigger import (
        crash_blame_signature,
        crash_component_refutation,
        crash_signature,
        record_crash_fire,
    )
    before = _uaf_evidence()
    attempts = record_crash_fire(
        {}, crash_signature(before), "Error",
        blame_sig=crash_blame_signature(before), vanished=before.vanished)
    # Same two names, plus one the scope has never seen.
    wider = CrashEvidence(
        vanished=before.vanished | {"test_ffi_alignment"},
        suspect_fns=before.suspect_fns)
    assert not crash_component_refutation(
        attempts, crash_signature(wider), "Error",
        blame_sig=crash_blame_signature(wider),
        vanished=wider.vanished).refuted
    # …and so does the blame-marker transition the signature already sanctions.
    blamed = _uaf_evidence(frozenset({FnLocator("src/error.rs", "object_drop")}))
    narrower = CrashEvidence(
        vanished=frozenset({"test_no_backtrace_fallback"}),
        suspect_fns=blamed.suspect_fns, blamed_fns=blamed.blamed_fns)
    assert not crash_component_refutation(
        attempts, crash_signature(narrower), "Error",
        blame_sig=crash_blame_signature(narrower),
        vanished=narrower.vanished).refuted


def test_negative_control_the_hash_exact_lookup_re_opens_on_the_repair() -> None:
    """Without this the case above proves nothing: it asserts a refusal that a
    lookup broken some other way would also produce. Drive the SAME transition
    with no scope recorded — which is exactly the hash-exact behaviour, since
    the subset branch has nothing to match against — and the aim re-opens."""
    from pipeline_core.domain.layout_retrigger import (
        crash_blame_signature,
        crash_component_refutation,
        crash_signature,
        record_crash_fire,
    )
    before = _uaf_evidence()
    after = CrashEvidence(vanished=frozenset({"test_no_backtrace_fallback"}),
                          suspect_fns=before.suspect_fns)
    # Pre-fix store: the landed fire booked the row and no scope.
    attempts = record_crash_fire({}, crash_signature(before), "Error")
    assert not crash_component_refutation(
        attempts, crash_signature(after), "Error",
        blame_sig=crash_blame_signature(after),
        vanished=after.vanished).refuted, (
        "the control no longer reproduces the burn")
    # …and the hash-exact refusal it DOES still make reports that it has no
    # scope, which is what makes the caller backfill one.
    same = crash_component_refutation(
        attempts, crash_signature(before), "Error",
        blame_sig=crash_blame_signature(before), vanished=before.vanished)
    assert same.refuted and same.exact and same.needs_scope


def test_the_backfilled_scope_covers_the_next_shrink() -> None:
    """The backfill is what arms the fix on a store written before it existed —
    the live `_layout_retrigger_attempts_stable.json` on 2026-08-17. The refusal
    being made right now is a refusal against the crash in front of it, so that
    crash is a valid scope."""
    from pipeline_core.domain.layout_retrigger import (
        crash_blame_signature,
        crash_component_refutation,
        crash_signature,
        record_crash_fire,
        record_crash_scope,
    )
    before = _uaf_evidence()
    attempts = record_crash_fire({}, crash_signature(before), "Error")
    attempts = record_crash_scope(
        attempts, crash_blame_signature(before), "Error", before.vanished)
    after = CrashEvidence(vanished=frozenset({"test_no_backtrace_fallback"}),
                          suspect_fns=before.suspect_fns)
    assert crash_component_refutation(
        attempts, crash_signature(after), "Error",
        blame_sig=crash_blame_signature(after), vanished=after.vanished).refuted


def test_one_signature_may_buy_only_so_many_authoring_rounds() -> None:
    """The hole a rejected round fell through: the refutation counts LANDED
    fires, so a rejected round —
    ~55 min and ~14 model calls on run `1920662-40f711b2`'s iteration 2, three
    attempts all rejected at the compile gate — was recorded nowhere and the
    identical signature re-fired at iteration 4."""
    from pipeline_core.domain.layout_retrigger import (
        DEFAULT_MAX_CRASH_ROUNDS,
        charge_crash_round,
        crash_round_budget_spent,
        crash_rounds_spent,
        crash_signature,
    )
    sig = crash_signature(_uaf_evidence())
    attempts: dict[str, int] = {}
    for n in range(DEFAULT_MAX_CRASH_ROUNDS):
        assert not crash_round_budget_spent(attempts, sig, "Error"), n
        attempts = charge_crash_round(attempts, sig, "Error")
    assert crash_round_budget_spent(attempts, sig, "Error")
    assert crash_rounds_spent(attempts, sig, "Error") == DEFAULT_MAX_CRASH_ROUNDS
    # Per component and per signature, not per cluster: another component is
    # untouched, and so is a changed crash.
    assert not crash_round_budget_spent(attempts, sig, "Quoted")
    other = crash_signature(_uaf_evidence(
        frozenset({FnLocator("src/error.rs", "object_drop")})))
    assert not crash_round_budget_spent(attempts, other, "Error")


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


def test_chain_blame_grades_nothing_and_a_frame_decides_alone() -> None:
    """The chain is prompt evidence and a signature marker, never an aim:
    a chain naming a fn the component DECLARES grades NONE, and the same
    locator handed as an observed frame grades OWNS — the negative control
    is the grade this replaced, which read the chain and returned OWNS."""
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
    chain_ev = CrashEvidence(
        vanished=frozenset({"test_drop"}), suspect_fns=frozenset(),
        chain_blamed=frozenset({FnLocator("src/error.rs",
                                          "construct_from_std")}))
    assert crash_candidate_grade(chain_ev, records, comp) is BlameGrade.NONE
    assert _grade_until_2026_09_06(chain_ev, records, comp) is BlameGrade.OWNS
    framed = CrashEvidence(
        vanished=frozenset({"test_drop"}), suspect_fns=frozenset(),
        blamed_fns=frozenset({FnLocator("src/error.rs",
                                        "construct_from_std")}))
    assert crash_candidate_grade(framed, records, comp) is BlameGrade.OWNS
    # An observed frame that IMPLICATES this component decides ALONE — the
    # chain beside it is not consulted even where it would grade higher.
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


def test_a_rejection_streak_from_another_build_does_not_refuse() -> None:
    """The same permanent-lock shape `mechanical_streak_is_stale` was written
    for, one `if` away from it and left behind. Run `1408670-3c074ece`
    (2026-08-16) printed both readings on the same sweep: `2 mechanical
    failure(s) … earned by a different build … NOT refusing this aim`
    immediately above `3 consecutive re-author(s) REJECTED … Not retriggering`.
    Only an ACCEPTED re-author clears `rejected:`, and this refusal sits
    upstream of the only path that can accept one, so the streak was
    unspendable for every future run."""
    from pipeline_core.domain.layout_retrigger import (
        REJECTION_CONTRACT_EPOCH,
        contract_key,
        rejected_key,
        rejection_budget_spent,
        rejection_build_key,
    )
    # The live store's shape: at the cap, current epoch, earned by build A.
    live = {rejected_key(_MEGA): 3,
            contract_key(_MEGA): REJECTION_CONTRACT_EPOCH,
            rejection_build_key(_MEGA): _BUILD_A}
    assert rejection_budget_spent(live, _MEGA, code_stamp=_BUILD_A)
    assert not rejection_budget_spent(live, _MEGA, code_stamp=_BUILD_B)
    # Negative control, matching the mechanical rule: an unstamped reader over
    # an unstamped store still refuses, so every pre-existing caller is
    # unchanged.
    legacy = {rejected_key(_MEGA): 3, contract_key(_MEGA): REJECTION_CONTRACT_EPOCH}
    assert rejection_budget_spent(legacy, _MEGA)


def test_charging_a_rejection_under_a_new_build_restarts_the_streak() -> None:
    """The write half. Stamping only on the read side is how a streak becomes
    unspendable: the release fires once, the next rejection extends a count
    earned under code that no longer runs, and the cap is reached again on
    evidence about two different builds."""
    from pipeline_core.domain.layout_retrigger import (
        REJECTION_CONTRACT_EPOCH,
        charge_rejection,
        contract_key,
        rejected_key,
        rejection_budget_spent,
        rejection_build_key,
    )
    attempts = {rejected_key(_MEGA): 3,
                contract_key(_MEGA): REJECTION_CONTRACT_EPOCH,
                rejection_build_key(_MEGA): _BUILD_A}
    retired = charge_rejection(attempts, _MEGA, _BUILD_B)
    assert retired == 3
    assert attempts[rejected_key(_MEGA)] == 1
    assert attempts[rejection_build_key(_MEGA)] == _BUILD_B
    assert not rejection_budget_spent(attempts, _MEGA, code_stamp=_BUILD_B)
    # Same build again extends rather than restarts, and reaches the cap.
    assert charge_rejection(attempts, _MEGA, _BUILD_B) == 0
    assert charge_rejection(attempts, _MEGA, _BUILD_B) == 0
    assert rejection_budget_spent(attempts, _MEGA, code_stamp=_BUILD_B)
    # An unstamped charge writes no build row — build 0 is what an absent row
    # already reads as (`charge_mechanical_failure`'s rule).
    fresh: dict[str, int] = {}
    charge_rejection(fresh, _MEGA)
    assert rejection_build_key(_MEGA) not in fresh


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


def _grade_until_2026_09_06(evidence, fn_records, component_names):
    """`crash_candidate_grade` as it stood from 2026-08-15 to 2026-09-06,
    verbatim: a frame decides wherever it implicates something, a frame
    implicating nothing falls through to the chain, and with no frame the
    chain and then the suspects grade. The negative control of every pin
    below — this is the grade that admitted `Quoted` off
    `src/context.rs::fmt` on run `1577437-c93d5c26` iter 3."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_blame_grade,
    )
    if not evidence.vanished:
        return BlameGrade.NONE
    if evidence.blamed_fns:
        frame_grade = crash_blame_grade(
            evidence.blamed_fns, fn_records, component_names)
        if frame_grade is not BlameGrade.NONE or not evidence.chain_blamed:
            return frame_grade
    if evidence.chain_blamed:
        return crash_blame_grade(
            evidence.chain_blamed, fn_records, component_names)
    if not evidence.suspect_fns:
        return BlameGrade.NONE
    return crash_blame_grade(
        evidence.suspect_fns, fn_records, component_names)


def test_a_frame_that_implicates_nothing_decides_none_and_the_chain_is_never_read(
) -> None:
    """The 2026-08-15 probe, re-pinned to the contract that replaced the
    fall-through. Same evidence, same records, same component: the gen_tests
    residue in `blamed_fns` grades NONE, and the chain beside it is NOT
    consulted — the walk declines and says so (`retrigger_layout`'s
    declined-walk line), which is the diagnosis that run needed. Consulting
    the chain here is what the negative control does, and on this fixture it
    grades the mega component OWNS: the fall-through was the same aim-by-
    inference that fired `Quoted` on 2026-09-06."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        classify_crash_retrigger,
        crash_candidate_grade,
        top_blame_tier,
    )
    records = _aim_records()
    ev = _aim_evidence(_RESIDUE, frozenset({_CHAIN_CONSTRUCT}))
    assert crash_candidate_grade(ev, records, _MEGA_NAMES) is BlameGrade.NONE
    assert crash_candidate_grade(
        ev, records, frozenset({"Indented", "Quoted"})) is BlameGrade.NONE
    assert top_blame_tier([BlameGrade.NONE]) is BlameGrade.NONE
    assert classify_crash_retrigger(
        ev, records, _MEGA_NAMES) is LayoutRetriggerCause.NONE
    # NEGATIVE CONTROL — the fall-through grade admitted the mega component.
    assert _grade_until_2026_09_06(ev, records, _MEGA_NAMES) is BlameGrade.OWNS


def _recorded_chain_blame_1577437() -> dict:
    """The `phase_c.crash.chain_blame` event of run `1577437-c93d5c26` iter 3
    (2026-09-06T22:14:42Z), the cluster's 17 component records, and the
    extractor's records for the chain-blamed and reverse-map-suspect fns —
    snapshotted from `logs/events.jsonl`, `.skeleton/_layout_outcomes_stable
    .json` and `skeleton-extractor --enumerate-fns` over the filled tree, not
    typed. The event caps its list at 50 of the 63 chain keys."""
    import json
    from pathlib import Path
    path = (Path(__file__).resolve().parent / "fixtures" / "crash"
            / "chain_blame_1577437-c93d5c26_2026-09-06_iter3.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _recorded_records(fx: dict) -> list[FnRecord]:
    return [FnRecord(file=r["file"], fn_name=r["fn_name"],
                     container=r["container"], sig_text=r["sig_text"],
                     has_unimplemented_body=r["has_unimplemented_body"])
            for r in fx["fn_records"]]


def test_the_recorded_chain_of_1577437_admits_no_component() -> None:
    """The live instance, replayed on the live records: `blamed_fns` empty,
    the recorded chain (50 of 63 names, as the event capped it), the 16
    non-GATED components of cluster `stable`. No component grades OWNS — or
    anything — so the walk declines instead of re-authoring `Quoted`.

    Negative control, the grade that ran that night: 7 of the 16 grade OWNS
    off the recorded 50 (the log's 8 came off the full 63), `Quoted` among
    them by `src/context.rs::fmt` and `::write_str` — methods whose self
    type is `Quoted` — and attempt 2 fired on it.

    Second control, the reason `suspect_fns` is withheld too: the recorded
    reverse-map suspects (34 fns) alone, under that same grade, admit the
    same `Quoted` by the same two methods. Withholding the chain and leaving
    reachability would have re-opened the identical fire."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_candidate_grade,
        top_blame_tier,
    )
    fx = _recorded_chain_blame_1577437()
    data = fx["event"]["data"]
    assert data["provenance"] == "chain_derived"
    assert data["chain_count"] == 63 and len(data["blamed"]) == 50
    records = _recorded_records(fx)
    chain = frozenset(FnLocator(*n.split("::", 1)) for n in data["blamed"])
    suspects = frozenset(FnLocator(*n.split("::", 1)) for n in fx["suspects"])
    assert len(suspects) == data["suspect_count"] == 34
    components = [frozenset(c["item_names"]) for c in fx["components"]
                  if c["outcome"] != "GATED"]
    assert len(components) == 16
    quoted = next(c for c in components if c == frozenset({"Quoted"}))

    live = CrashEvidence(vanished=frozenset(data["tests"]),
                         suspect_fns=suspects, chain_blamed=chain)
    grades = [crash_candidate_grade(live, records, c) for c in components]
    assert top_blame_tier(grades) is BlameGrade.NONE
    assert set(grades) == {BlameGrade.NONE}

    # NEGATIVE CONTROL — the grade of 2026-09-06, on the same input.
    old = {"|".join(sorted(c)): _grade_until_2026_09_06(live, records, c)
           for c in components}
    owns = sorted(k for k, g in old.items() if g is BlameGrade.OWNS)
    assert "Quoted" in owns and len(owns) == 7, owns
    assert _grade_until_2026_09_06(live, records, quoted) is BlameGrade.OWNS
    # …and reachability alone, chain withheld, admits `Quoted` the same way.
    reach_only = CrashEvidence(vanished=frozenset(data["tests"]),
                               suspect_fns=suspects)
    assert _grade_until_2026_09_06(
        reach_only, records, quoted) is BlameGrade.OWNS
    assert crash_candidate_grade(reach_only, records, quoted) is BlameGrade.NONE


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


def test_a_frame_grading_none_decides_and_suspects_never_do(
) -> None:
    """A frame that implicates nothing decides — NONE, suspects unread —
    which is the rule `classify_crash_retrigger` has always stated: a
    component the observed fault does not touch is not a candidate. And
    with no blame of any kind the suspects still do not decide: reachability
    is the inference the OWNS tier was built to stop aiming by (control:
    the pre-2026-09-06 grade admitted the mega component off it)."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_candidate_grade,
    )
    ev = _aim_evidence(_RESIDUE, frozenset(),
                       suspects=frozenset({_CHAIN_CONSTRUCT}))
    assert crash_candidate_grade(ev, _aim_records(), _MEGA_NAMES) is BlameGrade.NONE
    bare = _aim_evidence(frozenset(), frozenset(),
                         suspects=frozenset({_CHAIN_CONSTRUCT}))
    assert crash_candidate_grade(bare, _aim_records(), _MEGA_NAMES) is BlameGrade.NONE
    assert _grade_until_2026_09_06(
        bare, _aim_records(), _MEGA_NAMES) is BlameGrade.OWNS


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
