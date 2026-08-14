"""Admission rules for persisted retrospectives: what the sieve does with a
stored guidance string (counts, not just per-unit lines) and when a stored
`fully_exhausted` verdict may still convict a unit."""
from __future__ import annotations

from pipeline_core.domain.retro_gate import (
    GATE_VERSION,
    GUIDANCE_FROM_HOLLOW_TREE,
    ROUTE_VERSION,
    BlockedOnStubs,
    ConvictionEvidence,
    ConvictionStanding,
    RetroGateTally,
    RetroGateVerdict,
    RetroProvenance,
    admissible_guidance,
    anchor_types_for_container,
    conviction_standing,
    judge_guidance,
    layout_fingerprint,
    stamp_conviction,
)

NO_TESTS = ConvictionEvidence(frozenset())

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


def _standing(record: object, prov: RetroProvenance,
              ev: ConvictionEvidence = NO_TESTS) -> ConvictionStanding:
    return conviction_standing(record, prov, ev)  # type: ignore[arg-type]


def test_conviction_honoured_only_in_the_context_it_was_earned() -> None:
    prov = RetroProvenance.current("stable", LAYOUT)
    record = stamp_conviction({"fully_exhausted": True}, prov, NO_TESTS)
    assert record["cluster"] == "stable"
    assert record["gate_ver"] == GATE_VERSION
    assert record["route_ver"] == ROUTE_VERSION
    assert _standing(record, prov) is ConvictionStanding.CURRENT
    # other cluster, re-authored layout, changed gate semantics, changed routes
    for other in (RetroProvenance.current("nightly", LAYOUT),
                  RetroProvenance.current(
                      "stable", LAYOUT.replace("error: E", "_object: E")),
                  RetroProvenance("stable", prov.layout_fp, gate_ver="next"),
                  RetroProvenance("stable", prov.layout_fp, route_ver="next")):
        assert _standing(record, other) is ConvictionStanding.STALE_CONTEXT


def test_conviction_dies_when_the_tests_it_rests_on_stop_convicting() -> None:
    # The plateau this closes: a block was convicted while the frontier blamed
    # every unit for every failing test. Once attribution narrows to direct
    # evidence, that verdict rests on a failure the unit never answered for —
    # honouring it skips the unit before a single attempt, forever.
    prov = RetroProvenance.current("stable", LAYOUT)
    earned = ConvictionEvidence(frozenset({"test_own", "test_foreign"}))
    record = stamp_conviction({"fully_exhausted": True}, prov, earned)
    assert record["convicted_by"] == ["test_foreign", "test_own"]
    assert _standing(record, prov, earned) is ConvictionStanding.CURRENT
    narrowed = ConvictionEvidence(frozenset({"test_own"}))
    assert _standing(record, prov, narrowed) is ConvictionStanding.STALE_EVIDENCE
    # A WIDER frontier still supports it — every test it was drawn from may
    # still convict, so the sample it took is unchanged.
    wider = ConvictionEvidence(frozenset({"test_own", "test_foreign", "test_new"}))
    assert _standing(record, prov, wider) is ConvictionStanding.CURRENT


def test_compile_only_conviction_is_never_invalidated_by_the_frontier() -> None:
    # Exhausting on compile errors names no tests; nothing about the blame map
    # can make that verdict unfair, so it must survive every frontier change.
    prov = RetroProvenance.current("stable", LAYOUT)
    record = stamp_conviction({"fully_exhausted": True}, prov, NO_TESTS)
    assert record["convicted_by"] == []
    assert _standing(record, prov,
                     ConvictionEvidence(frozenset({"test_x"}))) is (
        ConvictionStanding.CURRENT)


def test_legacy_records_convict_nothing() -> None:
    # No stamp = earned in an UNKNOWN context, which is not evidence about this
    # one. Anything else silently re-imports a pre-migration verdict.
    prov = RetroProvenance.current("stable", LAYOUT)
    assert _standing({"fully_exhausted": True}, prov) is (
        ConvictionStanding.STALE_CONTEXT)
    assert _standing(None, prov) is ConvictionStanding.STALE_CONTEXT
    assert _standing({"fully_exhausted": True, "cluster": "stable"}, prov) is (
        ConvictionStanding.STALE_CONTEXT)


def test_context_stamped_but_evidence_missing_is_stale_evidence() -> None:
    # The live store's shape before this field existed: full provenance, no
    # `convicted_by`. Unknown blame map ⇒ it convicts nothing, same rule as a
    # missing provenance stamp — this is what retires them without a migration.
    prov = RetroProvenance.current("stable", LAYOUT)
    record = prov.stamp({"fully_exhausted": True})
    assert "convicted_by" not in record
    assert _standing(record, prov) is ConvictionStanding.STALE_EVIDENCE


def test_guidance_drawn_from_a_foreign_red_test_is_withheld() -> None:
    # Re-admitting a unit while still handing it the lesson a foreign red test
    # taught spends the fresh attempts on the same wrong target — the failure
    # mode a blind exhaustion reset already demonstrated.
    behavioural = {
        "guidance": "Make Display prepend the source chain so the output reads "
                    "\"other error: boxed_err\".",
        "error_count": 0,
        "convicted_by": ["test_from_boxed_basic"],
    }
    mine = ConvictionEvidence(frozenset({"test_from_boxed_basic"}))
    assert admissible_guidance(behavioural, LAYOUT, evidence=mine)
    theirs = ConvictionEvidence(frozenset({"test_display_plain"}))
    assert admissible_guidance(behavioural, LAYOUT, evidence=theirs) == ""


def test_compiler_taught_guidance_outlives_any_frontier_change() -> None:
    # A lesson drawn from a compile error came from the compiler, not from a
    # test — no change to the blame map can make it wrong, so narrowing the
    # frontier must not cost it (that would throw away the store's real value).
    compiled = {"guidance": "`Self` may not be nested inside a generic arg here.",
                "error_count": 4, "convicted_by": ["test_whatever"]}
    assert admissible_guidance(
        compiled, LAYOUT, evidence=ConvictionEvidence(frozenset()))
    # …but a layout-contradicted lesson is still withheld, on either path.
    lying = {"guidance": "Read the payload from the `_object` field of ErrorImpl.",
             "error_count": 4, "convicted_by": []}
    assert admissible_guidance(
        lying, LAYOUT, evidence=ConvictionEvidence(frozenset()),
        anchor_types=frozenset({"ErrorImpl"})) == ""


def test_legacy_behavioural_guidance_is_withheld_until_re_earned() -> None:
    # No `convicted_by` + compiled clean = a lesson from an unknown blame map.
    legacy = {"guidance": "Make the output read \"other error: x\".",
              "error_count": 0}
    assert admissible_guidance(
        legacy, LAYOUT, evidence=ConvictionEvidence(frozenset({"t"}))) == ""


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


VTABLE_LAYOUT = """
pub(crate) struct ErrorVTable {
    object_drop: unsafe fn(Own<ErrorImpl>),
    object_drop_rest: unsafe fn(Own<ErrorImpl>, TypeId),
}
unsafe fn object_drop(e: Own<ErrorImpl>) { unimplemented!() }
unsafe fn object_drop_front(e: Own<ErrorImpl>, t: TypeId) { unimplemented!() }
"""


def test_guidance_prescribing_a_field_as_a_fn_is_retired() -> None:
    # `object_drop_rest` is a FIELD of ErrorVTable and no fn of that name is
    # declared; the field-claim sieve alone reads it as a real member and keeps
    # the text. On cluster `stable` (2026-08-09) the stored lesson for
    # `error.rs::construct_from_std` read "replace `object_drop_front::<E>` with
    # `object_drop_rest::<E>` in the vtable literal" — the exact inverse of the
    # correct assignment `object_drop_rest: object_drop_front::<E>` — and every
    # attempt that honoured it died on an unresolved name.
    poisoned = {
        "guidance": "…replace `object_drop_front::<E>` with `object_drop_rest::<E>` "
                    "in the vtable literal.",
        "error_count": 3, "convicted_by": [],
    }
    assert admissible_guidance(poisoned, VTABLE_LAYOUT, evidence=NO_TESTS) == ""
    # The correct direction names a fn the file declares and must survive.
    sound = {"guidance": "Assign `object_drop_front::<E>` to the drop_rest slot.",
             "error_count": 3, "convicted_by": []}
    assert admissible_guidance(sound, VTABLE_LAYOUT, evidence=NO_TESTS)


def test_path_qualified_and_type_turbofish_are_not_call_claims() -> None:
    # `crate::x::object_drop_rest::<E>` resolves elsewhere and `Own::<T>` is a
    # type — neither is a claim about a fn THIS file should declare, so a sieve
    # that retired them would throw away sound guidance.
    for text in ("Use `crate::vt::object_drop_rest::<E>` here.",
                 "Annotate it `Own::<ErrorImpl>` explicitly."):
        assert admissible_guidance(
            {"guidance": text, "error_count": 3, "convicted_by": []},
            VTABLE_LAYOUT, evidence=NO_TESTS) == text


def test_lesson_written_against_hollow_callees_is_never_injected() -> None:
    # A body that COMPILED and then aborted the test binary, while the fns it
    # calls were still `unimplemented!()`, yields a retrospective explaining an
    # abort the stubs caused. All four of `error.rs`'s Error constructors earned
    # exactly that record on cluster `stable` (2026-08-09) — error_count 0,
    # blocked_on naming `construct`/`no_backtrace`/`error`/`object_drop_front`.
    prov = RetroProvenance.current("stable", VTABLE_LAYOUT)
    blocked = stamp_conviction(
        {"guidance": "Give drop_rest a no-op closure.", "error_count": 0},
        prov, NO_TESTS, BlockedOnStubs(frozenset({"construct", "no_backtrace"})))
    assert admissible_guidance(blocked, VTABLE_LAYOUT, evidence=NO_TESTS) == ""
    # Narrowing `blocked_on` as the blockers fill must NOT resurrect it: the
    # premise is provably false exactly then, and the stored text still argues
    # from it. Only a fresh retrospective clears the flag.
    blocked["blocked_on"] = []
    assert admissible_guidance(blocked, VTABLE_LAYOUT, evidence=NO_TESTS) == ""


def test_compiler_taught_lesson_survives_hollow_callees() -> None:
    # The same run's `error.rs::context::Error` record carried error_count 2 and
    # a non-empty blocked_on. A compile error is the compiler's verdict on the
    # body itself; a hollow sibling still type-checks, so it cannot have caused
    # it and must not cost the lesson.
    prov = RetroProvenance.current("stable", VTABLE_LAYOUT)
    taught = stamp_conviction(
        {"guidance": "Instantiate E explicitly; the Box deref makes it unsized.",
         "error_count": 2},
        prov, NO_TESTS, BlockedOnStubs(frozenset({"backtrace", "source"})))
    assert taught[GUIDANCE_FROM_HOLLOW_TREE] is False
    assert admissible_guidance(taught, VTABLE_LAYOUT, evidence=NO_TESTS)


def test_impossibility_guidance_expires_when_a_blocker_fills() -> None:
    """The live `context.rs::*impl*::StdErrorforContextError<C,Error>` record:
    the E0277 on `Some(&self.error)` was real, so `error_count` is 1 and every
    `error_count == 0` rule skips it — but the conclusion it drew ("the only
    body-only source is `None`") is a claim about the whole tree, refuted by
    `ErrorImpl::error` in the frozen layout. It recorded `provide` and `source`
    as blockers; `source` has since filled, so the claim was never re-put to
    the compiler and may not steer another re-fill. Four chain tests and six
    context tests had failed on that `None` since 2026-08-13."""
    from pipeline_core.domain.retro_gate import impossibility_claim_is_unfounded
    rec = {
        "guidance": "Change the `source` body to `None`; the frozen `C: "
                    "Display` bound provides no `Error` value, so the only "
                    "body-only source is `None`.",
        "error_count": 1,
        "blocked_on": ["provide", "source"],
    }
    assert impossibility_claim_is_unfounded(rec, frozenset({"provide"}))
    # While BOTH are still hollow the claim is answerable on its own terms and
    # stands — the release is bought by a blocker filling, not by time.
    assert not impossibility_claim_is_unfounded(
        rec, frozenset({"provide", "source"}))


def test_an_impossibility_claim_with_no_tree_premise_cannot_stand() -> None:
    """`error.rs::*impl*::Error` (GENUINELY_HARD, 5 retrospectives, convicting
    the whole crashed downcast family) says "The skeleton does not expose the
    VTable layout ... the chain cannot be traversed correctly" and records
    `blocked_on: []`. `struct ErrorVTable` is declared at
    `.skeleton/stable/src/error.rs:509` with `object_ref` and
    `object_downcast`. A claim that recorded no premise can never expire on its
    own evidence and would steer every re-fill for the life of the store."""
    from pipeline_core.domain.retro_gate import impossibility_claim_is_unfounded
    rec = {"guidance": "The skeleton does not expose the VTable layout, so the "
                       "chain cannot be traversed correctly with only this.",
           "error_count": 0, "blocked_on": []}
    assert impossibility_claim_is_unfounded(rec, frozenset())


def test_corrective_guidance_is_untouched_by_the_impossibility_rule() -> None:
    """The rule must bite on claims about the crate, not on lessons. The live
    store holds 26 records; only the three diagnosed as poisoned assert
    impossibility, and a blunt rule that also dropped `WRONG_DEREF_PATTERN`
    guidance would spend attempts re-learning what the compiler already
    taught."""
    from pipeline_core.domain.retro_gate import (
        asserts_impossibility,
        impossibility_claim_is_unfounded,
    )
    rec = {"guidance": "`this.deref()` resolves to the inherent `deref` on "
                       "`Ref` which returns `()`. Deref via the trait instead, "
                       "e.g. `unsafe { *this }`.",
           "error_count": 2, "blocked_on": []}
    assert not asserts_impossibility(rec["guidance"])
    assert not impossibility_claim_is_unfounded(rec, frozenset())


def test_negative_control_the_pre_fix_gate_admits_all_three() -> None:
    """Without the impossibility rule the three records pass every other gate:
    the layout sieve finds no field lie, `GUIDANCE_FROM_HOLLOW_TREE` is keyed
    on `error_count == 0` and two of them carry compile errors, and the
    evidence rule is skipped for the same reason. This is the state both runs
    were in, and it is why the `None` survived every re-fill."""
    from pipeline_core.domain.model_notes import layout_safe_guidance
    from pipeline_core.domain.retro_gate import (
        GUIDANCE_FROM_HOLLOW_TREE,
        ConvictionEvidence,
    )
    rec = {
        "guidance": "the compiler requires `E: core::error::Error` but the "
                    "FROZEN signature only grants `E: ext::StdError`. No body "
                    "can satisfy this. Do not retry this fn.",
        "error_count": 1, "blocked_on": [],
    }
    layout = "pub struct ContextError<C, E> { context: C, error: E }\n"
    assert not rec.get(GUIDANCE_FROM_HOLLOW_TREE)
    assert layout_safe_guidance(rec["guidance"], layout) == rec["guidance"]
    _ = ConvictionEvidence(frozenset())
