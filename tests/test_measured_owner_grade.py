"""A unit the owner map admitted is graded as coverage, never as a measured
futility conviction.

Run `3076-051d385a` (2026-10-13, cluster `stable`): the measured-owners
admission wrote 30 coverage owners of three leftovers into the escalation's
set, so every prompt that listed them said "the fns that consume what this
one produces were rewritten over repeated sweeps and NO test changed its
verdict, so the defect is … in what this fn constructs". No consumer of
those bodies had been measured futile. A `src/ptr.rs` file prompt told the
model a pointer cast was that defect for a Debug rendering and a downcast;
the reply rewrote pointer code and failed `E0512` twice.

The plan's sentence: no prompt carries the futility sentence for a unit
whose only provenance is the owner map; it carries the coverage sentence.
Negative control: the same unit passed through the escalation's set (the
pre-change wiring) renders the futility sentence. Invented shape.
"""
from __future__ import annotations

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    collect_refill_evidence,
    measured_owner_refill_names,
    render_refill_evidence,
)
from pipeline_core.prompts.fix_prompts import BlockFillPlan, _plan_line

_FILE = "src/cell.rs"
_FUTILITY = "the fns that consume what this one"
_KEY = FnLocator(_FILE, "cast")


def _block() -> ImplBlock:
    m = FnRecord(file=_FILE, fn_name="cast", sig_text="fn cast(self) -> Cell",
                 body_text="Cell(self.0)", has_unimplemented_body=False)
    return ImplBlock(file=_FILE, container="impl Cell {", members=(m,),
                     refill_members=(m,))


def _frontier() -> FailureFrontier:
    return FailureFrontier.build(
        {_KEY: {"renders_cell"}}, {},
        evidence={"renders_cell": "thread 'renders_cell' panicked at "
                                  "tests/cell.rs:9:5:\nassertion failed"})


def test_an_owner_map_admit_is_graded_as_coverage() -> None:
    b = _block()
    ev = collect_refill_evidence(b, (b,), _frontier(),
                                 measured_owner=frozenset({_KEY}))
    assert [e.attribution for e in ev] == [Attribution.MEASURED_OWNER]
    assert measured_owner_refill_names(ev) == {"cast"}
    text = render_refill_evidence(ev)
    assert _FUTILITY not in text
    assert "executed this body before their failure" in text


def test_negative_control_the_escalation_set_renders_the_futility_claim() -> None:
    b = _block()
    ev = collect_refill_evidence(b, (b,), _frontier(),
                                 escalated=frozenset({_KEY}))
    assert [e.attribution for e in ev] == [Attribution.ESCALATED]
    assert _FUTILITY in render_refill_evidence(ev)


def test_a_futility_product_outranks_the_coverage_grade() -> None:
    b = _block()
    ev = collect_refill_evidence(b, (b,), _frontier(),
                                 escalated=frozenset({_KEY}),
                                 measured_owner=frozenset({_KEY}))
    assert [e.attribution for e in ev] == [Attribution.ESCALATED]


def test_the_file_write_list_states_coverage_not_a_rewrite_order() -> None:
    line = _plan_line(BlockFillPlan("impl Cell", refills=("cast",),
                                    measured_owner=("cast",)))
    assert "on the failing path" in line and "cast" in line
    assert "constructs or returns" not in line
    assert "rewrite (a body exists, but its test still fails)" not in line
    # Negative control: the same name graded escalated, as the admits were.
    esc = _plan_line(BlockFillPlan("impl Cell", refills=("cast",),
                                   escalated=("cast",)))
    assert "constructs or returns" in esc
