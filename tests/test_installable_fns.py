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
