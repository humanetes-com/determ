"""Admission rules for persisted retrospectives: what the sieve does with a
stored guidance string (counts, not just per-unit lines) and when a stored
`fully_exhausted` verdict may still convict a unit."""
from __future__ import annotations

from pipeline_core.domain.retro_gate import (
    GATE_VERSION,
    ROUTE_VERSION,
    RetroGateTally,
    RetroGateVerdict,
    RetroProvenance,
    anchor_types_for_container,
    conviction_is_current,
    judge_guidance,
    layout_fingerprint,
)

LAYOUT = """
pub(crate) struct ErrorImpl<E = ()> {
    pub(crate) vtable: &'static ErrorVTable,
    pub(crate) error: E,
}
"""


def test_anchor_types_from_container_renderings() -> None:
    # canonical key form (no whitespace around `for`) and the raw impl signature
    assert anchor_types_for_container("DebugforErrorImpl<E>") == {"ErrorImpl"}
    assert anchor_types_for_container("impl Debug for ErrorImpl<E>") == {"ErrorImpl"}
    assert anchor_types_for_container("Error") == {"Error"}
    # a free fn has no self type to anchor to
    assert anchor_types_for_container("") == frozenset()


def test_verdicts_split_retired_from_unjudgeable() -> None:
    assert judge_guidance("", LAYOUT) is RetroGateVerdict.NO_GUIDANCE
    assert judge_guidance(
        "ErrorImpl keeps the payload in `self.error`.", LAYOUT,
    ) is RetroGateVerdict.HONOURED
    assert judge_guidance(
        "ErrorImpl keeps the payload in `self._object`.", LAYOUT,
    ) is RetroGateVerdict.RETIRED
    # names no struct declared here → injected, but never actually checked;
    # counted apart so "retires nothing" is not silence.
    assert judge_guidance(
        "prefer `Box::leak` over a stack borrow", LAYOUT,
    ) is RetroGateVerdict.UNANCHORED
    assert judge_guidance(
        "change the body to `self._object.provide(request)`", LAYOUT,
        anchor_types=frozenset({"ErrorImpl"}),
    ) is RetroGateVerdict.RETIRED


def test_tally_counts_and_renders() -> None:
    tally = RetroGateTally()
    for verdict in (RetroGateVerdict.HONOURED, RetroGateVerdict.RETIRED,
                    RetroGateVerdict.RETIRED, RetroGateVerdict.UNANCHORED,
                    RetroGateVerdict.NO_GUIDANCE):
        assert tally.record(verdict) is verdict
    assert (tally.honoured, tally.retired, tally.unanchored) == (1, 2, 1)
    assert tally.judged == 4  # guidance existed and was sieved
    assert tally.total == 5   # every unit that reached the gate
    assert tally.render() == (
        "retro-gate: honoured=1 retired=2 unanchored=1 no_guidance=1")


def test_tally_line_fires_on_an_empty_retro_store() -> None:
    """A --clean run has no stored guidance: the batch must still render, or
    'nothing to judge' looks exactly like 'the gate never ran'."""
    tally = RetroGateTally()
    for _ in range(3):
        tally.record(RetroGateVerdict.NO_GUIDANCE)
    assert tally.judged == 0
    assert tally.total == 3
    assert tally.render().endswith("no_guidance=3")


def test_fingerprint_tracks_layout_not_bodies() -> None:
    filled = LAYOUT + "\nimpl ErrorImpl { fn get(&self) -> &E { &self.error } }\n"
    assert layout_fingerprint(filled) == layout_fingerprint(LAYOUT)
    renamed = LAYOUT.replace("error: E", "_object: E")
    assert layout_fingerprint(renamed) != layout_fingerprint(LAYOUT)
    assert layout_fingerprint("") == ""


def test_conviction_honoured_only_in_the_context_it_was_earned() -> None:
    prov = RetroProvenance.current("stable", LAYOUT)
    record = prov.stamp({"fully_exhausted": True})
    assert record["cluster"] == "stable"
    assert record["gate_ver"] == GATE_VERSION
    assert record["route_ver"] == ROUTE_VERSION
    assert conviction_is_current(record, prov)
    # other cluster, re-authored layout, changed gate semantics, changed routes
    assert not conviction_is_current(
        record, RetroProvenance.current("nightly", LAYOUT))
    assert not conviction_is_current(
        record, RetroProvenance.current("stable", LAYOUT.replace("error: E",
                                                                 "_object: E")))
    assert not conviction_is_current(
        record, RetroProvenance("stable", prov.layout_fp, gate_ver="next"))
    assert not conviction_is_current(
        record, RetroProvenance("stable", prov.layout_fp, route_ver="next"))


def test_legacy_records_convict_nothing() -> None:
    # No stamp = earned in an UNKNOWN context, which is not evidence about this
    # one. Anything else silently re-imports a pre-migration verdict.
    prov = RetroProvenance.current("stable", LAYOUT)
    assert not conviction_is_current({"fully_exhausted": True}, prov)
    assert not conviction_is_current(None, prov)
    assert not conviction_is_current(
        {"fully_exhausted": True, "cluster": "stable"}, prov)


def test_fingerprint_survives_comment_and_fn_local_churn() -> None:
    # Annotated stubs preserve failed attempts + cargo errors as comments, and
    # bodies may declare local helpers — none of that is layout, so none of it
    # may void a conviction.
    churned = (
        LAYOUT
        + "\nfn mk() { struct Local; let _ = Local; }\n"
        + "// last attempt: struct ErrorImpl { _object: E }\n"
        + "/* error[E0560]: struct ErrorImpl has no field named `_object` */\n"
    )
    assert layout_fingerprint(churned) == layout_fingerprint(LAYOUT)
