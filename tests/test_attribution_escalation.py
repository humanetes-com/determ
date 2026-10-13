"""Pins for futility-triggered attribution escalation: when every
fn attributed to a failing test has been rewritten to the same dead end, the
frontier escalates to producers of the exercised type."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    container_type_name,
    escalate_frontier_to_producers,
    producer_escalation,
    producer_scan,
    producers_for_types,
)
from pipeline_core.domain.models import FnLocator, TypeRecord

RECS = [
    {"file": "src/chain.rs", "fn_name": "len",
     "container": "impl ExactSizeIterator for Chain<'_>",
     "sig_text": "fn len(&self) -> usize", "body_text": "..."},
    {"file": "src/error.rs", "fn_name": "chain",
     "container": "impl Error",
     "sig_text": "pub fn chain(&self) -> Chain", "body_text": "Chain::new(...)"},
    {"file": "src/chain.rs", "fn_name": "new",
     "container": "impl<'a> Chain<'a>",
     "sig_text": "pub fn new(head) -> Self", "body_text": "Chain { state }"},
    {"file": "src/fmt.rs", "fn_name": "fmt",
     "container": "impl Debug for Error",
     "sig_text": "fn fmt(&self, f) -> fmt::Result", "body_text": "write!(f)"},
]


# The crate's own declarations — the escalation subject must be one of these
# `Chain` is a real struct; `Context` is a trait and `Result` an
# alias, and both used to arrive as subjects.
TYPES = [
    TypeRecord(kind="struct", name="Chain"),
    TypeRecord(kind="struct", name="Error"),
    TypeRecord(kind="trait", name="Context"),
    TypeRecord(kind="type", name="Result"),
]


def _deps(types=None):
    class _S:
        def enumerate_fns(self, _sd):
            return RECS

        def enumerate_types(self, _sd):
            return TYPES if types is None else types
    return AttributionDeps(
        structure=_S(), classify=lambda *a, **k: {},
        locate_fn=lambda *a, **k: None, run_check_diag=lambda *a, **k: (0, "", []),
        derive_check_only_cmd=lambda c: c, is_gen_tests_file=lambda n: False,
        log=lambda m: None,
    )


def test_container_type_name_shapes():
    assert container_type_name("impl ExactSizeIterator for Chain<'_>") == "Chain"
    assert container_type_name("impl<'a> Chain<'a>") == "Chain"
    assert container_type_name("impl Debug for Quoted<C>") == "Quoted"
    assert container_type_name("") == ""


def test_producers_found_by_return_and_construction():
    got = producers_for_types(RECS, {"Chain"},
                              exclude={("src/chain.rs", "len")})
    assert ("src/error.rs", "chain") in got     # returns Chain
    assert ("src/chain.rs", "new") in got        # constructs Chain { … }
    assert ("src/fmt.rs", "fmt") not in got      # unrelated


def test_escalation_targets_producers_not_accessors():
    extra = escalate_frontier_to_producers(
        _deps(), Path("src"), "test_len",
        futile_units={("src/chain.rs", "len")})
    assert ("src/error.rs", "chain") in extra
    assert ("src/chain.rs", "new") in extra
    assert ("src/chain.rs", "len") not in extra   # never re-blames the accessor
    assert all(tests == {"test_len"} for tests in extra.values())


def test_escalation_failsafe_on_missing_records():
    class _Broken:
        def enumerate_fns(self, _sd):
            raise RuntimeError("no extractor")

        def enumerate_types(self, _sd):
            raise RuntimeError("no extractor")
    deps = _deps()
    object.__setattr__(deps, "structure", _Broken())
    assert escalate_frontier_to_producers(
        deps, Path("src"), "t", {("f", "g")}) == {}


def test_escalation_subject_must_be_a_type_the_crate_declares() -> None:
    """`impl<E> StdError for E` yields the subject `E`. It is a generic
    parameter, so "producers of E" is the whole crate — 33 of 186 fns on the
    live tree. The escalation must name nothing instead."""
    recs = [
        {"file": "src/ctx.rs", "fn_name": "ext_context",
         "container": "impl StdError for E",
         "sig_text": "fn ext_context<C>(self, context: C) -> Error",
         "body_text": ""},
        {"file": "src/error.rs", "fn_name": "msg",
         "container": "impl Error", "sig_text": "pub fn msg<M>(m: M) -> Self",
         "body_text": ""},
    ]

    class _S:
        def enumerate_fns(self, _sd):
            return recs

        def enumerate_types(self, _sd):
            return TYPES

    deps = _deps()
    object.__setattr__(deps, "structure", _S())
    assert escalate_frontier_to_producers(
        deps, Path("src"), "t", {("src/ctx.rs", "ext_context")}) == {}


def test_escalation_without_a_readable_type_set_stays_silent() -> None:
    """Fails toward silence: over-widening is this path's only failure mode."""
    deps = _deps(types=[])
    assert escalate_frontier_to_producers(
        deps, Path("src"), "test_len",
        futile_units={("src/chain.rs", "len")}) == {}


def test_unreadable_types_are_reported_as_a_read_failure_not_as_a_verdict() -> None:
    """The refusal must not describe the crate when it never read the crate.

    A reader missing `enumerate_types` raised `AttributeError` into a blanket
    `except`, and the escalation then logged `['Chain'] name no type this crate
    declares` — 99 times over three days, about types the crate declares. Both
    outcomes are silence; only one of them is a pipeline defect, and the log is
    the only place that difference can show up."""
    class _NoTypes:
        def enumerate_fns(self, _sd):
            return RECS
        # deliberately no enumerate_types — the production reader's shape

    lines: list[str] = []
    deps = _deps()
    object.__setattr__(deps, "structure", _NoTypes())
    object.__setattr__(deps, "log", lines.append)
    assert escalate_frontier_to_producers(
        deps, Path("src"), "test_len",
        futile_units={("src/chain.rs", "len")}) == {}
    msg = "\n".join(lines)
    assert "COULD NOT READ" in msg
    assert "AttributeError" in msg
    assert "pipeline defect" in msg
    # and it must NOT make the claim the old message made
    assert "name no type this crate declares" not in msg


def test_the_crate_declares_none_message_is_kept_for_the_real_case() -> None:
    """The other branch still says what it used to: the types WERE read and the
    candidates genuinely are not among them."""
    lines: list[str] = []
    deps = _deps(types=[TypeRecord(kind="struct", name="Unrelated")])
    object.__setattr__(deps, "log", lines.append)
    assert escalate_frontier_to_producers(
        deps, Path("src"), "test_len",
        futile_units={("src/chain.rs", "len")}) == {}
    msg = "\n".join(lines)
    assert "name no type this crate declares" in msg
    assert "COULD NOT READ" not in msg


def test_the_production_reader_can_answer_every_call_escalation_makes() -> None:
    """The seam pin. Every fake in this file defines `enumerate_types`, so the
    suite passed for three days while the composed object could not answer it —
    `AttributionDeps.structure` is `pipeline._LateStructure`, which carried
    `enumerate_fns` alone. Assert against the PRODUCTION class, not a fake."""
    import pipeline

    reader = pipeline._LateStructure()
    for method in ("enumerate_fns", "enumerate_types"):
        assert callable(getattr(reader, method, None)), (
            f"_LateStructure cannot answer {method!r}; "
            "escalate_frontier_to_producers calls it and swallows the failure")


def test_calling_an_associated_item_is_not_producing() -> None:
    """`Chain::new(...)` in a body makes the caller a CONSUMER of Chain. Counting
    it as a producer escalated a futile test to a third of the crate — and, since
    escalation additions bypassed the oracle filter, to `#[test]` fns whose
    bodies merely construct the type on the way to asserting something."""
    recs = [
        {"file": "src/user.rs", "fn_name": "consume", "container": "",
         "sig_text": "fn consume(c: &Chain) -> usize",
         "body_text": "let c = Chain::new(x); c.len()"},
        {"file": "src/t.gen_tests_1.rs", "fn_name": "test_chain_len",
         "container": "", "sig_text": "fn test_chain_len()",
         "body_text": "assert_eq!(Chain::new(x).len(), 1);"},
    ]
    assert producers_for_types(recs, {"Chain"}) == []


def test_self_returning_constructor_of_its_own_type_produces() -> None:
    """`impl Own<T> { fn new(p) -> Self { … } }` hands back an `Own` even though
    neither the return type nor the body spells the name."""
    recs = [
        {"file": "src/ptr.rs", "fn_name": "new", "container": "impl<T> Own<T>",
         "sig_text": "pub fn new(p: Box<T>) -> Self", "body_text": "unsafe { … }"},
        {"file": "src/other.rs", "fn_name": "new", "container": "impl<T> Ref<T>",
         "sig_text": "pub fn new(p: *const T) -> Self", "body_text": "…"},
    ]
    got = producers_for_types(recs, {"Own"})
    assert got == [("src/ptr.rs", "new")]


# The chain-traversal shape, from the live tree: `Chain::next` walks the error
# chain by calling `.source()` on each link, so the fn that severs the chain
# produces no `Chain` and constructs no `Chain` — no producer scan can reach it.
_CHAIN_RECS = [
    {"file": "src/chain.rs", "fn_name": "next",
     "container": "impl<'a> Iterator for Chain<'a>",
     "sig_text": "fn next(&mut self) -> Option<Self::Item>",
     "body_text": "let cur = next.take()?; *next = cur.source(); Some(cur)"},
    {"file": "src/chain.rs", "fn_name": "new",
     "container": "impl<'a> Chain<'a>",
     "sig_text": "pub fn new(head) -> Self", "body_text": "Chain { state }"},
    {"file": "src/context.rs", "fn_name": "source",
     "container": "impl<C> StdError for ContextError<C, Error>",
     "sig_text": "fn source(&self) -> Option<&(dyn StdError + 'static)>",
     "body_text": "None"},
    {"file": "src/error.gen_tests_2.rs", "fn_name": "source",
     "container": "impl StdError for MyError",
     "sig_text": "fn source(&self) -> Option<&(dyn StdError + 'static)>",
     "body_text": "Some(&self.inner)"},
    {"file": "src/error.rs", "fn_name": "source_helper",
     "container": "impl ErrorImpl",
     "sig_text": "fn source_helper(&self) -> u8", "body_text": "0"},
]


def _chain_deps():
    class _S:
        def enumerate_fns(self, _sd):
            return _CHAIN_RECS

        def enumerate_types(self, _sd):
            return [TypeRecord(kind="struct", name="Chain")]
    return AttributionDeps(
        structure=_S(), classify=lambda *a, **k: {},
        locate_fn=lambda *a, **k: None,
        run_check_diag=lambda *a, **k: (0, "", []),
        derive_check_only_cmd=lambda c: c, is_gen_tests_file=lambda n: False,
        log=lambda m: None,
    )


def test_escalation_names_the_traversal_step_a_walked_chain_dispatches_through():
    """`test_iter`/`test_len`/`test_rev`/`test_clone` escalated to
    `chain.rs::new`, `chain.rs::default`, `error.rs::construct`,
    `error.rs::chain` and `error.rs::erase` on every sweep of two runs, while
    the fn that severed the chain — `impl<C> StdError for ContextError<C,
    Error>::source`, returning `None` — was in neither escalated set. It
    produces no `Chain`, so no producer scan can ever reach it."""
    extra = escalate_frontier_to_producers(
        _chain_deps(), Path("src"), "test_iter",
        futile_units={("src/chain.rs", "next")})
    assert ("src/context.rs", "source") in extra
    assert all(tests == {"test_iter"} for tests in extra.values())


def test_the_traversal_step_stays_bounded():
    """Only TRAIT impls of a method the futile accessor actually CALLS, and
    never an oracle fixture's own impl. An inherent same-named method is
    reachable already and would widen the set for nothing."""
    extra = escalate_frontier_to_producers(
        _chain_deps(), Path("src"), "test_iter",
        futile_units={("src/chain.rs", "next")})
    # oracle fixture: a `gen_tests` sibling's `impl StdError for MyError`
    assert ("src/error.gen_tests_2.rs", "source") not in extra
    # inherent, and not even a called name
    assert ("src/error.rs", "source_helper") not in extra
    # the accessor itself is never re-blamed
    assert ("src/chain.rs", "next") not in extra


def test_negative_control_the_producer_scan_alone_never_names_it():
    """The pre-fix escalation, run on the same records: producers of `Chain`
    are found, the traversal step is not. This is the state both runs were in."""
    got = producers_for_types(_CHAIN_RECS, {"Chain"},
                              exclude={("src/chain.rs", "next")})
    assert ("src/chain.rs", "new") in got
    assert ("src/context.rs", "source") not in got


# --- the oracle guard the producer scan was missing --------------------------

# A `#[test]` fn that builds the subject on its way to an assertion. It matches
# `constructs_type` exactly as a real constructor does, and the reconstruction
# may never write to it.
ORACLE_RECS = [
    *RECS,
    {"file": "tests/test_chain.rs", "fn_name": "error", "container": "",
     "sig_text": "fn error() -> Error", "body_text": "Error::msg(\"oh no\")",
     "cfg": "test", "is_test": True},
    {"file": "tests/test_context.rs", "fn_name": "make_chain", "container": "",
     "sig_text": "fn make_chain() -> Chain", "body_text": "Chain { state }",
     "cfg": "test", "is_test": True},
]


def _oracle_deps():
    class _S:
        def enumerate_fns(self, _sd):
            return ORACLE_RECS

        def enumerate_types(self, _sd):
            return TYPES
    return AttributionDeps(
        structure=_S(), classify=lambda *a, **k: {},
        locate_fn=lambda *a, **k: None,
        run_check_diag=lambda *a, **k: (0, "", []),
        derive_check_only_cmd=lambda c: c, is_gen_tests_file=lambda n: False,
        log=lambda m: None,
    )


def test_the_escalation_never_names_an_oracle_fn_as_a_producer() -> None:
    """`_traversal_steps` excludes oracle records by its own rule and the
    name-match frontier excludes them with the same predicate; the producer
    scan between them did neither.

    On the live tree that put `tests/test_chain.rs::error`,
    `tests/test_context.rs::make_chain` and `tests/test_ffi.rs::anyhow3` at the
    HEAD of all fourteen escalation fires across runs `1246060-2339497b` and
    `3410521-4ccf6d68` — 3 of every 24-25 escalated units, in the frontier and
    therefore in the pass denominator, discarded only later by the fill route's
    immutable-target refusal (95 `skipping test-oracle target` lines)."""
    extra = escalate_frontier_to_producers(
        _oracle_deps(), Path("src"), "test_len",
        futile_units={("src/chain.rs", "len")})
    assert ("tests/test_chain.rs", "error") not in extra
    assert ("tests/test_context.rs", "make_chain") not in extra
    # …and the real producers still escalate, so the guard did not empty it.
    assert ("src/error.rs", "chain") in extra
    assert ("src/chain.rs", "new") in extra


def test_negative_control_the_unguarded_scan_does_name_them() -> None:
    """Without the record filter the oracle fns match `constructs_type` and
    `returns_type` like any other producer — which is why the guard is needed
    and not merely tidy."""
    got = producers_for_types(ORACLE_RECS, {"Chain", "Error"},
                              exclude={("src/chain.rs", "len")})
    assert ("tests/test_chain.rs", "error") in got
    assert ("tests/test_context.rs", "make_chain") in got


# A resumed floor's producer grade is this scan, not the saved tuple. The
# accessor's own impl returns the type and is excluded. The free fn hands
# the type back. The third fn does not. An oracle fn that constructs it is
# not a producer either.
_CELL_RECS: list[dict[str, Any]] = [
    {"file": "src/cell.rs", "fn_name": "fmt", "container": "impl Cell",
     "sig_text": "fn fmt(&self) -> Cell", "body_text": "Cell { n: 0 }"},
    {"file": "src/cell.rs", "fn_name": "make", "container": "",
     "sig_text": "fn make() -> Cell", "body_text": "Cell { n: 1 }"},
    {"file": "src/cell.rs", "fn_name": "widen", "container": "",
     "sig_text": "fn widen(p: usize) -> usize", "body_text": "p"},
    {"file": "tests/t.rs", "fn_name": "builds", "container": "",
     "sig_text": "fn builds() -> Cell", "body_text": "Cell { n: 2 }",
     "is_test": True},
]
_CELL = FnLocator("src/cell.rs", "fmt")


def test_the_producer_scan_names_who_builds_the_accessors_type() -> None:
    """The grade a futility fire would write. The accessor, the fn that
    does not produce the type, and the oracle fn are not in it."""
    scan = producer_scan(
        _CELL_RECS, [{"kind": "struct", "name": "Cell"}], {_CELL})
    assert scan.producers == (FnLocator("src/cell.rs", "make"),)
    assert scan.type_names == ("Cell",)
    assert "Cell" in scan.candidates


def test_an_unreadable_type_list_is_not_a_producer_measurement() -> None:
    """`None` is the extractor unavailable: no producer, and the candidates
    are still named so a caller can refuse to treat that empty result as a
    measurement."""
    scan = producer_scan(_CELL_RECS, None, {_CELL})
    assert scan.producers == ()
    assert scan.candidates == ("Cell",)
    assert scan.type_names == ()


# The derivation the fire and a resumed floor share. The accessor calls
# `.source()`, which a trait impl defines: that impl is a traversal step,
# added beside the producer and not graded as one.
_LINK_RECS: list[dict[str, Any]] = [
    {"file": "src/cell.rs", "fn_name": "fmt", "container": "impl Cell",
     "sig_text": "fn fmt(&self) -> usize", "body_text": "self.inner.source()"},
    {"file": "src/cell.rs", "fn_name": "make", "container": "",
     "sig_text": "fn make() -> Cell", "body_text": "Cell { n: 1 }"},
    {"file": "src/link.rs", "fn_name": "source",
     "container": "impl StdError for Link",
     "sig_text": "fn source(&self) -> Option<&dyn StdError>",
     "body_text": "None"},
]


def test_the_escalation_adds_producers_and_steps_and_grades_producers() -> None:
    esc = producer_escalation(
        _LINK_RECS, [{"kind": "struct", "name": "Cell"}], {_CELL})
    assert esc.producers == (FnLocator("src/cell.rs", "make"),)
    assert esc.traversed == (FnLocator("src/link.rs", "source"),)
    assert esc.additions("t") == {FnLocator("src/cell.rs", "make"): {"t"},
                                  FnLocator("src/link.rs", "source"): {"t"}}


def test_no_declared_type_is_no_escalation_even_with_steps() -> None:
    """The fire returns nothing when the container names no declared type,
    so a resumed product on that tree is dropped, steps and all."""
    esc = producer_escalation(
        _LINK_RECS, [{"kind": "struct", "name": "Other"}], {_CELL})
    assert esc.additions("t") == {}
    assert esc.traversed == ()
