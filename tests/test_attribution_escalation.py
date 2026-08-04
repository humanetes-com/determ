"""Pins for futility-triggered attribution escalation: when every
fn attributed to a failing test has been rewritten to the same dead end, the
frontier escalates to producers of the exercised type."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    container_type_name,
    escalate_frontier_to_producers,
    producers_for_types,
)
from pipeline_core.domain.models import TypeRecord

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
