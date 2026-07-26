"""Pins for futility-triggered attribution escalation (debt #82): when every
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


def _deps():
    class _S:
        def enumerate_fns(self, _sd):
            return RECS
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
    deps = _deps()
    object.__setattr__(deps, "structure", _Broken())
    assert escalate_frontier_to_producers(
        deps, Path("src"), "t", {("f", "g")}) == {}


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
