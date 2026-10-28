"""A reverted write's breakage reaches every later route over its units.

Run `2355041-eae39441` (2026-10-27): fill 7's pair over a dispatcher and its
formatter was reverted for reddening five passing tests, and the pair kept
that measurement on its `*pair*` record. Fill 8's session over the same two
units reddened the same five; its turn-0 prompt (`build_slice_session_prompt`,
slot `prior_regression`) named none of them, because the session read only
its `*session*` record.

Claims: the stamp holds the tests, what they printed and whose write it was;
the union over a slice's unit records renders that section, less the tests
the caller already renders; it is not written into `regressed`, so
`regression_scoped`'s decision about a unit's own diagnosis does not move.
Control: a unit record without the stamp renders nothing (the session's view
before the change). Invented shape.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.block_evidence import render_sibling_regression
from pipeline_core.domain.retro_gate import ConvictionEvidence, regression_scoped
from pipeline_core.domain.sibling_regression import SIBLING_REDDENED, stamped, union_of


def test_the_stamp_reaches_the_next_route_over_the_units() -> None:
    a = stamped({"guidance": "own"}, tests=["bin::t_one", "t_two"],
                outputs={"t_one": "left: 1 right: 2", "other": "x"}, by="pair a + b")
    b = stamped(None, tests=["t_two"], outputs={}, by="pair a + b")
    tests, outputs, writers = union_of([a, b, None], skip=["t_two"])
    assert tests == ("bin::t_one",) and outputs == {"t_one": "left: 1 right: 2"}
    assert writers == ("pair a + b",)
    text = render_sibling_regression(tests, "", outputs, writers)
    assert "bin::t_one" in text and "left: 1 right: 2" in text
    assert "pair a + b" in text and "REVERTED" in text
    # Control: the record as the session read it before carries nothing.
    assert union_of([{"guidance": "own"}]) == ((), {}, ())
    assert render_sibling_regression((), "", {}, ()) == ""


def test_the_stamp_does_not_move_the_unit_s_own_regression_scope() -> None:
    rec = {"regressed": ["t_own"], "guidance": "g"}
    ev = ConvictionEvidence(frozenset({"t_own"}))
    before = regression_scoped(rec, ev)
    after = regression_scoped(stamped(rec, tests=["t_other"], outputs={}, by="x"), ev)
    assert before == after
    assert SIBLING_REDDENED in stamped(rec, tests=["t_other"], outputs={}, by="x")
