"""Admission rules for persisted retrospectives: what the sieve does with a
stored guidance string (counts, not just per-unit lines) and when a stored
`fully_exhausted` verdict may still convict a unit."""
from __future__ import annotations

import json

from pipeline_core.domain.retro_gate import (
    GATE_VERSION,
    GUIDANCE_FROM_HOLLOW_TREE,
    REGRESSION_SCOPED,
    ROUTE_VERSION,
    BlockedOnStubs,
    ConvictionEvidence,
    ConvictionStanding,
    GuidanceScreening,
    RetroGateTally,
    RetroGateVerdict,
    RetroProvenance,
    admissible_guidance,
    anchor_types_for_container,
    conviction_standing,
    evidence_for_record,
    judge_guidance,
    layout_fingerprint,
    regressed_tests_from_excerpt,
    regression_scoped,
    retired_guidance_line,
    screen_guidance,
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
    assert layout_fingerprint("struct Unit;") == layout_fingerprint("struct Unit;")
    unit_fp = layout_fingerprint("struct Unit;")
    braced_fp = layout_fingerprint("struct Unit { x: u8 }")
    assert unit_fp != braced_fp
    assert unit_fp != ""


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


def test_compile_only_conviction_stands_while_no_test_blames_the_unit() -> None:
    # Exhausting on compile errors names no tests. Read where the blame map
    # still names none, the verdict is about the same question and stands.
    prov = RetroProvenance.current("stable", LAYOUT)
    record = stamp_conviction({"fully_exhausted": True}, prov, NO_TESTS)
    assert record["convicted_by"] == []
    assert _standing(record, prov, NO_TESTS) is ConvictionStanding.CURRENT


# The record that held `anyhow`/`stable` at 83 for two complete traversals of
# its frontier (run `1246060-2339497b`, 2026-08-21), verbatim from
# `.phase_c_logs/retrospectives.json`: a `fully_exhausted` verdict convicted by
# NO test, stamped on a nesting diagnostic (the reply's own `fn source(&self)`
# line spliced inside the body), read under a frontier in which nine failing
# tests blamed the unit's `None` body.
_CONTEXT_ERROR_SOURCE = {
    "category": "WRONG_DEREF_PATTERN",
    "guidance": "The `error` field is a wrapper/smart-pointer, so `&self.error` "
                "yields a reference to the wrapper, not to the inner `dyn "
                "Error`. Build the source value by dereferencing the wrapper, "
                "e.g. `Some(self.error.as_ref())` / `Some(&*self.error)`",
    "retrospective_count": 10, "error_count": 3, "failing_test_count": 0,
    "best_failing_tests": 1, "fully_exhausted": True, "reset_attempts": 0,
    "convicted_by": [], "blocked_on": ["provide"],
    "last_error_preview": "# Errors in target file:\nerror: `self` parameter "
                          "is only allowed in associated functions\n   --> "
                          "src/context.rs:148:19",
}
_NINE = ConvictionEvidence(frozenset({
    "test_iter", "test_rev", "test_len", "test_clone", "test_root_cause",
    "test_anyhow_from_anyhow", "test_boxed_anyhow", "test_debug",
    "test_altdisplay"}))


def test_a_conviction_by_no_test_dies_once_a_test_blames_the_unit() -> None:
    # The blame map moved from "nothing" to "nine tests": `frozenset() <= tests`
    # was True in every context, and the impl route logged `skipped (prior
    # retro marked exhausted)` at both dispatches while every release path
    # waited on the score rise only this unit could deliver.
    prov = RetroProvenance.current("stable", LAYOUT)
    record = prov.stamp(dict(_CONTEXT_ERROR_SOURCE))
    assert _standing(record, prov, _NINE) is ConvictionStanding.STALE_EVIDENCE
    # Negative control, the same run's `src/error.rs::*impl*::ErrorImpl`: a
    # verdict convicted ON tests that still convict stands exactly as before.
    seven = ConvictionEvidence(frozenset({
        "test_anyhow_from_anyhow", "test_boxed_anyhow", "test_boxed_str",
        "test_clone", "test_iter", "test_len", "test_rev"}))
    error_impl = stamp_conviction(
        {"category": "WRONG_DEREF_PATTERN", "fully_exhausted": True,
         "error_count": 0, "failing_test_count": 7}, prov, seven)
    assert _standing(error_impl, prov, seven) is ConvictionStanding.CURRENT
    assert _standing(error_impl, prov, _NINE) is ConvictionStanding.STALE_EVIDENCE



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
        "blocked_on": ["from_boxed"],
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
                "error_count": 4, "convicted_by": ["test_whatever"],
                "blocked_on": ["construct"]}
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
             "error_count": 3, "convicted_by": [],
             "blocked_on": ["object_drop"]}
    assert admissible_guidance(sound, VTABLE_LAYOUT, evidence=NO_TESTS)


def test_path_qualified_and_type_turbofish_are_not_call_claims() -> None:
    # `crate::x::object_drop_rest::<E>` resolves elsewhere and `Own::<T>` is a
    # type — neither is a claim about a fn THIS file should declare, so a sieve
    # that retired them would throw away sound guidance.
    for text in ("Use `crate::vt::object_drop_rest::<E>` here.",
                 "Annotate it `Own::<ErrorImpl>` explicitly."):
        assert admissible_guidance(
            {"guidance": text, "error_count": 3, "convicted_by": [],
             "blocked_on": ["object_drop"]},
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


def test_corrective_guidance_with_no_tree_premise_is_retired_at_screen() -> None:
    """The impossibility function still ignores corrective; screen_guidance
    does not. Same record as the function-level control, judged where the
    other eight rules are judged. A missing cache entry is not a stored
    conviction."""
    from pipeline_core.domain.retro_gate import (
        NO_TREE_PREMISE,
        impossibility_claim_is_unfounded,
        tree_premise_absent,
    )
    rec = {"guidance": "`this.deref()` resolves to the inherent `deref` on "
                       "`Ref` which returns `()`. Deref via the trait instead, "
                       "e.g. `unsafe { *this }`.",
           "error_count": 2, "blocked_on": []}
    assert not impossibility_claim_is_unfounded(rec, frozenset())
    assert tree_premise_absent(rec)
    assert not tree_premise_absent({})
    assert not tree_premise_absent(None)
    verdict = screen_guidance(rec, LAYOUT, evidence=NO_TESTS)
    assert verdict.text == ""
    assert verdict.retired_by == NO_TREE_PREMISE
    line = retired_guidance_line("src/error.rs::deref::Error", rec, verdict)
    assert NO_TREE_PREMISE in line
    assert "no tree premise" in line


def test_impossibility_with_no_tree_premise_still_names_impossibility() -> None:
    """Ninth is after impossibility, so the more specific class wins."""
    rec = {"guidance": "The skeleton does not expose the VTable layout, so the "
                       "chain cannot be traversed correctly with only this.",
           "error_count": 0, "blocked_on": []}
    verdict = screen_guidance(rec, LAYOUT, evidence=NO_TESTS)
    assert verdict.text == ""
    assert verdict.retired_by == "impossibility_unfounded"


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


# ---------------------------------------------------------------------------
# The honoured-record log line: the observability floor for silently-steering
# guidance. A record with no log signature steered the ContextError `source`
# body to `None` for two days (2026-08-13/14) and was visible only through
# its downstream test failures.

def test_honoured_guidance_line_names_record_and_premise() -> None:
    from pipeline_core.domain.retro_gate import honoured_guidance_line
    rec = {"category": "TRAIT_BOUND_VIOLATION", "retrospective_count": 2,
           "blocked_on": ["provide", "source"]}
    line = honoured_guidance_line("src/context.rs::source", rec,
                                  "Change the body to X because Y")
    assert "[phase-c/retro-honoured]" in line
    assert "src/context.rs::source" in line
    assert "TRAIT_BOUND_VIOLATION" in line
    assert "blocked_on[2]" in line
    assert "Change the body to X" in line


def test_honoured_guidance_line_marks_a_missing_tree_premise() -> None:
    from pipeline_core.domain.retro_gate import honoured_guidance_line
    line = honoured_guidance_line("u", {"category": "X"}, "do the thing")
    assert "NO tree premise" in line


def test_no_line_when_nothing_was_honoured() -> None:
    from pipeline_core.domain.retro_gate import honoured_guidance_line
    assert honoured_guidance_line("u", {"category": "X"}, "") == ""


def test_futility_suspension_withholds_guidance_for_the_stamping_cluster_only() -> None:
    """The futility escalation measured that rewrites of the unit change
    nothing for its tests — the exact question the guidance advises on. The
    live instance (run `2773540-e47c3754`, 2026-08-17): a WRONG_TYPE_CHOICE
    record steered 14 rounds of `ChainState`-as-`Vec` bodies against a tree
    whose `ChainState::Buffered` holds `vec::IntoIter`, while the escalation
    declared the accessor futile in the same sweep. The suspension is
    cluster-scoped (the store is shared; the futility was measured under one
    cluster's tree) and a caller that does not know its cluster is never
    suppressed — cross-cluster reuse is what `conviction_standing` forbids."""
    from pipeline_core.domain.retro_gate import (
        FUTILITY_SUSPENDED,
        futility_suspension_applies,
    )
    # `error_count` 1 so the evidence rule (compiler-taught lessons survive a
    # moved blame map) admits it everywhere — the ONLY discriminator under
    # test is the suspension stamp.
    rec = {"guidance": "In the Buffered arm keep the storage as a Vec.",
           "error_count": 1,
           "blocked_on": ["next_back"],
           FUTILITY_SUSPENDED: {"cluster": "stable", "tests": ["test_rev"]}}
    assert futility_suspension_applies(rec, "stable")
    assert not futility_suspension_applies(rec, "nightly")
    assert not futility_suspension_applies(rec, "")
    assert admissible_guidance(rec, "", evidence=NO_TESTS,
                               cluster="stable") == ""
    assert admissible_guidance(rec, "", evidence=NO_TESTS,
                               cluster="nightly") != ""
    # NEGATIVE CONTROL — pre-fix shape: the same record with no stamp (or a
    # caller passing no cluster) flows exactly as before.
    assert admissible_guidance(rec, "", evidence=NO_TESTS) != ""
    unstamped = {"guidance": rec["guidance"], "error_count": 1,
                 "blocked_on": rec["blocked_on"]}
    assert admissible_guidance(unstamped, "", evidence=NO_TESTS,
                               cluster="stable") != ""


# ---------------------------------------------------------------------------
# A do-nothing body prescribed for a unit that answers for failing tests is
# not a lesson. The live record (`StdErrorforContextError<C,Error>`, 9
# rounds) told the model "replace the body with `None`" and was honoured at
# every dispatch of the crawl's central unit (run `626784-4b7c38ba`,
# 2026-08-20); it carried `best_failing_tests: 1` the whole time.

_SEVERED_SOURCE_RECORD = {
    "category": "TRAIT_BOUND_VIOLATION",
    "guidance": (
        "The frozen where clause is only `C: Display`, so `self.error` "
        "cannot be used as a `&(dyn StdError + 'static)` and "
        "`self.error.provide` cannot be proved to exist. Avoid the unfrozen "
        "`Error: StdError` requirement by not touching `self.error`. For "
        "`source` replace the body with `None` instead of `Some(&self.error)`, "
        "and for `provide` replace the body with a no-op such as "
        "`let _ = request;`. This makes both methods type-check under the "
        "frozen `C: Display` bound alone."),
    "retrospective_count": 9, "error_count": 3, "failing_test_count": 0,
    "best_compile_errors": 0, "best_failing_tests": 1,
    "convicted_by": [], "blocked_on": ["provide"],
}

_CONTEXT_RS = """\
impl<C> StdError for ContextError<C, Error>
where
    C: Display,
{
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        None
    }
    #[cfg(error_generic_member_access)]
    fn provide<'a>(&'a self, request: &mut Request<'a>) {
        ::core::unimplemented!()
    }
}
"""


def test_degenerate_prescription_for_a_unit_with_failing_tests_is_retired() -> None:
    from pipeline_core.domain.retro_gate import (
        TEST_SACRIFICING_PRESCRIPTION,
        ConvictionEvidence,
        asserts_impossibility,
        prescribes_degenerate_body,
        prescription_sacrifices_tests,
        retired_guidance_line,
        screen_guidance,
    )
    rec = dict(_SEVERED_SOURCE_RECORD)
    # The class the impossibility rule cannot see: no marker, phrased as a fix.
    assert not asserts_impossibility(rec["guidance"])
    assert prescribes_degenerate_body(rec["guidance"])
    assert prescription_sacrifices_tests(rec)
    verdict = screen_guidance(rec, _CONTEXT_RS,
                             evidence=ConvictionEvidence(frozenset()))
    assert verdict.text == ""
    assert verdict.retired_by == TEST_SACRIFICING_PRESCRIPTION
    line = retired_guidance_line(
        "src/context.rs::*impl*::StdErrorforContextError<C,Error>", rec, verdict)
    assert line.startswith("[phase-c/retro-retired] src/context.rs::*impl*::")
    assert "rounds=9" in line and "best_failing_tests=1" in line
    assert "replace the body with `None`" in line


def test_degenerate_prescription_without_test_evidence_is_left_alone() -> None:
    """No failing test on the record, none stamped, none live: "make it
    type-check" may be all there is to ask, and the rule must not guess."""
    from pipeline_core.domain.retro_gate import (
        ConvictionEvidence,
        prescription_sacrifices_tests,
        retired_guidance_line,
        screen_guidance,
    )
    rec = dict(_SEVERED_SOURCE_RECORD, best_failing_tests=0,
               failing_test_count=0, convicted_by=[])
    assert not prescription_sacrifices_tests(rec)
    verdict = screen_guidance(rec, _CONTEXT_RS,
                             evidence=ConvictionEvidence(frozenset()))
    assert verdict.text and verdict.retired_by == ""
    assert retired_guidance_line("k", rec, verdict) == ""
    # Live evidence alone is enough: the unit answers for a red test NOW.
    assert prescription_sacrifices_tests(
        rec, ConvictionEvidence(frozenset({"test_iter"})))
    assert screen_guidance(
        rec, _CONTEXT_RS, evidence=ConvictionEvidence(frozenset({"test_iter"})),
    ).text == ""
    assert not prescription_sacrifices_tests(None)
    assert not ConvictionEvidence(frozenset({"t"})).still_supports(None)
    assert not ConvictionEvidence(frozenset()).still_supports({"convicted_by": "not-a-list"})


def test_corrective_guidance_without_a_degenerate_body_is_untouched() -> None:
    """The `ErrorImpl` GENUINELY_HARD record (21 rounds, convicted by the seven
    chain/boxed tests) prescribes nothing degenerate — it argues about `Ref`
    deref — and must reach the prompt on this rule's account (its own defect
    is a premise the tree refutes, a different gate)."""
    from pipeline_core.domain.retro_gate import (
        ConvictionEvidence,
        prescribes_degenerate_body,
        prescription_sacrifices_tests,
    )
    rec = {
        "guidance": "The `ptr::Ref<'_, ErrorImpl>` parameter cannot be "
                    "dereferenced with `*`; the prior `let r = &*this` deref "
                    "pattern was already tried and still yields E0614.",
        "best_failing_tests": 0, "failing_test_count": 0,
        "convicted_by": ["test_clone", "test_iter", "test_len", "test_rev"],
    }
    assert not prescribes_degenerate_body(rec["guidance"])
    assert not prescription_sacrifices_tests(
        rec, ConvictionEvidence(frozenset({"test_iter"})))


def test_a_blocker_the_cluster_never_compiles_does_not_keep_a_claim_alive() -> None:
    """`provide` is textually `unimplemented!()` in `context.rs` and cfg-gated
    out of the `stable` build, so it can never fill there. An impossibility
    claim blocked only on it would stand for the life of the store; handed
    the cluster's cfg-dead names, the premise check treats the blocker as
    cleared. Negative control: the same record without `cfg_dead` stands."""
    from pipeline_core.domain.retro_gate import ConvictionEvidence, screen_guidance
    rec = {
        "guidance": "The frozen `C: Display` bound provides no `Error` value, "
                    "so the only body-only source is `None`. Do not retry.",
        "error_count": 1, "blocked_on": ["provide"],
        "best_failing_tests": 0, "failing_test_count": 0, "convicted_by": [],
    }
    ev = ConvictionEvidence(frozenset())
    standing = screen_guidance(rec, _CONTEXT_RS, evidence=ev)
    assert standing.text  # pre-fix shape: `provide` is still hollow, claim stands
    released = screen_guidance(rec, _CONTEXT_RS, evidence=ev,
                              cfg_dead=frozenset({"provide"}))
    assert released.text == "" and released.retired_by == "impossibility_unfounded"


# --------------------------------------------------------------------------- #
# The regression-derived prescription.
# --------------------------------------------------------------------------- #
# Recorded artefact: `tmp/anyhow/.phase_c_logs/retrospectives.json` key
# `src/ensure.rs::*impl*::BothDebugfor(A,B)`, read 2026-08-26 (cluster
# `stable`, layout_fp f95c2372; earned by the per-block route, one retro
# round). `error_count 0, failing_test_count 13, convicted_by []`, the preview
# cut at 240 characters mid-list. Its guidance prescribes the removal of the
# 40-byte overflow fallback `test_trailer` / `test_whitespace` pin.
_LIVE_BOTHDEBUG_KEY = "src/ensure.rs::*impl*::BothDebugfor(A,B)"
_LIVE_BOTHDEBUG_RECORD_JSON = r'''{
 "category": "WRONG_TYPE_CHOICE",
 "guidance": "Drop the fixed 40-byte SmallBuf and format into an allocating Write so the debug output is never truncated; e.g. let mut s = alloc::string::String::new(); core::fmt::write(&mut s, format_args!(\"{} ({:?} vs {:?})\", msg, self.0, self.1)).unwrap(); return crate::Error::msg(s); This keeps the full \"{:?}\" rendering for self.0/self.1 and avoids the overflow fallback to crate::Error::msg(msg) that caused the regressions.",
 "signal": "ok",
 "retrospective_count": 1,
 "error_count": 0,
 "failing_test_count": 13,
 "best_compile_errors": 0,
 "best_failing_tests": 13,
 "fully_exhausted": false,
 "last_error_preview": "This compiled, but it REGRESSED previously-passing test(s): ['test_as', 'test_atom', 'test_high_precedence_binary_operator', 'test_if', 'test_loop', 'test_low_precedence_binary_operator', 'test_macro', 'test_match', 'test_pat', 'test_path',",
 "cluster": "stable",
 "layout_fp": "f95c2372",
 "gate_ver": "comparative_2026_07",
 "route_ver": "leakproof_revert_2026_07",
 "convicted_by": [],
 "guidance_from_hollow_tree": false,
 "blocked_on": [],
 "futility_suspended": {
  "cluster": "stable",
  "tests": [
   "test_altdebug",
   "test_boxed_str",
   "test_downcast",
   "test_downcast_high",
   "test_downcast_low",
   "test_downcast_mid",
   "test_trailer"
  ]
 }
}'''
# `tmp/anyhow/.skeleton/stable/src/ensure.rs` lines 1-50 (2026-08-26): every
# item before `macro_rules! __parse_ensure` — the declarations the layout
# rule reads (`struct Buf { bytes: [MaybeUninit<u8>; 40], written: usize }`).
_LIVE_ENSURE_LAYOUT = "#![allow(unused, dead_code, non_snake_case, clippy::all)]\nuse crate::Error;\nuse alloc::string::String;\nuse core::fmt::{self, Debug, Write};\nuse core::mem::MaybeUninit;\nuse core::ptr;\nuse core::slice;\nuse core::str;\n#[doc(hidden)]\npub trait BothDebug {\n    fn __dispatch_ensure(self, msg: &'static str) -> Error;\n}\nimpl<A, B> BothDebug for (A, B)\nwhere\n    A: Debug,\n    B: Debug,\n{\n    fn __dispatch_ensure(self, msg: &'static str) -> Error {\n        ::core::unimplemented!()\n    }\n}\n#[doc(hidden)]\npub trait NotBothDebug {\n    fn __dispatch_ensure(self, msg: &'static str) -> Error;\n}\nimpl<A, B> NotBothDebug for &(A, B) {\n    fn __dispatch_ensure(self, msg: &'static str) -> Error {\n        ::core::unimplemented!()\n    }\n}\nstruct Buf {\n    bytes: [core::mem::MaybeUninit<u8>; 40],\n    written: usize,\n}\nimpl Buf {\n    fn new() -> Self {\n        ::core::unimplemented!()\n    }\n    fn as_str(&self) -> &str {\n        ::core::unimplemented!()\n    }\n}\nimpl Write for Buf {\n    fn write_str(&mut self, s: &str) -> fmt::Result {\n        ::core::unimplemented!()\n    }\n}\nfn render(msg: &'static str, lhs: &dyn Debug, rhs: &dyn Debug) -> Error {\n    ::core::unimplemented!()\n}\n"
_BOTHDEBUG_CONTAINER = "impl<A, B> BothDebug for (A, B)"


def _live_bothdebug() -> dict:
    rec = json.loads(_LIVE_BOTHDEBUG_RECORD_JSON)
    assert rec["error_count"] == 0 and rec["convicted_by"] == []
    return rec


def _screen(rec: dict, tests: frozenset[str], *, cluster: str = "stable") -> GuidanceScreening:
    return screen_guidance(
        rec, _LIVE_ENSURE_LAYOUT, evidence=ConvictionEvidence(tests),
        anchor_types=anchor_types_for_container(_BOTHDEBUG_CONTAINER),
        cluster=cluster)


def test_regressed_tests_parse_the_regression_head_and_survive_the_preview_cut() -> None:
    # The live preview: the list is cut mid-name after `'test_path',` — the
    # ten complete names before the cut are the answer.
    assert regressed_tests_from_excerpt(_live_bothdebug()["last_error_preview"]) == (
        "test_as", "test_atom", "test_high_precedence_binary_operator",
        "test_if", "test_loop", "test_low_precedence_binary_operator",
        "test_macro", "test_match", "test_pat", "test_path")
    # The full render (`render_regression_detail`): closed list, trailing
    # sentence, qualified names, a double-quoted repr for a name with `'`.
    full = ("This compiled, but it REGRESSED previously-passing test(s): "
            "['chain::test_rev', \"it's\", 'test_as'] (cluster pass count "
            "93\u219291).\nKeep those tests passing while fixing the target.")
    assert regressed_tests_from_excerpt(full) == ("chain::test_rev", "it's", "test_as")
    # Not a regression verdict: a compile excerpt, a U2 failure, nothing.
    assert regressed_tests_from_excerpt("error[E0277]: the trait bound") == ()
    assert regressed_tests_from_excerpt("This body COMPILES but its attributed "
                                        "test(s) below still FAIL.") == ()
    assert regressed_tests_from_excerpt("") == ()


def test_stamp_conviction_writes_the_regressed_tests() -> None:
    prov = RetroProvenance(cluster="stable", layout_fp="x")
    rec = stamp_conviction({"error_count": 0}, prov, NO_TESTS,
                           regressed=("test_b", "test_a"))
    assert rec["regressed"] == ["test_a", "test_b"]
    # Always written: a compile-taught record reads as "not this class",
    # never as a legacy record.
    assert stamp_conviction({"error_count": 2}, prov, NO_TESTS)["regressed"] == []


def test_live_bothdebug_record_is_regression_scoped_for_the_tests_it_prescribes_against() -> None:
    """The plan's fixture: under the new screen, with evidence
    `{test_trailer}`, the live record is withheld `regression_scoped`."""
    rec = _live_bothdebug()
    # Measured under `stable`: with the suspension in place the FIRST rule
    # answers, and that is the control for the ordering (below). Stripped
    # here so the verdict is the regression rule's.
    rec.pop("futility_suspended")
    stamped = stamp_conviction(
        dict(rec), RetroProvenance(rec["cluster"], rec["layout_fp"]),
        ConvictionEvidence(frozenset()),
        regressed=regressed_tests_from_excerpt(rec["last_error_preview"]))
    trailer = frozenset({"test_trailer"})
    assert regression_scoped(stamped, ConvictionEvidence(trailer))
    assert _screen(stamped, trailer) == GuidanceScreening("", REGRESSION_SCOPED)
    assert admissible_guidance(
        stamped, _LIVE_ENSURE_LAYOUT, evidence=ConvictionEvidence(trailer),
        anchor_types=anchor_types_for_container(_BOTHDEBUG_CONTAINER),
        cluster="stable") == ""
    # The retirement has a log signature naming the class and the lesson.
    line = retired_guidance_line(_LIVE_BOTHDEBUG_KEY, stamped,
                                 _screen(stamped, trailer))
    assert line.startswith(f"[phase-c/retro-retired] {_LIVE_BOTHDEBUG_KEY}: ")
    assert "retro guidance withheld (regression_scoped)" in line
    assert "10 test(s)" in line and "SmallBuf" in line
    # On a re-fill FOR a regressed test the lesson is in scope, and the
    # leaf rule reconciles the frontier's qualified names.
    assert not regression_scoped(stamped, ConvictionEvidence(frozenset({"test_as"})))
    assert not regression_scoped(stamped, ConvictionEvidence(frozenset({"ensure::test_as"})))
    # NEGATIVE CONTROL 1: the record as it sits in the store — no
    # `regressed` stamp — with a named blocker so the ninth rule does
    # not fire, falls to today's rule, `stale_evidence` (`error_count 0`,
    # `convicted_by []`, a test on the unit), and is not this class.
    # That is what makes the stamp load-bearing.
    rec_named = dict(rec)
    rec_named["blocked_on"] = ["render"]
    assert not regression_scoped(rec_named, ConvictionEvidence(trailer))
    assert _screen(rec_named, trailer).retired_by == "stale_evidence"
    assert _screen(rec_named, trailer).text == ""
    # NEGATIVE CONTROL 2: the suspension outranks it where it applies.
    assert _screen(_live_bothdebug(), trailer).retired_by == "futility_suspended"
    # NEGATIVE CONTROL 3: a compiler-taught record (`error_count 2`) is
    # untouched — a compile excerpt carries no regression head, so its
    # `regressed` is [] and the lesson is honoured (the layout rule has no
    # field claim to contradict in this text).
    taught = dict(stamped)
    taught["error_count"] = 2
    taught["regressed"] = list(regressed_tests_from_excerpt("error[E0277]: …"))
    taught["blocked_on"] = ["render"]
    assert not regression_scoped(taught, ConvictionEvidence(trailer))
    verdict = _screen(taught, trailer)
    assert verdict.retired_by == ""
    assert verdict.text.startswith("Drop the fixed 40-byte SmallBuf")
    # A legacy record missing the key entirely, or with a non-list, is not
    # this class either.
    assert not regression_scoped({"regressed": "test_as"}, ConvictionEvidence(trailer))
    assert not regression_scoped(None, ConvictionEvidence(trailer))


def test_compiled_record_reads_under_the_tests_the_unit_answers_for() -> None:
    """`convicted_by: []` on an `error_count 0` record supports nothing once
    the unit has ANY attributed test, escalated included — the reading the
    call sites now take (`evidence_for_record`). The pre-change read used
    the convicting set alone, under which the live BothDebug record was
    supported by `[] <= {}` while thirteen escalated tests sat on it."""
    prov = RetroProvenance(cluster="stable", layout_fp="x")
    compiled = stamp_conviction(
        {"error_count": 0, "fully_exhausted": True}, prov, NO_TESTS)
    convicting = ConvictionEvidence(frozenset())            # nothing convicts
    answers = ConvictionEvidence(frozenset({"test_trailer"}))  # escalated
    # NEGATIVE CONTROL: the convicting-only reading supports the record.
    assert convicting.still_supports(compiled)
    assert conviction_standing(compiled, prov, convicting) is ConvictionStanding.CURRENT
    # The answered-for reading does not.
    assert not answers.still_supports(compiled)
    chosen = evidence_for_record(compiled, convicting=convicting, answers=answers)
    assert chosen is answers
    assert conviction_standing(compiled, prov, chosen) is ConvictionStanding.STALE_EVIDENCE
    # A compile-taught record keeps the convicting reading, exactly as before.
    taught = stamp_conviction({"error_count": 3, "fully_exhausted": True}, prov, NO_TESTS)
    assert evidence_for_record(taught, convicting=convicting, answers=answers) is convicting
    assert conviction_standing(taught, prov, convicting) is ConvictionStanding.CURRENT
    # No record reads as compiled (the wider, safer default).
    assert evidence_for_record(None, convicting=convicting, answers=answers) is answers
    # And a compiled record stamped with the wider set stands under it.
    wide = stamp_conviction({"error_count": 0, "fully_exhausted": True}, prov, answers)
    assert wide["convicted_by"] == ["test_trailer"]
    assert conviction_standing(
        wide, prov, evidence_for_record(wide, convicting=convicting, answers=answers)
    ) is ConvictionStanding.CURRENT


# --- a conviction earned from a crash the tree's stubs decided ---------------
#
# Run `2585062-494e630e` (2026-09-07/08), cluster `stable`, fresh stub tree:
# every route wrote the textbook `Own::new`, the test process aborted in the
# still-stubbed `vtable()` reached through `Error::drop`, the gates reverted
# it, and the retro store convicted the unit on the abort signature. The two
# live records, copied from `.phase_c_logs/retrospectives.json` at 04:50 UTC
# (guidance trimmed): the per-fn one is `fully_exhausted` with `blocked_on`
# EMPTY (the body never names the stub it reaches through `Drop`) and
# `guidance_from_hollow_tree: false`; the impl one carries the CRASH head.
_LIVE_PER_FN_ABORT = {
    "category": "WRONG_TYPE_CHOICE",
    "guidance": "Move the Box into an owned raw pointer with Box::into_raw and "
                "build a valid NonNull from it without ever forming a NonNull "
                "from a null/dangling raw pointer",
    "signal": "ok", "retrospective_count": 2, "error_count": 0,
    "failing_test_count": 29, "best_compile_errors": 0,
    "best_failing_tests": 29, "fully_exhausted": True,
    "last_error_preview": "Your body ABORTED the test process. These test(s) "
                          "produced a verdict BEFORE your change and produced "
                          "no output at all after it:\nboxed_error_source_none",
    "cluster": "stable", "convicted_by": ["boxed_error_source_none", "test_drop"],
    "regressed": [],
    "convictor_entry": {"boxed_error_source_none": [
        "src/error.rs::construct", "src/ptr.rs::new"]},
    "guidance_from_hollow_tree": False, "blocked_on": [],
}
_LIVE_IMPL_CRASH = {
    "category": "WRONG_TYPE_CHOICE",
    "guidance": "for the frozen `Own<T>` API the constructor should just adopt "
                "the supplied raw pointer directly",
    "signal": "ok", "retrospective_count": 2, "error_count": 0,
    "failing_test_count": 4, "fully_exhausted": False,
    "last_error_preview": "This made test binaries ['tests/context_gen_1.rs', "
                          "'tests/context_gen_2.rs'] CRASH before reporting "
                          "results (e.g. segfault from bad unsafe code)",
    "cluster": "stable", "convicted_by": [], "regressed": [],
    "guidance_from_hollow_tree": True,
    "blocked_on": ["boxed", "by_ref", "cast", "new"],
}


def test_the_live_crash_earned_records_are_retired_at_read_time() -> None:
    from pipeline_core.domain.retro_gate import (
        CRASH_EARNED,
        crash_earned_conviction,
    )
    assert crash_earned_conviction(_LIVE_PER_FN_ABORT)
    assert crash_earned_conviction(_LIVE_IMPL_CRASH)
    # The per-fn record's exhaustion and lesson are both withheld — and by
    # THIS rule, not by the entry premise, which flips back to "current"
    # the moment the gate keeps the correct body.
    v = screen_guidance(_LIVE_PER_FN_ABORT, VTABLE_LAYOUT, evidence=NO_TESTS)
    assert v.text == "" and v.retired_by == CRASH_EARNED
    line = retired_guidance_line("src/ptr.rs::new::Own<T>", _LIVE_PER_FN_ABORT, v)
    assert line.startswith("[phase-c/retro-retired] src/ptr.rs::new::Own<T>")
    assert "crash/abort" in line and CRASH_EARNED in line
    # NEGATIVE CONTROLS: a compile-taught record with the same head is the
    # compiler's verdict; a compiled record with a test-failure head is not
    # this class; an empty record is nothing to retire.
    assert not crash_earned_conviction({**_LIVE_PER_FN_ABORT, "error_count": 2})
    assert not crash_earned_conviction({
        **_LIVE_PER_FN_ABORT,
        "last_error_preview": "This compiled, but these tests still FAIL: ['a']"})
    assert not crash_earned_conviction({})
    assert not crash_earned_conviction(None)


def test_stamp_conviction_records_the_crash_sites_and_never_exhausts_on_them() -> None:
    # Write-time: the gate located stub panics outside the unit in the
    # aborted binaries. The record is hollow-tree guidance, not exhausted,
    # and carries the sites — checkable, and read without the prose.
    from pipeline_core.domain.retro_gate import CRASH_STUB_SITES
    prov = RetroProvenance.current("stable", VTABLE_LAYOUT)
    rec = stamp_conviction(
        {"guidance": "Adopt the raw pointer directly.", "error_count": 0,
         "fully_exhausted": True,
         "last_error_preview": "Your body ABORTED the test process."},
        prov, NO_TESTS, BlockedOnStubs(frozenset()),
        crash_stub_sites=[("src/error.rs", 669), ("src/error.rs", 553)])
    assert rec[CRASH_STUB_SITES] == ["src/error.rs:553", "src/error.rs:669"]
    assert rec[GUIDANCE_FROM_HOLLOW_TREE] is True
    assert rec["fully_exhausted"] is False
    assert admissible_guidance(rec, VTABLE_LAYOUT, evidence=NO_TESTS) == ""
    # A stamped-EMPTY record is the post-stamp segfault: the failure was a
    # crash and the gate located no stub panic — the body's own, honoured
    # exactly as before (the prose head no longer decides).
    seg = stamp_conviction(
        {"guidance": "Do not read past the allocation.", "error_count": 0,
         "fully_exhausted": True,
         "last_error_preview": "Your body ABORTED the test process."},
        prov, NO_TESTS, BlockedOnStubs(frozenset()), crash_stub_sites=[])
    assert seg[CRASH_STUB_SITES] == [] and seg["fully_exhausted"] is True
    assert seg[GUIDANCE_FROM_HOLLOW_TREE] is False
    from pipeline_core.domain.retro_gate import crash_earned_conviction
    assert not crash_earned_conviction(seg)
    # A non-crash failure writes no key at all (not "this class", not legacy).
    plain = stamp_conviction(
        {"guidance": "x", "error_count": 0,
         "last_error_preview": "This compiled, but these tests still FAIL"},
        prov, NO_TESTS, BlockedOnStubs(frozenset()))
    assert CRASH_STUB_SITES not in plain
    # Compile-taught: the sites are recorded but the compiler's verdict
    # stands — no hollow flag, exhaustion untouched.
    taught = stamp_conviction(
        {"guidance": "y", "error_count": 2, "fully_exhausted": True},
        prov, NO_TESTS, BlockedOnStubs(frozenset()),
        crash_stub_sites=[("src/error.rs", 669)])
    assert taught["fully_exhausted"] is True
    assert taught[GUIDANCE_FROM_HOLLOW_TREE] is False
