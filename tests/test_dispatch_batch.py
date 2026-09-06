"""Domain pins for the TDD dispatch batch.

The batch exists because the sweep is the loop's only measurement of the tree
and it was being taken once every 3-4 hours — one or two readings per run, so
every sweep-boundary lever (batch ratchet, re-admission, crash retrigger,
plateau exit) got one or two chances to fire. See
`pipeline_core/domain/dispatch_batch.py` for the measurement.

Two invariants carry the weight here and each has its own case below: the batch
is a PREFIX of the ranked frontier (so capping changes when the loop looks, not
what it fills or in what order), and the cursor is a SET (so a frontier that
churns between iterations cannot re-serve its head and starve its tail).
"""
from __future__ import annotations

from pipeline_core.domain.dispatch_batch import (
    DEFAULT_MAX_DISPATCH,
    drop_standing_flat,
    promote_crash_evidence,
    take_batch,
)


def test_batch_is_a_prefix_of_the_ranking_and_does_not_complete_the_pass():
    """The cap withholds work without reordering it: `order_frontier` ranks
    leaf-first and the batch must take that ranking's head, or the cap would
    silently become a scheduling change."""
    ordered = [f"u{i}" for i in range(30)]
    b = take_batch(ordered, set(), 12)
    assert b.keys == tuple(ordered[:12])
    assert not b.completes_pass
    assert b.capped
    assert b.attempted_before == 0
    assert b.frontier_size == 30


def test_successive_batches_traverse_the_whole_frontier_then_close_the_pass():
    """Three batches of 12 cover 30 units, and only the LAST reports a
    completed pass — that flag is what lets the stall budget charge, so a
    mid-pass batch claiming it would restore the premature-exit bug."""
    ordered = [f"u{i}" for i in range(30)]
    attempted: set[str] = set()
    seen: list[str] = []
    completions: list[bool] = []
    for _ in range(3):
        b = take_batch(ordered, attempted, 12)
        seen.extend(b.keys)
        completions.append(b.completes_pass)
        attempted = set() if b.completes_pass else attempted | set(b.keys)
    assert seen == ordered            # every unit dispatched exactly once
    assert completions == [False, False, True]
    # The pass closed, so the cursor is empty and the next pass reopens whole.
    assert take_batch(ordered, attempted, 12).attempted_before == 0


def test_the_cursor_is_a_set_so_a_churning_frontier_cannot_starve_its_tail():
    """The frontier is rebuilt every iteration and shrinks as fills land. A
    POSITIONAL cursor would re-serve the head each time the list changed; the
    attempted-set survives the churn and the tail still gets its turn."""
    first = [f"u{i}" for i in range(20)]
    b1 = take_batch(first, set(), 5)
    attempted = set(b1.keys)
    # Two of the dispatched units filled and left the frontier; one new unit
    # arrived from the freshly attributed failures.
    second = [k for k in first if k not in {"u0", "u1"}] + ["new"]
    b2 = take_batch(second, attempted, 5)
    assert not set(b2.keys) & attempted        # nothing re-served
    assert b2.keys == ("u5", "u6", "u7", "u8", "u9")
    # The unit that arrived mid-pass is still reachable before the pass closes.
    rest: set[str] = set(attempted) | set(b2.keys)
    later: list[str] = []
    for _ in range(4):
        b = take_batch(second, rest, 5)
        later.extend(b.keys)
        if b.completes_pass:
            break
        rest |= set(b.keys)
    assert "new" in later


def test_a_frontier_under_the_cap_completes_in_one_batch():
    """A small crate must not pay for the machinery: when the whole pending
    set fits, the batch is the frontier and the pass closes immediately —
    which is exactly the uncapped loop's behaviour."""
    b = take_batch(["a", "b", "c"], set(), DEFAULT_MAX_DISPATCH)
    assert b.keys == ("a", "b", "c")
    assert b.completes_pass
    assert not b.capped


def test_cap_of_zero_disables_batching():
    """`PHASE_C_TDD_MAX_DISPATCH=0` is the escape hatch back to pre-cap
    behaviour, and it must dispatch the WHOLE frontier in one completed pass —
    otherwise the escape hatch would still charge the stall differently."""
    ordered = [f"u{i}" for i in range(50)]
    b = take_batch(ordered, set(), 0)
    assert b.keys == tuple(ordered)
    assert b.completes_pass


def test_a_frontier_that_shrank_below_the_cursor_reopens_the_pass():
    """Every pending unit filled and left. There is nothing left to traverse,
    so the pass has closed: reopen on the frontier as it stands NOW rather
    than returning an empty batch forever (which would dispatch nothing and
    look like a plateau)."""
    b = take_batch(["a", "b"], {"a", "b", "gone"}, 12)
    assert b.keys == ("a", "b")
    assert b.completes_pass
    assert b.attempted_before == 0


def test_a_growing_frontier_never_closes_a_pass_without_a_cohort():
    """THE NEGATIVE CONTROL for the cohort, and the whole of run
    `1692702-94e6163a`'s failure to terminate.

    Its frontier grew 35 -> 57 -> 63 while the cursor advanced 12 -> 21 -> 36,
    because futility escalation adds a crashed test's producers and crash
    evidence adds its subjects. With closure judged against the frontier AS IT
    STANDS, the pending set never empties: `completes_pass` is False forever,
    `_traversals` stays 0, and BOTH terminators denominated in traversals — the
    flat-score guard and the plateau budget — are disabled by arithmetic."""
    sizes = [35, 57, 63, 69, 75]
    attempted: set[str] = set()
    completions: list[bool] = []
    for n in sizes:
        ordered = [f"u{i}" for i in range(n)]
        b = take_batch(ordered, attempted, 12)
        completions.append(b.completes_pass)
        attempted = set() if b.completes_pass else attempted | set(b.keys)
    assert completions == [False] * len(sizes)


def test_the_cohort_closes_the_pass_even_while_the_frontier_grows():
    """The same growth, judged against the cohort the pass OPENED over: 35
    units at 12 a sweep closes on the third batch, and the units that arrived
    while it ran belong to the NEXT cohort rather than extending this one."""
    cohort = [f"u{i}" for i in range(35)]
    attempted: set[str] = set()
    completions: list[bool] = []
    for n in (35, 57, 63):
        ordered = [f"u{i}" for i in range(n)]
        b = take_batch(ordered, attempted, 12, cohort=cohort)
        completions.append(b.completes_pass)
        attempted = set() if b.completes_pass else attempted | set(b.keys)
    assert completions == [False, False, True]


def test_a_cohort_unit_that_left_the_frontier_does_not_block_closure():
    """A cohort member that filled and left can never be attempted again, so
    waiting for it would be the same unsatisfiable condition in a smaller
    disguise: closure asks about cohort units STILL in the frontier."""
    cohort = ["a", "b", "c"]
    b = take_batch(["a", "c"], {"a"}, 12, cohort=cohort)
    assert b.keys == ("c",)
    assert b.completes_pass


def test_cohort_none_is_exactly_the_pre_cohort_behaviour():
    """The default must not change for a caller that does not track passes —
    the cohort is then the frontier as it stands, which is what every existing
    case above asserts."""
    ordered = [f"u{i}" for i in range(30)]
    for attempted in (set(), {"u0", "u1"}, set(ordered[:29])):
        a = take_batch(ordered, attempted, 12)
        b = take_batch(ordered, attempted, 12, cohort=ordered)
        assert (a.keys, a.completes_pass) == (b.keys, b.completes_pass)


def test_crash_evidence_is_promoted_ahead_of_the_static_ranking():
    """The defect (run `1692702-94e6163a`): the evidence pack granted eligibility and the ranking
    decided WHEN, so the five fns a double drop named waited for their
    alphabetical turn. Promotion puts every graded unit ahead of the frontier,
    best grade first — an observed frame above an inference above a synthesized
    subject — and leaves the rest in the order `order_frontier` chose."""
    ordered = [(f"u{i}", {"t"}) for i in range(6)]
    out = promote_crash_evidence(ordered, {"u4": 1, "u5": 0, "u2": 2})
    assert [k for k, _ in out] == ["u5", "u4", "u2", "u0", "u1", "u3"]


def test_promotion_is_stable_so_one_fault_s_family_dispatches_together():
    """Within a grade the ranking is untouched, so the five subjects of one
    fault occupy consecutive slots of the batch that found them instead of
    arriving one iteration apart in name order — which is how
    `object_drop_front`, last of its family on the `(file, fn)` tiebreak, went
    undispatched while `object_drop` (correct on disk) took the slot."""
    fam = ["context_chain_drop_rest", "context_drop_rest", "drop",
           "object_drop", "object_drop_front"]
    # The frontier as run `1692702-94e6163a` had it: the family ranked behind
    # the units attribution had already collected, on a frontier well over the
    # cap of 12.
    ordered = [(f"u{i}", {"t"}) for i in range(20)] + [(k, {"t"}) for k in fam]
    grades = dict.fromkeys(fam, 1)
    # NEGATIVE CONTROL — the pre-fix ordering. Every one of the five is
    # eligible and none of them is dispatched: iteration 2 of that run took 12
    # of 57 and logged `0 skipped — free fns` while four of the five are free
    # fns.
    before = take_batch([k for k, _ in ordered], set(), 12).keys
    assert not set(before) & set(fam)
    out = promote_crash_evidence(ordered, grades)
    assert [k for k, _ in out][:5] == fam
    assert take_batch([k for k, _ in out], set(), 12).keys[:5] == tuple(fam)


def test_promotion_without_evidence_changes_nothing():
    """No crash this sweep must mean no reordering at all: the static ranking
    is correct for an attribution-built frontier and this function must not
    become a second opinion about it."""
    ordered = [(f"u{i}", {"t"}) for i in range(5)]
    assert promote_crash_evidence(ordered, {}) == ordered
    assert promote_crash_evidence(ordered, {"absent": 0}) == ordered


def test_rendered_marks_a_mid_pass_batch_for_the_iteration_line():
    """The iteration line is the one place a live run's cadence is visible, and
    the census keys its P7 reaction marker on the mid-pass text."""
    ordered = [f"u{i}" for i in range(30)]
    assert "mid-pass" in take_batch(ordered, set(), 12).rendered
    assert "mid-pass" not in take_batch(ordered, set(), 0).rendered


# --- promote_producers: the constructors before the delegators ---------------

def _k(file, fn):
    from pipeline_core.domain.models import FnLocator
    return FnLocator(file, fn)


def test_promote_producers_puts_builders_first_in_the_escalated_cohort():
    from pipeline_core.domain.dispatch_batch import promote_producers
    # order_frontier's name-frequency ranking put the delegators first.
    msg, frm, new = _k("e.rs", "msg"), _k("e.rs", "from"), _k("e.rs", "new")
    cfa, cfs, construct = (_k("e.rs", "construct_from_adhoc"),
                           _k("e.rs", "construct_from_std"),
                           _k("e.rs", "construct"))
    ordered = [(msg, {"t"}), (frm, {"t"}), (new, {"t"}),
               (cfa, {"t"}), (cfs, {"t"}), (construct, {"t"})]
    escalated = frozenset({msg, frm, new, cfa, cfs, construct})
    builders = frozenset({cfa, cfs, construct})
    callees = frozenset({cfa, cfs, construct})  # msg/from/new call them
    out = [k for k, _ in promote_producers(ordered, escalated, builders, callees)]
    # builders lead, delegators follow — same set, reordered.
    assert set(out[:3]) == {cfa, cfs, construct}
    assert set(out[3:]) == {msg, frm, new}


def test_promote_producers_leaves_non_escalated_units_in_place():
    from pipeline_core.domain.dispatch_batch import promote_producers
    crash = _k("x.rs", "crash_fn")      # a non-escalated, higher-ranked unit
    deleg = _k("e.rs", "msg")
    build = _k("e.rs", "construct_from_adhoc")
    ordered = [(crash, {"t"}), (deleg, {"t"}), (build, {"t"})]
    escalated = frozenset({deleg, build})
    out = [k for k, _ in promote_producers(
        ordered, escalated, frozenset({build}), frozenset({build}))]
    # crash stays at its slot (position 0); only the two escalated slots swap.
    assert out[0] == crash
    assert out[1] == build and out[2] == deleg


def test_promote_producers_noop_without_escalation():
    from pipeline_core.domain.dispatch_batch import promote_producers
    ordered = [(_k("a.rs", "f"), {"t"}), (_k("a.rs", "g"), {"t"})]
    assert promote_producers(
        ordered, frozenset(), frozenset(), frozenset()) == ordered


def test_promote_by_blocker_counts_puts_the_hottest_blocker_first():
    """Whole-list reorder, not an escalated-subsequence shuffle: take_batch
    takes a prefix, so a producer ranked last never makes the cap=12 slice."""
    from pipeline_core.domain.dispatch_batch import promote_by_blocker_counts
    fmt = _k("src/wrapper.rs", "fmt")
    construct = _k("src/error.rs", "construct")
    from_fn = _k("src/error.rs", "from")
    ordered = [(fmt, {"t"}), (from_fn, {"t"}), (construct, {"t"})]
    counts = {construct: 27, from_fn: 3, fmt: 1}
    out = [k for k, _ in promote_by_blocker_counts(ordered, counts)]
    assert out[0] == construct
    assert promote_by_blocker_counts(ordered, {}) == ordered


def test_overlay_crash_on_ranks_puts_blamed_frames_above_the_histogram():
    """The engine re-sorts on dispatch_ranks. Histogram-only ranks would
    undo crash-first the same way leaf-first undid constructor-first."""
    from pipeline_core.domain.dispatch_batch import overlay_crash_on_ranks
    construct = _k("src/error.rs", "construct")
    fmt = _k("src/wrapper.rs", "fmt")
    blamed = _k("src/error.rs", "object_drop")
    ranks = {construct: 27, fmt: 14}
    out = overlay_crash_on_ranks(ranks, {blamed: 0, fmt: 2})
    assert out[blamed] > out[construct]
    assert out[fmt] > out[construct]
    assert out[construct] == 27
    assert overlay_crash_on_ranks(ranks, {}) == ranks


def test_promote_product_ahead_lifts_producers_over_new_accessors():
    """The attribution-shift re-buy: order_frontier ranks the new accessor
    first; persist keeps the producer in the cohort; without this lift
    cap=1 dispatches the accessor and parks the producer."""
    from pipeline_core.domain.dispatch_batch import (
        promote_crash_evidence,
        promote_producers,
        promote_product_ahead,
    )
    acc = _k("a.rs", "other_accessor")
    deleg = _k("e.rs", "msg")
    build = _k("e.rs", "construct_from_adhoc")
    crash = _k("x.rs", "crash_fn")
    ordered = [(acc, {"t"}), (deleg, {"t"}), (crash, {"t"}), (build, {"t"})]
    product = frozenset({deleg, build})
    ahead = promote_product_ahead(ordered, product)
    assert [k for k, _ in ahead] == [deleg, build, acc, crash]
    # Constructors first inside the product block; crash still wins after.
    mid = promote_producers(
        ahead, product, frozenset({build}), frozenset({build}))
    assert [k for k, _ in mid] == [build, deleg, acc, crash]
    final = promote_crash_evidence(mid, {crash: 0})
    assert [k for k, _ in final] == [crash, build, deleg, acc]
    assert promote_product_ahead(ordered, frozenset()) == ordered
    # Escalated keys that are not in `ordered` leave it untouched.
    assert promote_producers(
        ordered, frozenset({_k("z.rs", "absent")}),
        frozenset(), frozenset()) == list(ordered)
    assert promote_product_ahead(
        ordered, frozenset({_k("z.rs", "absent")})) == list(ordered)


def _types():
    from pipeline_core.domain.models import FieldRecord, TypeRecord
    F = FieldRecord
    return [
        TypeRecord(kind="struct", name="ErrorVTable",
                   fields=(F(name="object_drop", ty="unsafe fn(Own<ErrorImpl>)"),
                           F(name="object_downcast",
                             ty="unsafe fn(Ref<ErrorImpl>, TypeId) -> Option<Ref<()>>"))),
        TypeRecord(kind="struct", name="ErrorImpl", generics="<E = ()>",
                   fields=(F(name="vtable", ty="&'static ErrorVTable"),
                           F(name="backtrace", ty="Option<Backtrace>"),
                           F(name="value", ty="E"))),
        TypeRecord(kind="struct", name="Error",
                   fields=(F(name="inner", ty="Own<ErrorImpl>"),
                           F(name="ptr", ty="PhantomData<Error>"))),
        TypeRecord(kind="struct", name="ContextError", generics="<C, E>",
                   fields=(F(name="context", ty="C"), F(name="error", ty="E"))),
        # Tuple structs: the extractor names their fields 0, 1, …
        TypeRecord(kind="struct", name="MessageError", generics="<M>",
                   fields=(F(name="0", ty="M", is_pub=True),)),
        TypeRecord(kind="struct", name="DisplayError", generics="<M>",
                   fields=(F(name="0", ty="M", is_pub=True),)),
        TypeRecord(kind="struct", name="BoxedError",
                   fields=(F(name="0", ty="Box<dyn StdError + Send + Sync>"),)),
        TypeRecord(kind="struct", name="Own", generics="<T>",
                   fields=(F(name="ptr", ty="NonNull<T>"),)),
    ]


def test_producer_dispatch_ranks_from_records():
    from pipeline_core.domain.installable_fns import producer_dispatch_ranks
    from pipeline_core.domain.models import FnLocator
    recs = [
        # The Defect-1 wrong body: delegates to the std producer, builds no
        # table — but CONSTRUCTS the wrapper (a tuple-struct call), which is
        # the constructor role.
        {"file": "e.rs", "fn_name": "construct_from_adhoc", "container": "impl Error",
         "body_text": "Self::construct_from_std(crate::wrapper::MessageError(message), backtrace)"},
        {"file": "e.rs", "fn_name": "construct_from_std", "container": "impl Error",
         "body_text": ("let vtable = Box::leak(Box::new(ErrorVTable { object_drop: object_drop::<E>, "
                       "object_downcast: object_downcast::<E> })); unsafe { Self::construct(error, vtable, backtrace) }")},
        # The funnel: a builder because it constructs the payload literal, not
        # because of its name.
        {"file": "e.rs", "fn_name": "construct", "container": "impl Error",
         "body_text": ("let err_impl = ErrorImpl { vtable, backtrace, value: error }; "
                       "let boxed = Box::new(err_impl); let inner = Own::new(boxed).cast::<ErrorImpl>(); "
                       "Self { inner, ptr: PhantomData }")},
        {"file": "e.rs", "fn_name": "msg", "container": "impl Error",
         "body_text": "Self::construct_from_adhoc(message, backtrace!())"},
        # `Box::new(` and `String::from(` appear in bodies; the crate's `new`
        # and `from` are NOT thereby called.
        {"file": "e.rs", "fn_name": "new", "container": "impl Error",
         "body_text": "let bt = backtrace_if_absent!(&error); Self::construct_from_std(error, bt)"},
        {"file": "e.rs", "fn_name": "from", "container": "impl<E> From<E> for Error",
         "body_text": "let s = alloc::string::String::from(x); Self::construct_from_std(error, bt)"},
        {"file": "l.rs", "fn_name": "format_err", "container": "",
         "body_text": "crate::Error::msg(s)"},
    ]
    esc = frozenset(FnLocator(r["file"], r["fn_name"]) for r in recs)
    builders, callees = producer_dispatch_ranks(esc, recs, _types())
    assert builders == {FnLocator("e.rs", "construct_from_adhoc"),
                        FnLocator("e.rs", "construct_from_std"),
                        FnLocator("e.rs", "construct")}
    # Resolved calls: Self::construct_from_std / Self::construct /
    # Self::construct_from_adhoc / crate::Error::msg.
    assert callees == {FnLocator("e.rs", "construct_from_std"),
                       FnLocator("e.rs", "construct"),
                       FnLocator("e.rs", "construct_from_adhoc"),
                       FnLocator("e.rs", "msg")}
    assert FnLocator("e.rs", "new") not in callees      # `Box::new(` is not it
    assert FnLocator("e.rs", "from") not in callees     # `String::from(` is not it


def test_calls_unit_resolves_on_call_shape_not_token_presence():
    from pipeline_core.domain.installable_fns import calls_unit
    kw = {"callee_self_types": frozenset({"Error"}), "callee_is_free": False,
          "callee_is_method": True}
    assert calls_unit("crate::Error::msg(s)", "", "msg", **kw)
    assert calls_unit("Self::msg(s)", "Error", "msg", **kw)
    assert not calls_unit("Self::msg(s)", "Other", "msg", **kw)
    assert calls_unit("e.msg(s)", "", "msg", **kw)
    assert calls_unit("Self::msg::<E>(s)", "Error", "msg", **kw)
    assert not calls_unit("Box::new(x)", "Error", "new", **kw)
    assert not calls_unit("let msg = 1; msg + 1", "", "msg", **kw)
    assert not calls_unit("fn msg(x: u8) {}", "", "msg", callee_self_types=frozenset(),
                          callee_is_free=True, callee_is_method=False)
    assert calls_unit("render(a, b)", "", "render", callee_self_types=frozenset(),
                      callee_is_free=True, callee_is_method=False)
    assert not calls_unit("render(a, b)", "", "render", callee_self_types=frozenset(),
                          callee_is_free=False, callee_is_method=True)


# The stable cluster's escalated cohort as run `95878-aaf5bf1d` iter 1 ordered
# it (`order_frontier` on the baked leaf counts; the `attempted` batch on the
# floor was the first twelve). Bodies are the live tree's, abbreviated to the
# construction and call facts the ranking reads.
_COHORT_ORDER = [
    ("src/context.rs", "context"), ("src/error.rs", "context"),
    ("src/error.rs", "msg"), ("src/error.rs", "new"), ("src/kind.rs", "new"),
    ("src/error.rs", "downcast"), ("src/error.rs", "from"),
    ("src/lib.rs", "must_use"), ("src/error.rs", "from_boxed"),
    ("src/context.rs", "with_context"), ("src/ensure.rs", "__dispatch_ensure"),
    ("src/error.rs", "deref"), ("src/lib.rs", "format_err"),
    ("src/context.rs", "ext_context"), ("src/error.rs", "deref_mut"),
    ("src/ensure.rs", "render"), ("src/error.rs", "construct"),
    ("src/error.rs", "construct_from_adhoc"), ("src/error.rs", "construct_from_boxed"),
    ("src/error.rs", "construct_from_context"), ("src/error.rs", "construct_from_display"),
    ("src/error.rs", "construct_from_std"), ("src/lib.rs", "Ok"),
    ("src/error.rs", "error"), ("src/error.rs", "is"),
    ("src/error.rs", "downcast_ref"), ("src/error.rs", "erase"),
]
_NOT_ESCALATED = {("src/lib.rs", "Ok"), ("src/error.rs", "error"),
                  ("src/error.rs", "is"), ("src/error.rs", "downcast_ref")}
def _table(t: str) -> str:
    return ("let vtable = Box::leak(Box::new(ErrorVTable { object_drop: object_drop::<T>, "
            "object_downcast: object_downcast::<T> }));").replace("T>", t + ">")
_COHORT_RECS = [
    ("src/context.rs", "context", "impl<T, E> Context<T, E> for Result<T, E>",
     "match self { Ok(v) => Ok(v), Err(e) => Err(e.ext_context(context)) }"),
    ("src/context.rs", "context", "impl<T> Context<T, Infallible> for Option<T>",
     "match self { Some(v) => Ok(v), None => Err(crate::Error::construct_from_display(context, backtrace!())) }"),
    ("src/error.rs", "context", "impl Error",
     _table("ContextError<C, Error>") + " unsafe { Self::construct(ContextError { context, error: self }, vtable, backtrace) }"),
    ("src/error.rs", "msg", "impl Error", "Self::construct_from_adhoc(message, backtrace!())"),
    ("src/error.rs", "new", "impl Error", "let bt = backtrace_if_absent!(&error); Self::construct_from_std(error, bt)"),
    ("src/kind.rs", "new", "impl Adhoc", "crate::Error::msg(message)"),
    ("src/kind.rs", "new", "impl Trait", "error.into()"),
    ("src/kind.rs", "new", "impl Boxed", "crate::Error::from_boxed(error)"),
    ("src/error.rs", "downcast", "impl Error", "let target = TypeId::of::<E>(); unsafe { let inner = self.inner.by_mut(); (vtable(inner.ptr).object_downcast)(inner.by_ref(), target) }"),
    ("src/error.rs", "from", "impl<E> From<E> for Error", "let bt = backtrace_if_absent!(&error); Self::construct_from_std(error, bt)"),
    ("src/lib.rs", "must_use", "", "error"),
    ("src/error.rs", "from_boxed", "impl Error", "let bt = backtrace_if_absent!(&*boxed_error); Self::construct_from_boxed(boxed_error, bt)"),
    ("src/context.rs", "with_context", "impl<T, E> Context<T, E> for Result<T, E>",
     "match self { Ok(v) => Ok(v), Err(e) => Err(e.ext_context(context())) }"),
    ("src/ensure.rs", "__dispatch_ensure", "impl<A, B> BothDebug for (A, B)",
     "let mut sb = SmallBuf { buf: [0; 128], pos: 0 }; let owned = alloc::string::String::from(s); crate::Error::msg(owned)"),
    ("src/ensure.rs", "__dispatch_ensure", "impl<A, B> NotBothDebug for &(A, B)", "crate::Error::msg(msg)"),
    ("src/error.rs", "deref", "impl Deref for Error", "unsafe { ErrorImpl::error(self.inner.by_ref()) }"),
    ("src/lib.rs", "format_err", "", "if let Some(s) = args.as_str() { crate::Error::msg(s) } else { crate::Error::msg(s) }"),
    ("src/context.rs", "ext_context", "impl<E> StdError for E", "let bt = backtrace_if_absent!(self); crate::Error::construct_from_context(context, self, bt)"),
    ("src/context.rs", "ext_context", "impl StdError for Error", "self.context(context)"),
    ("src/error.rs", "deref_mut", "impl DerefMut for Error", "unsafe { ErrorImpl::error_mut(self.inner.by_mut()) }"),
    ("src/ensure.rs", "render", "", "let mut buf = Buf::new(); let owned = alloc::string::String::from(s); crate::Error::msg(owned)"),
    ("src/error.rs", "construct", "impl Error",
     "let err_impl = ErrorImpl { vtable, backtrace, value: error }; let boxed = Box::new(err_impl); let inner = Own::new(boxed).cast::<ErrorImpl>(); Self { inner, ptr: PhantomData }"),
    ("src/error.rs", "construct_from_adhoc", "impl Error", "Self::construct_from_std(crate::wrapper::MessageError(message), backtrace)"),
    ("src/error.rs", "construct_from_boxed", "impl Error",
     _table("BoxedError") + " unsafe { Self::construct(crate::error::BoxedError(error), vtable, backtrace) }"),
    ("src/error.rs", "construct_from_context", "impl Error",
     _table("ContextError<C, E>") + " unsafe { Self::construct(ContextError { context, error }, vtable, backtrace) }"),
    ("src/error.rs", "construct_from_display", "impl Error", "Self::construct_from_std(crate::wrapper::DisplayError(message), backtrace)"),
    ("src/error.rs", "construct_from_std", "impl Error", _table("E") + " unsafe { Self::construct(error, vtable, backtrace) }"),
    ("src/lib.rs", "Ok", "", "Result::Ok(value)"),
    ("src/error.rs", "error", "impl ErrorImpl", "unsafe { &*(vtable(this.ptr).object_ref)(this).ptr }"),
    ("src/error.rs", "is", "impl Error", "self.downcast_ref::<E>().is_some()"),
    ("src/error.rs", "downcast_ref", "impl Error", "unsafe { let addr = (vtable(self.inner.ptr).object_downcast)(self.inner.by_ref(), target)?; Some(&*addr.cast::<E>().ptr.as_ptr()) }"),
    ("src/error.rs", "erase", "impl ErrorImpl", "Ref::new(self).cast::<ErrorImpl>()"),
]
_SIX = {("src/error.rs", n) for n in (
    "construct", "construct_from_adhoc", "construct_from_boxed",
    "construct_from_context", "construct_from_display", "construct_from_std")}


def test_recorded_95878_cohort_dispatches_the_constructors_in_batch_one():
    from pipeline_core.domain.dispatch_batch import promote_producers
    from pipeline_core.domain.installable_fns import producer_dispatch_ranks
    from pipeline_core.domain.models import FnLocator
    recs = [{"file": f, "fn_name": n, "container": c, "body_text": b}
            for f, n, c, b in _COHORT_RECS]
    ordered = [(FnLocator(f, n), {"t"}) for f, n in _COHORT_ORDER]
    esc = frozenset(FnLocator(f, n) for f, n in _COHORT_ORDER
                    if (f, n) not in _NOT_ESCALATED)
    builders, callees = producer_dispatch_ranks(esc, recs, _types())
    # The constructor ROLE — every unit that constructs a crate type — and
    # nothing else: the six `construct*` plus `Error::context` (its own table).
    assert {(k.file, k.fn_name) for k in builders} == _SIX | {("src/error.rs", "context")}
    # The token-pollution control: `Box::new(` / `String::from(` in the bodies
    # do not make the crate's `new` / `from` callees; the resolved ones are.
    assert FnLocator("src/error.rs", "new") not in callees
    assert FnLocator("src/kind.rs", "new") not in callees
    assert FnLocator("src/error.rs", "from") not in callees
    assert {FnLocator("src/error.rs", "msg"), FnLocator("src/error.rs", "from_boxed"),
            FnLocator("src/context.rs", "ext_context"),
            FnLocator("src/error.rs", "construct_from_display")} <= callees  # noqa: SIM300
    after = [k for k, _ in promote_producers(ordered, esc, builders, callees)]
    first = {(k.file, k.fn_name) for k in after[:12]}
    assert first >= _SIX, sorted(first)
    # NEGATIVE CONTROL: the recorded order (what the floor's `attempted` batch
    # was) carried none of the six in its first twelve.
    before = {(k.file, k.fn_name) for k, _ in ordered[:12]}
    assert not (_SIX & before)
    # Non-escalated units keep their slots; the ensure pair (`render`, its
    # intended caller `__dispatch_ensure`) moves to the NEXT batch together,
    # where one ensure.rs file-first prompt carries both.
    for i, (k, _) in enumerate(ordered):
        if (k.file, k.fn_name) in _NOT_ESCALATED:
            assert after[i] == k
    second = {(k.file, k.fn_name) for k in after[12:24]}
    assert second >= {("src/ensure.rs", "render"), ("src/ensure.rs", "__dispatch_ensure")}


def test_drop_standing_flat_removes_flats_and_keeps_exempt() -> None:
    """Cap 12 over a 30-unit frontier whose last 10 are flat still spends
    those slots unless the flats leave the list. Crash-blamed flats stay."""
    ordered = [(f"u{i}", i) for i in range(30)]
    flat = {f"u{i}" for i in range(20, 30)}
    dropped = drop_standing_flat(ordered, flat)
    assert [k for k, _ in dropped] == [f"u{i}" for i in range(20)]
    kept = drop_standing_flat(ordered, flat, exempt={"u29"})
    assert "u29" in {k for k, _ in kept}
    assert drop_standing_flat(ordered, set()) == ordered
    batch = take_batch([k for k, _ in dropped], set(), 12)
    assert batch.keys == tuple(f"u{i}" for i in range(12))
    assert "u20" not in batch.keys


def test_a_still_hollow_body_is_never_excluded_by_a_standing_grade() -> None:
    """A stub's ledger grade is a claim about bytes that are not the body
    the batch would write; first-fill ranks live-stub producers first, and
    the exclusion must not undo that. The caller passes the stub inventory
    as `exempt`, exactly as it passes crash-blamed units."""
    ordered = [(f"u{i}", i) for i in range(6)]
    flat = {"u1", "u4"}
    stubs = {"u4"}
    kept = [k for k, _ in drop_standing_flat(ordered, flat, exempt=stubs)]
    assert "u4" in kept and "u1" not in kept
    # Control: without the stub exemption the hollow constructor is gone.
    assert "u4" not in {k for k, _ in drop_standing_flat(ordered, flat)}
