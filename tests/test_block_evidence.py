"""A hop pair is one write: an unentered implementer is not graded INFERRED.

Coverage of the current tree often proves the failing tests enter the
dispatcher and not the implementer. That emptiness is the pair's premise
(the dispatcher does not yet call the implementer on this arm), not a
licence to leave the implementer unchanged. `promote_pair_write_grades`
turns NAMED members DIRECT for prompt rendering only, copying tests
already on DIRECT donors. It adds no test-to-unit edge.

The crate here is a Display dispatcher in lib.rs / render.rs — the
never-steer check's own example. Solo (one-block) assembly must stay
byte-identical to the pre-flag call.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.block_evidence import (
    assemble_block_evidence,
    promote_pair_write_grades,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    RefillEvidence,
    inferred_refill_names,
    render_refill_evidence,
)


def _member(file: str, name: str) -> FnRecord:
    return FnRecord(file=file, fn_name=name, sig_text=f"fn {name}(&self)",
                    body_text="()", has_unimplemented_body=False)


def _block(file: str, container: str, name: str) -> ImplBlock:
    m = _member(file, name)
    return ImplBlock(file=file, container=container, members=(m,),
                     refill_members=(m,))


def _ev(name: str, grade: Attribution, tests: tuple[str, ...] = ()) -> RefillEvidence:
    return RefillEvidence(
        fn_name=name, tests=tests, attribution=grade,
        sibling_containers=(), excerpts=())


def test_named_member_becomes_direct_when_a_donor_already_is() -> None:
    named = _ev("render", Attribution.NAMED)
    donor = _ev("fmt", Attribution.DIRECT, tests=("t_pair",))
    out = promote_pair_write_grades((donor, named))
    assert out[0].attribution is Attribution.DIRECT
    assert out[1].attribution is Attribution.DIRECT
    assert out[1].tests == ("t_pair",)
    assert inferred_refill_names(out) == frozenset()
    text = render_refill_evidence(out)
    assert "DIRECT: a failing test's own failure points" in text
    assert "return it UNCHANGED" not in text


def test_no_direct_donor_leaves_named_untouched() -> None:
    """A pair with no convicting tests is still a name-match fill."""
    named = (_ev("fmt", Attribution.NAMED), _ev("render", Attribution.NAMED))
    assert promote_pair_write_grades(named) == named
    assert inferred_refill_names(named) == frozenset({"fmt", "render"})


def test_escalated_and_crashed_outrank_and_are_left_alone() -> None:
    donor = _ev("fmt", Attribution.DIRECT, tests=("t_pair",))
    crashed = _ev("drop", Attribution.CRASHED)
    escalated = _ev("new", Attribution.ESCALATED)
    out = promote_pair_write_grades((donor, crashed, escalated))
    assert out[1].attribution is Attribution.CRASHED
    assert out[2].attribution is Attribution.ESCALATED


def test_joint_rewrite_assembly_promotes_the_unentered_implementer() -> None:
    disp = _block("lib.rs", "impl fmt::Display for Report", "fmt")
    impl = _block("render.rs", "impl Renderer", "render")
    fr = FailureFrontier.build(
        {FnLocator("lib.rs", "fmt"): {"t_pair"},
         FnLocator("render.rs", "render"): {"t_pair"}},
        {FnLocator("lib.rs", "fmt"): {"t_pair"}})
    joint = assemble_block_evidence(
        (disp, impl), [disp, impl], fr,
        find_tests=lambda leaf, mx: [],
        joint_rewrite=True)
    grades = {e.fn_name: e.attribution for e in joint.evidence}
    assert grades == {"fmt": Attribution.DIRECT, "render": Attribution.DIRECT}
    assert inferred_refill_names(joint.evidence) == frozenset()
    assert "return it UNCHANGED" not in joint.refill_evidence
    assert "`render` — DIRECT:" in joint.refill_evidence


def test_one_block_assembly_stays_inferred_without_the_flag() -> None:
    """Negative control: the per-impl route's one-block call does not
    promote. An unentered implementer dispatched alone is still INFERRED —
    that is Fix 3's unreached gate, and the pair flag must not leak into
    it."""
    impl = _block("render.rs", "impl Renderer", "render")
    fr = FailureFrontier.build(
        {FnLocator("render.rs", "render"): {"t_pair"}},
        {})
    solo = assemble_block_evidence(
        (impl,), [impl], fr, find_tests=lambda leaf, mx: [])
    (ev,) = solo.evidence
    assert ev.attribution is Attribution.NAMED
    assert "INFERRED" in solo.refill_evidence
    assert "return it UNCHANGED" in solo.refill_evidence
