"""Domain pins for what `dispatch_batch` still holds: the batch record, the
crash cohort's provenance, the claim-order map handed to the routes, the
standing-flat exclusion, and the producer facts the rank reads.

The order of the frontier, the batch itself and the pass accounting are
`dispatch_rank`'s, pinned in `tests/test_dispatch_rank.py`.
"""
from __future__ import annotations

from pipeline_core.domain.dispatch_batch import (
    CrashCohort,
    dispatch_ranks_of,
    drop_standing_flat,
)
from pipeline_core.domain.dispatch_rank import take
from pipeline_core.domain.models import FnLocator


def test_rendered_marks_a_mid_pass_batch_for_the_iteration_line():
    """The iteration line is the one place a live run's cadence is visible, and
    the census keys its reaction marker on the mid-pass text."""
    ordered = [f"u{i}" for i in range(30)]
    assert "mid-pass" in take(ordered, (), 12, cohort=None,
                              traversed=()).rendered
    assert "mid-pass" not in take(ordered, (), 0, cohort=None,
                                  traversed=()).rendered


def _k(file, fn):
    return FnLocator(file, fn)


def test_dispatch_ranks_of_is_the_order_it_is_given():
    """The engine's pool ranks are the driver's order, head highest; a key
    outside the order has no rank, and a repeat keeps its first slot."""
    a, b, c = _k("src/z.rs", "a"), _k("src/a.rs", "b"), _k("src/m.rs", "c")
    out = dispatch_ranks_of([a, b, c, a])
    assert out[a] > out[b] > out[c] > 0
    assert _k("src/a.rs", "absent") not in out
    assert dispatch_ranks_of([]) == {}


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
    batch = take([k for k, _ in dropped], (), 12, cohort=None, traversed=())
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



def test_the_crash_cohort_keeps_its_provenance_apart():
    """The split is by provenance, not by grade: a blamed frame, the report's
    other frames and the producer aims are SITED; a chain key no site names is
    the inference proper; a key that is both a chain member and a frame grades
    as the chain does (1, the pre-split rule) and is not chain-only."""
    drop = FnLocator("src/error.rs", "drop")
    caller = FnLocator("src/context.rs", "source")
    producer = FnLocator("src/error.rs", "object_drop")
    chain_only = FnLocator("src/error.rs", "construct")
    both = FnLocator("src/error.rs", "new")
    cohort = CrashCohort(blamed=frozenset({drop}),
                         sited=frozenset({drop, caller, producer, both}),
                         chain=frozenset({chain_only, both}))
    assert cohort.keys == frozenset({drop, caller, producer, both, chain_only})
    assert cohort.chain_only == frozenset({chain_only})
    assert cohort.grades == {drop: 0, caller: 2, producer: 2, both: 1,
                             chain_only: 1}
    assert CrashCohort().keys == frozenset() and CrashCohort().grades == {}
