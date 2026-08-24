"""Pins for the writable COMPLEMENT of an escalated producer: the fns of the
same file that producer could install into a table it builds.

The defect this exists for spans two units — one inside `impl Error`, one a
free fn — which are never writable in the same prompt, so every intermediate
state scores identically and no per-unit fill can climb it (measured
2026-08-21: a past tree with the wiring but not the bodies scored 92 of 105,
the same 92, the same 13 failures)."""
from __future__ import annotations

from pipeline_core.domain.installable_fns import (
    _canon,
    _norm,
    fn_decl_shape,
    fn_pointer_shape,
    installable_fns,
    offer_producers,
    producer_bypasses,
    wrapper_producers,
)
from pipeline_core.domain.models import FieldRecord, FnLocator, TypeRecord

REL = "src/error.rs"

# The live tree's `ErrorVTable`, verbatim (tmp/anyhow/.skeleton-filled/stable).
VTABLE = TypeRecord(file=REL, kind="struct", name="ErrorVTable", fields=(
    FieldRecord(name="object_drop", ty="unsafe fn(Own<ErrorImpl>)"),
    FieldRecord(name="object_ref",
                ty="unsafe fn(Ref<ErrorImpl>) -> Ref<dyn StdError + Send + Sync + 'static>"),
    FieldRecord(name="object_downcast",
                ty="unsafe fn(Ref<ErrorImpl>, TypeId) -> Option<Ref<()>>"),
    FieldRecord(name="object_drop_rest",
                ty="unsafe fn(Own<ErrorImpl>, TypeId)"),
    FieldRecord(name="object_backtrace",
                ty="unsafe fn(Ref<ErrorImpl>) -> Option<&Backtrace>"),
))

PRODUCER = {
    "file": REL, "fn_name": "construct_from_std", "container": "impl Error",
    "sig_text": "", "body_text": "let vtable = Box::leak(Box::new(ErrorVTable {\n"
                                 "    object_drop: object_drop::<E>,\n}));",
}

FREE = [
    ("context_downcast",
     "unsafe fn context_downcast<C, E>(e: Ref<ErrorImpl>, target: TypeId) -> Option<Ref<()>>"),
    ("context_drop_rest",
     "unsafe fn context_drop_rest<C, E>(e: Own<ErrorImpl>, target: TypeId)"),
    ("context_backtrace",
     "unsafe fn context_backtrace<C>(e: Ref<ErrorImpl>) -> Option<&Backtrace>"),
    ("object_downcast",
     "unsafe fn object_downcast<E>(e: Ref<ErrorImpl>, target: TypeId) -> Option<Ref<()>>"),
    # SAFE, and upstream installs it into an `unsafe fn` field.
    ("no_backtrace", "fn no_backtrace(e: Ref<ErrorImpl>) -> Option<&Backtrace>"),
    # Same file, matches no field type: the shape test has to exclude it.
    ("vtable", "unsafe fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable"),
]
FREE_RECS = [{"file": REL, "fn_name": n, "container": "", "sig_text": s,
              "body_text": ""} for n, s in FREE]
ESCALATED = frozenset({FnLocator(REL, "construct_from_std")})


def test_a_declaration_matches_the_field_type_that_would_hold_it() -> None:
    """The only differences between the two are the generic list and the
    parameter NAMES; everything the type system cares about is identical."""
    assert (fn_decl_shape(
        "unsafe fn context_downcast<C, E>(e: Ref<ErrorImpl>, target: TypeId)"
        " -> Option<Ref<()>>")
        == fn_pointer_shape("unsafe fn(Ref<ErrorImpl>, TypeId) -> Option<Ref<()>>"))


def test_the_unreferenced_halves_of_the_split_defect_come_first() -> None:
    """Ranking, not filtering: `object_downcast` IS installable and is already
    wired by the one literal, so it sorts behind the `context_*` family whose
    only occurrence in the file is its own declaration."""
    got = installable_fns([PRODUCER, *FREE_RECS], [VTABLE], ESCALATED, REL,
                          reference_counts={"context_downcast": 1,
                                            "context_drop_rest": 1,
                                            "context_backtrace": 1,
                                            "no_backtrace": 2,
                                            "object_downcast": 8})
    names = [i.fn_name for i in got]
    assert names[:3] == ["context_backtrace", "context_downcast",
                         "context_drop_rest"]
    assert "object_downcast" in names and names.index("object_downcast") > 2
    assert "vtable" not in names            # matches no field type
    assert got[0].type_name == "ErrorVTable"
    assert got[1].field == "object_downcast"


def test_a_safe_fn_may_fill_an_unsafe_field_but_not_the_reverse() -> None:
    """Rust coerces safe -> unsafe, never the other way. Upstream `anyhow`
    installs the SAFE `no_backtrace` into an `unsafe fn` field, so an
    exact-equality rule drops a real answer."""
    got = {i.fn_name for i in installable_fns(
        [PRODUCER, *FREE_RECS], [VTABLE], ESCALATED, REL)}
    assert "no_backtrace" in got

    safe_field = TypeRecord(file=REL, kind="struct", name="ErrorVTable", fields=(
        FieldRecord(name="hook", ty="fn(Ref<ErrorImpl>) -> Option<&Backtrace>"),))
    unsafe_only = [{"file": REL, "fn_name": "context_backtrace", "container": "",
                    "sig_text": "unsafe fn context_backtrace<C>(e: Ref<ErrorImpl>)"
                                " -> Option<&Backtrace>", "body_text": ""}]
    assert installable_fns([PRODUCER, *unsafe_only], [safe_field],
                           ESCALATED, REL) == ()


def test_nothing_escalated_in_this_file_yields_nothing() -> None:
    """The gate that stops this widening every prompt on every iteration: it
    fires only where the loop has already MEASURED that the writable set is not
    paying."""
    assert installable_fns([PRODUCER, *FREE_RECS], [VTABLE],
                           frozenset({FnLocator("src/other.rs", "x")}), REL) == ()


def test_an_impl_member_is_never_offered_as_installable() -> None:
    """A table names free fns. A member is reachable only through its container
    and could not be stored in a plain fn-pointer field."""
    member = [{"file": REL, "fn_name": "context_downcast",
               "container": "impl ErrorImpl", "body_text": "",
               "sig_text": "unsafe fn context_downcast<C, E>(e: Ref<ErrorImpl>,"
                           " target: TypeId) -> Option<Ref<()>>"}]
    assert installable_fns([PRODUCER, *member], [VTABLE], ESCALATED, REL) == ()


def test_the_offer_reads_the_measured_set_not_the_batch() -> None:
    """A 12-of-N slice of accessors is not an empty measurement.

    `installable_fns` gates on whether an escalated producer of the file
    builds a fn-pointer table. The batch that happens to be in flight is
    a different quantity: run `508344-41b52769` iteration 2 dispatched
    `new`/`msg`/`from_boxed`/`context` after a fire that had already
    named `construct_from_std`, and offered nothing. The pre-fix
    reading — keys = batch only — is the first assertion."""
    accessors = frozenset({FnLocator(REL, "downcast"),
                           FnLocator(REL, "context")})
    assert installable_fns([PRODUCER, *FREE_RECS], [VTABLE], accessors,
                           REL) == ()
    assert offer_producers(frozenset(), accessors) == accessors
    measured = accessors | ESCALATED
    assert offer_producers(measured, accessors) == measured
    got = {i.fn_name for i in installable_fns(
        [PRODUCER, *FREE_RECS], [VTABLE],
        offer_producers(measured, accessors), REL)}
    assert "context_downcast" in got
    assert "no_backtrace" in got


def test_a_producer_that_builds_no_table_yields_nothing() -> None:
    """The relation is `constructs a struct literal`, not `is escalated`."""
    plain = {**PRODUCER, "body_text": "Self::construct_from_std(e, bt)"}
    assert installable_fns([plain, *FREE_RECS], [VTABLE], ESCALATED, REL) == ()


# What the EXTRACTOR hands the runtime call, verbatim from the live tree
# (`CliExtractor.enumerate_types` / `enumerate_fns` over
# tmp/anyhow/.skeleton-filled/stable, 2026-08-22): field types TOKEN-SPACED by
# the pretty-printer, declaration `sig_text` verbatim and starting at `fn`
# (no `unsafe`, no visibility, the `where` clause attached). The pins above
# paste the struct from SOURCE text, which is why they passed while the live
# call returned () on every file-first prompt of run 55484-84c991f1 — zero
# `installable_offered` events in five sweeps, the escalated constructors
# dispatched without the `context_*` bodies their tables name.
EXTRACTOR_VTABLE = TypeRecord(file=REL, kind="struct", name="ErrorVTable", fields=(
    FieldRecord(name="object_drop", ty="unsafe fn (Own < ErrorImpl >)"),
    FieldRecord(name="object_ref",
                ty="unsafe fn (Ref < ErrorImpl >) -> Ref < dyn StdError + Send + Sync + 'static >"),
    FieldRecord(name="object_downcast",
                ty="unsafe fn (Ref < ErrorImpl > , TypeId) -> Option < Ref < () > >"),
    FieldRecord(name="object_drop_rest", ty="unsafe fn (Own < ErrorImpl > , TypeId)"),
    FieldRecord(name="object_backtrace",
                ty="unsafe fn (Ref < ErrorImpl >) -> Option < & Backtrace >"),
))
EXTRACTOR_FREE = [
    ("context_downcast",
     "fn context_downcast<C, E>(e: Ref<ErrorImpl>, target: TypeId) -> Option<Ref<()>>"
     "\nwhere\n    C: 'static,\n    E: 'static,"),
    ("context_drop_rest",
     "fn context_drop_rest<C, E>(e: Own<ErrorImpl>, target: TypeId)"
     "\nwhere\n    C: 'static,\n    E: 'static,"),
    ("context_backtrace",
     "fn context_backtrace<C>(e: Ref<ErrorImpl>) -> Option<&Backtrace>\nwhere\n    C: 'static,"),
    ("object_ref",
     "fn object_ref<E>(e: Ref<ErrorImpl>) -> Ref<dyn StdError + Send + Sync + 'static>"
     "\nwhere\n    E: StdError + Send + Sync + 'static,"),
    ("no_backtrace", "fn no_backtrace(e: Ref<ErrorImpl>) -> Option<&Backtrace>"),
    ("vtable", "fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable"),
]


def test_the_extractors_token_spaced_field_types_match_verbatim_declarations() -> None:
    """The negative control is the first assertion: whitespace-collapse alone
    leaves the two printers' renderings unequal, which is the state the live
    tree was in for five sweeps. The comparison form has to drop the spaces
    the pretty-printer adds around punctuation on BOTH sides."""
    assert _norm("Own < ErrorImpl >") != _norm("Own<ErrorImpl>")
    assert _canon("Own < ErrorImpl >") == _canon("Own<ErrorImpl>")
    assert (_canon("Ref < dyn StdError + Send + Sync + 'static >")
            == _canon("Ref<dyn StdError + Send + Sync + 'static>"))
    recs = [{"file": REL, "fn_name": n, "container": "", "sig_text": s,
             "body_text": ""} for n, s in EXTRACTOR_FREE]
    got = {i.fn_name: i.field for i in installable_fns(
        [PRODUCER, *recs], [EXTRACTOR_VTABLE], ESCALATED, REL)}
    assert got == {
        "context_downcast": "object_downcast",
        "context_drop_rest": "object_drop_rest",
        "context_backtrace": "object_backtrace",
        "object_ref": "object_ref",
        "no_backtrace": "object_backtrace",
    }


# ---------------------------------------------------------------------------
# The closure's OTHER population: call sites that construct the wrapper the
# producer erases without invoking the producer (`producer_bypasses`).
# Bodies below are the LIVE tree's, verbatim shapes
# (tmp/anyhow/.skeleton-filled/stable, 2026-08-24): the census the review of
# THE PLAN demanded as the negative control — `new`, `From::from` and
# `construct` must NOT be re-admitted, and the extra hits must be zero.
# ---------------------------------------------------------------------------

WRAPPER_TY = TypeRecord(file=REL, kind="struct", name="ContextError", fields=())

CONTEXT_PRODUCER = {
    "file": REL, "fn_name": "construct_from_context", "container": "impl Error",
    "sig_text": "",
    "body_text": (
        "let vtable = Box::leak(Box::new(ErrorVTable {\n"
        "    object_drop: object_drop::<crate::error::ContextError<C, E>>,\n"
        "    object_downcast: context_downcast::<C, E>,\n"
        "    object_drop_rest: context_drop_rest::<C, E>,\n"
        "}));\n"
        "unsafe { Self::construct(crate::error::ContextError { context, error }"
        ", vtable, backtrace) }"),
}

ERROR_RS_CALLERS = [
    # THE bypass: run `40938-f4e8e94b`'s thinking named it, the write list
    # omitted it, the sweep moved nothing (`error.rs:297` on the live tree).
    {"file": REL, "fn_name": "context", "container": "impl Error",
     "sig_text": "", "body_text":
         "Self::new(crate::error::ContextError { context, error: self })"},
    # Generic path itself — constructs no wrapper literal. Re-admitting it
    # would re-buy the accessor slice the record convicted (199.21).
    {"file": REL, "fn_name": "new", "container": "impl Error",
     "sig_text": "", "body_text":
         "let bt = backtrace_if_absent!(&error);\n"
         "Self::construct_from_std(error, bt)"},
    {"file": REL, "fn_name": "from", "container": "impl<E> From<E> for Error",
     "sig_text": "", "body_text": "Self::new(error)"},
    # Takes the wrapper as a PARAMETER; constructs only ErrorImpl.
    {"file": REL, "fn_name": "construct", "container": "impl Error",
     "sig_text": "", "body_text":
         "let inner = Box::new(ErrorImpl { vtable, backtrace, _object: error });\n"
         "Error { inner: Own::new(inner) }"},
    # Calls the producer — the correct shape must never be re-listed.
    {"file": REL, "fn_name": "wrapped", "container": "impl Error",
     "sig_text": "", "body_text":
         "Self::construct_from_context(context, error, backtrace)"},
]

CTX = "src/context.rs"
CONTEXT_RS_CALLERS = [
    # Both `ext_context` impls route ContextError through `Error::new` — the
    # path `tests/test_context.rs::make_chain` actually takes, so the five
    # failing downcast pairs never even reach `Error::context` (measured
    # 2026-08-24, live tree `context.rs:24`/`:32`). `impl_self_generic` is the
    # extractor's AST-decided fact (True only where the impl's self type is
    # its own generic parameter); the live extractor emits True for exactly
    # this record and False for its concrete-self sibling below.
    {"file": CTX, "fn_name": "ext_context", "container": "impl<E> StdError for E",
     "impl_self_generic": True, "sig_text": "", "body_text":
         "crate::Error::new(crate::error::ContextError { context, error: self })"},
    {"file": CTX, "fn_name": "ext_context", "container": "impl StdError for Error",
     "sig_text": "", "body_text":
         "crate::Error::new(crate::error::ContextError { context, error: self })"},
    # Delegates to ext_context; constructs nothing itself.
    {"file": CTX, "fn_name": "context",
     "container": "impl<T, E> Context<T, E> for Result<T, E>",
     "sig_text": "", "body_text":
         "match self { Ok(v) => Ok(v), Err(e) => Err(e.ext_context(context)) }"},
]

ALL_RECORDS = [CONTEXT_PRODUCER, PRODUCER, *ERROR_RS_CALLERS,
               *CONTEXT_RS_CALLERS, *FREE_RECS]
ALL_TYPES = [VTABLE, WRAPPER_TY]
MEASURED = frozenset({FnLocator(REL, "construct_from_context"),
                      FnLocator(REL, "construct_from_std")})


def test_the_wrapper_relation_requires_a_typed_producer() -> None:
    """`construct_from_context` instantiates its table at `ContextError` — a
    crate nominal type — so it IS a typed producer of that wrapper.
    `construct_from_std` instantiates at the generic parameter `E`, which
    names every type and therefore none: the `type_production` discipline,
    and the type check the review demanded be real."""
    rels = wrapper_producers(ALL_RECORDS, ALL_TYPES, MEASURED)
    assert rels == (("ContextError", "construct_from_context", REL),)
    std_only = frozenset({FnLocator(REL, "construct_from_std")})
    assert wrapper_producers(ALL_RECORDS, ALL_TYPES, std_only) == ()


def test_the_census_flags_exactly_the_bypassing_call_site() -> None:
    """On the live error.rs shapes: `context` and nothing else. `new` and
    `From::from` construct no wrapper literal, `construct` builds ErrorImpl
    (not a wrapper any producer erases), and a body that CALLS the producer
    is the correct shape, not a bypass. Extra hits: zero.

    `context`'s self is the concrete `Error` — this crate's own already-erased
    type — so `erases_generic` is False: the instruction split must render the
    table form for it and never name the producer. The call-form offer line
    for exactly this record is what the model complied with over the compiler
    through three identical `E0277`s (run `57025-6ba652f2`, 17:45/17:55/18:05
    UTC — the pre-fix negative control, recorded live)."""
    got = producer_bypasses(ALL_RECORDS, ALL_TYPES, MEASURED, REL)
    assert [(b.fn_name, b.wrapper, b.producer, b.same_file, b.erases_generic)
            for b in got] == [
        ("context", "ContextError", "construct_from_context", True, False)]
    assert got[0].container == "impl Error"


def test_the_bypass_crosses_files_because_the_defect_does() -> None:
    """`make_chain` builds BOTH wraps through `context.rs::ext_context`, so a
    closure bound to the producer's file can never reach the defect's actual
    call path. Both impls are flagged, the producer's home file is named for
    the instruction line, and the delegating `Result::context` is not
    re-admitted (it constructs nothing).

    The two impls SPLIT on the erased inner type: the generic impl erases its
    own parameter (call form — the shape reply sha `96b99782…` executed
    correctly), the Error impl erases the crate's own type (table form — the
    shape upstream builds a chain vtable for, never a producer call)."""
    got = producer_bypasses(ALL_RECORDS, ALL_TYPES, MEASURED, CTX)
    assert [(b.fn_name, b.container, b.same_file, b.erases_generic)
            for b in got] == [
        ("ext_context", "impl StdError for Error", False, False),
        ("ext_context", "impl<E> StdError for E", False, True)]
    assert all(b.producer == "construct_from_context"
               and b.producer_file == REL for b in got)


def test_an_accessor_only_measurement_offers_no_bypass() -> None:
    """NEGATIVE CONTROL (199.21): no typed producer in the measured set — an
    accessor batch — must offer nothing, in either file."""
    accessors = frozenset({FnLocator(REL, "downcast"),
                           FnLocator(REL, "context")})
    assert producer_bypasses(ALL_RECORDS, ALL_TYPES, accessors, REL) == ()
    assert producer_bypasses(ALL_RECORDS, ALL_TYPES, accessors, CTX) == ()


def test_the_producer_never_convicts_itself() -> None:
    """`construct_from_context` constructs the wrapper in its own body and
    does not name itself — the table-builder exclusion is what keeps it out."""
    got = producer_bypasses(ALL_RECORDS, ALL_TYPES, MEASURED, REL)
    assert all(b.fn_name != "construct_from_context" for b in got)


def test_producer_name_absence_is_a_token_test() -> None:
    """A body calling `construct_from_context` must not read as calling a
    producer named `construct` by substring accident — and vice versa."""
    ty = TypeRecord(file=REL, kind="struct", name="Wrap", fields=())
    producer = {"file": REL, "fn_name": "construct", "container": "impl E",
                "sig_text": "", "body_text":
                    "let v = Box::leak(Box::new(ErrorVTable {\n"
                    "    object_drop: object_drop::<Wrap>,\n}));\n"
                    "Self::install(Wrap { x }, v)"}
    caller = {"file": REL, "fn_name": "maker", "container": "impl E",
              "sig_text": "", "body_text":
                  "Self::constructor(Wrap { x: 1 })"}
    got = producer_bypasses(
        [producer, caller], [VTABLE, ty],
        frozenset({FnLocator(REL, "construct")}), REL)
    assert [(b.fn_name, b.producer) for b in got] == [("maker", "construct")]
