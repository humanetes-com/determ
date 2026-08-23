"""Pins for `domain.refill_evidence` — what a prompt may claim about a body it
asks the model to rewrite.

The instance: the frontier entry `src/context.rs::source` (a traversal step the
escalation added for `test_iter`/`test_len`/`test_rev`/`test_clone`) stamped
[REFILL] on BOTH `impl<C, E> StdError for ContextError<C, E>` (whose body is
correct) and `impl<C> StdError for ContextError<C, Error>` (the severed
`None`). The prompt told the model the correct one "FAILS — rewrite the
BEHAVIOR" and showed the sibling as settled; the tests it meant were not in
the prompt (run `902902-daee577d`, 2026-08-20).
"""
from __future__ import annotations

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    RefillEvidence,
    attributed_test_leaves,
    collect_refill_evidence,
    container_header,
    inferred_refill_names,
    render_refill_evidence,
)

FILE = "src/context.rs"
GENERIC = "impl<C, E> StdError for ContextError<C, E>\nwhere\n    C: Display,\n    E: StdError + 'static, {"
ANYHOW = "impl<C> StdError for ContextError<C, Error>\nwhere C: Display, {"


def _member(name: str, body: str) -> FnRecord:
    return FnRecord(file=FILE, fn_name=name, sig_text=f"fn {name}(&self)",
                    body_text=body, has_unimplemented_body=False)


def _block(container: str, body: str) -> ImplBlock:
    m = _member("source", body)
    return ImplBlock(file=FILE, container=container, members=(m,), refill_members=(m,))


def _frontier(direct: bool, evidence: dict[str, str] | None = None) -> FailureFrontier:
    key = FnLocator(FILE, "source")
    dispatch = {key: {"test_iter", "test_len"}}
    convicting = {key: {"test_len"}} if direct else {}
    return FailureFrontier.build(dispatch, convicting, evidence=evidence)


def test_same_named_refill_in_two_blocks_is_inferred_with_the_sibling_named() -> None:
    generic, anyhow = _block(GENERIC, "Some(&self.error)"), _block(ANYHOW, "None")
    fr = _frontier(direct=False, evidence={
        "test_len": "panicked at tests/test_chain.rs:31:5\nassertion `left == right` failed\n  left: 4\n  right: 1"})
    (ev,) = collect_refill_evidence(generic, [generic, anyhow], fr)
    assert ev == RefillEvidence(
        fn_name="source", tests=("test_iter", "test_len"),
        attribution=Attribution.NAMED,
        sibling_containers=(ANYHOW,),
        excerpts=(("test_len", fr.evidence_for("test_len")),))
    assert inferred_refill_names([ev]) == frozenset({"source"})
    text = render_refill_evidence([ev])
    assert "INFERRED" in text and "return it UNCHANGED" in text
    assert "impl<C> StdError for ContextError<C, Error> where C: Display," in text
    assert "attributed failing test(s): test_iter, test_len" in text
    assert "test_len said:" in text and "left: 4" in text and "right: 1" in text
    # The sibling sees the generic block as ITS candidate.
    (ev2,) = collect_refill_evidence(anyhow, [generic, anyhow], fr)
    assert ev2.sibling_containers == (GENERIC,)


def test_direct_conviction_is_stated_as_such() -> None:
    generic = _block(GENERIC, "Some(&self.error)")
    (ev,) = collect_refill_evidence(generic, [generic], _frontier(direct=True))
    assert ev.direct and ev.sibling_containers == ()
    assert inferred_refill_names([ev]) == frozenset()
    text = render_refill_evidence([ev])
    assert "DIRECT" in text and "INFERRED" not in text
    assert "Rewrite its BEHAVIOR" in text


def test_no_frontier_is_inference_with_no_tests() -> None:
    generic = _block(GENERIC, "Some(&self.error)")
    (ev,) = collect_refill_evidence(generic, [generic], None)
    assert ev.tests == () and not ev.direct and ev.excerpts == ()
    assert render_refill_evidence([ev]).startswith("`source` — INFERRED")
    assert render_refill_evidence([]) == ""
    # A block with no re-fill members has nothing to say.
    plain = ImplBlock(file=FILE, container=GENERIC, members=(_member("source", "x"),))
    assert collect_refill_evidence(plain, [plain], _frontier(direct=True)) == ()


def test_attributed_test_leaves_direct_first_bare_deduped_capped() -> None:
    evs = [
        RefillEvidence("a", ("chain::test_rev", "test_iter"),
                       Attribution.NAMED, (), ()),
        RefillEvidence("b", ("tests::test_len", "test_iter"),
                       Attribution.DIRECT, (), ()),
    ]
    assert attributed_test_leaves(evs) == ("test_len", "test_iter", "test_rev")
    assert attributed_test_leaves(evs, cap=2) == ("test_len", "test_iter")
    assert attributed_test_leaves([]) == ()


def test_container_header_is_one_line_without_the_body() -> None:
    assert container_header(GENERIC) == (
        "impl<C, E> StdError for ContextError<C, E> where C: Display, E: StdError + 'static,")
    assert container_header("impl Error {") == "impl Error"
    assert container_header("") == ""
