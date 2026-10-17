"""The proven-body ledger's domain rules: identity, the gate, the promotion.

The record this exists against: `.phase_c_logs/retrospectives.json` holds 39
records over 19 fields and not one of them is a positive claim, so every
deliberate destruction re-derived proven bodies through the model. Run
`259637-36db9aad` (2026-08-07) reverted 56 of 178 filled bodies and spent nine
hours, 148 landed fills and +5 tests without an exit — with the bodies on disk
the whole time.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pipeline_core.domain.assertion_position import KeyedVerdicts, TestKey
from pipeline_core.domain.body_ledger import (
    GATE_VERSION,
    BodyState,
    LayoutDep,
    LedgerEntry,
    LedgerKey,
    ReportKeep,
    ReseedPlan,
    SweepView,
    authored_layout_fp,
    body_layout_deps,
    body_sha,
    classify,
    latest_by_unit,
    latest_crash_refutation,
    oracle_fingerprint,
    plan_reseed,
    rebuy_by_grade,
    rebuy_hits,
    reseed_allowed,
    rows_for_units,
    sig_sha_of,
    standing_on_tree,
    strip_comments,
    text_sha,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.retro_gate import layout_fingerprint

_KEY = LedgerKey(project="anyhow", cluster="stable", file="src/error.rs",
                 fn_name="new")


_ORACLE = "oracle-A"


def _entry(**kw: object) -> LedgerEntry:
    base = {
        "key": _KEY, "body_sha": body_sha(("Error::from(e)",)),
        "bodies": ("Error::from(e)",), "sig_sha": sig_sha_of(("pub fn new",)),
        "layout_fp": "fp-original", "state": BodyState.PROVEN,
        "oracle_fp": _ORACLE,
    }
    base.update(kw)
    return LedgerEntry(**base)      # type: ignore[arg-type]


def _allowed(entry: LedgerEntry, **kw):  # type: ignore[no-untyped-def]
    """`reseed_allowed` under the oracle the fixture rows carry, so the
    layout/signature/state tests below keep asking their own question."""
    kw.setdefault("oracle_fp", _ORACLE)
    return reseed_allowed(entry, **kw)


def _rebuy(*a, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("oracle_fp", _ORACLE)
    return rebuy_hits(*a, **kw)


# --- identity ---------------------------------------------------------------

def test_a_row_round_trips_and_a_corrupt_one_costs_one_entry_not_the_run() -> None:
    e = _entry(layout_deps=(LayoutDep("ErrorImpl", "struct",
                                      "struct ErrorImpl { x: u8 }",
                                      "src/error.rs"),))
    back = LedgerEntry.from_dict(e.to_dict())
    assert back == e
    bads: tuple[object, ...] = (None, [], {}, {"project": "anyhow"},
                                {**e.to_dict(), "state": "hopeful"})
    for bad in bads:
        assert LedgerEntry.from_dict(bad) is None


def test_the_body_identity_folds_whitespace_but_the_stored_body_does_not() -> None:
    """The fold is an IDENTITY. Splicing it would be a defect: a body holding a
    `//` comment, collapsed onto one line, comments out its own remainder."""
    raw = "// take the fast path\n    Error::from(e)\n"
    assert body_sha((raw,)) == body_sha(("// take the fast path Error::from(e)",))
    e = _entry(bodies=(raw,), body_sha=body_sha((raw,)))
    assert LedgerEntry.from_dict(e.to_dict()) is not None
    assert e.bodies == (raw,), "the raw text is what goes back on the tree"


# --- the layout gate --------------------------------------------------------

_BASE = """
// a comment that must not matter
pub struct ErrorImpl { pub backtrace: Backtrace, pub obj: Own }

pub enum ChainState { Linked, Buffered }

pub trait CastTo { type Target; fn cast(&self) -> Self::Target; }

pub type Result<T> = core::result::Result<T, Error>;

pub fn construct(x: u8) -> Error { unimplemented!() }
"""


def test_the_gate_moves_on_all_five_authored_kinds() -> None:
    """t1.5 authors `struct | enum | type | trait | fn`
    (`domain/authored_layout.py`). A body re-seeded across a move in ANY of
    them does not compile, so the fingerprint has to see all five."""
    moves = {
        "struct": _BASE.replace("pub backtrace: Backtrace,", ""),
        "enum": _BASE.replace("Linked, Buffered", "Linked, Buffered, Empty"),
        "trait": _BASE.replace("fn cast(&self) -> Self::Target;", ""),
        "type": _BASE.replace("core::result::Result<T, Error>",
                              "core::result::Result<T, Box<Error>>"),
        "fn": _BASE.replace("pub fn construct(x: u8)",
                            "pub fn construct(x: u8, y: u8)"),
    }
    for kind, moved in moves.items():
        assert authored_layout_fp(moved) != authored_layout_fp(_BASE), (
            f"a moved {kind} must move the fingerprint")


def test_negative_control_the_retro_gates_fingerprint_misses_four_of_them() -> None:
    """Why the ledger mints its own digest instead of sharing
    `retro_gate.layout_fingerprint`: that one digests `struct NAME` items and
    their field sets and nothing else, so four of the five kinds move under it
    unchanged — and a file that declares no struct at all digests to
    `d41d8cd9`, md5 of the empty string, which would re-seed every body
    unconditionally."""
    for moved in (_BASE.replace("Linked, Buffered", "Linked, Buffered, Empty"),
                  _BASE.replace("fn cast(&self) -> Self::Target;", ""),
                  _BASE.replace("core::result::Result<T, Error>",
                                "core::result::Result<T, Box<Error>>"),
                  _BASE.replace("pub fn construct(x: u8)",
                                "pub fn construct(x: u8, y: u8)")):
        assert layout_fingerprint(moved) == layout_fingerprint(_BASE), (
            "the control must reproduce the blindness the ledger's gate fixes")
    # `backtrace.rs` and `macros.rs` in the live skeleton declare no struct at
    # all, so under the control every struct-less file has the SAME digest —
    # md5 of the empty string — whatever it declares.
    assert (layout_fingerprint("fn free() {}")
            == layout_fingerprint("pub enum X { A }") == "d41d8cd9")
    assert authored_layout_fp("fn free() {}") != authored_layout_fp(
        "pub enum X { A }")


def test_comments_and_indentation_are_not_a_layout_move() -> None:
    """A refused re-seed costs a re-derivation, so the gate is allowed to be
    strict — but not so strict that reformatting counts as a re-author."""
    assert authored_layout_fp(
        _BASE.replace("// a comment that must not matter", "// something else")
    ) == authored_layout_fp(_BASE)
    assert authored_layout_fp(
        _BASE.replace("pub struct ErrorImpl {", "pub struct ErrorImpl\n{")
    ) == authored_layout_fp(_BASE)


def test_a_body_depends_on_the_items_it_names() -> None:
    items = {"ErrorImpl": ("struct", "struct ErrorImpl { x: u8 }"),
             "Own": ("struct", "struct Own;"),
             "Buf": ("struct", "struct Buf;")}
    deps = body_layout_deps("let e: ErrorImpl = own.into();",
                            "fn new(own: Own) -> Self", items)
    assert {d.name for d in deps} == {"ErrorImpl", "Own"}, (
        "Buf is not referenced, so a move in Buf cannot break this body")


def test_the_gate_refuses_a_moved_layout_and_accepts_a_moved_neighbour() -> None:
    dep = LayoutDep("ErrorImpl", "struct", "struct ErrorImpl { x: u8 }",
                    "src/error.rs")
    e = _entry(layout_deps=(dep,))
    assert _allowed(e, file_fp="fp-original",
                          sig_sha=e.sig_sha).allowed, "the fast path"
    # The file moved. Without a way to reach the tree the gate refuses…
    assert not _allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha).allowed
    # …and with one, it asks whether the items THIS body references moved.
    assert _allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha,
                          definition_present=lambda _d: True).allowed
    refused = _allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha,
                             definition_present=lambda _d: False)
    assert not refused.allowed and refused.moved == ("ErrorImpl",)


def test_an_entry_with_no_dependency_list_is_gated_by_its_file_alone() -> None:
    """An empty check is not a pass. A body that names no authored item has
    nothing for the fallback to compare, so a moved file refuses it — costing
    a re-derivation rather than a tree that does not build."""
    e = _entry(layout_deps=())
    assert _allowed(e, file_fp="fp-original", sig_sha=e.sig_sha).allowed
    refused = _allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha,
                             definition_present=lambda _d: True)
    assert not refused.allowed and refused.reason == "layout moved"


def test_the_gate_refuses_a_moved_signature_a_stale_version_and_a_refutation() -> None:
    e = _entry()
    assert not _allowed(e, file_fp="fp-original",
                              sig_sha="other").allowed
    assert not _allowed(replace(e, gate_ver="bl0"), file_fp="fp-original",
                              sig_sha=e.sig_sha).allowed
    assert not _allowed(replace(e, state=BodyState.REFUTED),
                              file_fp="fp-original", sig_sha=e.sig_sha).allowed
    assert not _allowed(replace(e, state=BodyState.LANDED),
                              file_fp="fp-original", sig_sha=e.sig_sha).allowed, (
        "a backfilled body no sweep has judged on THIS code is not re-seeded")
    assert _allowed(replace(e, state=BodyState.UNMEASURED),
                          file_fp="fp-original", sig_sha=e.sig_sha).allowed, (
        "the majority class — tests blocked behind a stubbed sibling — IS "
        "worth putting back")
    assert e.gate_ver == GATE_VERSION


# --- promotion --------------------------------------------------------------

def _views(before: tuple[set[str], set[str]], after: tuple[set[str], set[str]],
           *, epoch_before: int = 0, epoch_after: int = 0,
           intact: bool = True) -> tuple[SweepView, SweepView]:
    return (SweepView(frozenset(before[0]), frozenset(before[1]), True,
                      epoch_before),
            SweepView(frozenset(after[0]), frozenset(after[1]), intact,
                      epoch_after))


def test_a_body_whose_tests_never_ran_is_unmeasured_not_proven() -> None:
    """The majority class, and the reason `proven` had to stop meaning
    "0 >= 0". In the last iteration of run `259637-36db9aad` all 17 frontier
    units were compiling bodies whose attributed tests were blocked by a
    still-stubbed sibling."""
    e = _entry(state=BodyState.LANDED)
    before, after = _views((set(), set()), ({"test_other"}, set()))
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=after, body_now_sha=e.body_sha)
    assert state is BodyState.UNMEASURED and delta == ()


def test_a_regression_inside_one_epoch_with_the_body_still_on_the_tree_refutes() -> None:
    e = _entry(state=BodyState.LANDED)
    before, after = _views(({"test_new"}, set()), (set(), {"test_new"}))
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=after, body_now_sha=e.body_sha)
    assert state is BodyState.REFUTED and delta == (("test_new", "lost"),)


def test_a_test_lost_to_an_aborted_binary_is_unmeasured_not_refuted() -> None:
    """The sweep pads its failed set with tests whose binary aborted before
    they reported (right for the score). Run `119837-09e3ac57`, 2026-09-15:
    ten binaries aborted on panics inside still-stubbed siblings, and 8 of 12
    landed bodies were graded `refuted` on `lost` tests that never ran —
    `refuted` is the state that is never re-seeded."""
    e = _entry(state=BodyState.LANDED)
    before = SweepView.of_sweep(passed={"test_new", "test_conv"},
                                failed=set(), vanished=(), intact=True,
                                epoch=0, positions={},
                                aborted=(), aborted_silent=(),
                                verdicts=KeyedVerdicts.of_names(
                                    passed=("test_new", "test_conv")))
    # What the sweep hands over: both names counted failing, both vanished
    # (the capture reported neither, so its reading holds no key for them).
    after = SweepView.of_sweep(passed=set(), failed={"test_new", "test_conv"},
                               vanished={"test_new", "test_conv"}, intact=True,
                               epoch=0, positions={},
                               aborted=(), aborted_silent=(),
                               verdicts=KeyedVerdicts())
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=after, body_now_sha=e.body_sha)
    assert state is BodyState.UNMEASURED and delta == ()
    # Negative control: the same sweep with the vanished names left in
    # `failed`, as the ledger read it before `of_sweep`.
    padded = SweepView(frozenset(), frozenset({"test_new", "test_conv"}), True, 0)
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=padded, body_now_sha=e.body_sha)
    assert state is BodyState.REFUTED and delta == (("test_new", "lost"),)


def test_a_test_that_really_failed_beside_an_aborted_binary_still_refutes() -> None:
    e = _entry(state=BodyState.LANDED)
    before, _ = _views(({"test_new"}, set()), (set(), set()))
    after = SweepView.of_sweep(passed=set(), failed={"test_new", "test_gone"},
                               vanished={"test_gone"}, intact=True, epoch=0,
                               positions={}, aborted=(), aborted_silent=(),
                               verdicts=KeyedVerdicts.of_names(
                                   failed=("test_new",)))
    state, _ = classify(e, attributed={"test_new"}, before=before, after=after,
                        body_now_sha=e.body_sha)
    assert state is BodyState.REFUTED


def test_a_regression_across_a_destruction_epoch_is_unmeasured() -> None:
    """The standing rule, arriving in the entry whose whole subject is
    deliberate destruction: a body whose tests drop because a SIBLING was
    re-stubbed must not be refuted — `refuted` is the one state that suppresses
    re-seeding forever."""
    e = _entry(state=BodyState.LANDED)
    before, after = _views(({"test_new"}, set()), (set(), {"test_new"}),
                           epoch_before=0, epoch_after=1)
    state, _ = classify(e, attributed={"test_new"}, before=before, after=after,
                        body_now_sha=e.body_sha)
    assert state is BodyState.UNMEASURED


def test_a_regression_after_the_body_was_replaced_is_unmeasured() -> None:
    """The clause the epoch cannot carry: the batch ratchet's restore is
    deliberately NOT an epoch bump, so a restore that reverted this unit leaves
    the epoch unchanged while changing the tree under the comparison."""
    e = _entry(state=BodyState.LANDED)
    before, after = _views(({"test_new"}, set()), (set(), {"test_new"}))
    state, _ = classify(e, attributed={"test_new"}, before=before, after=after,
                        body_now_sha=body_sha(("something else",)))
    assert state is BodyState.UNMEASURED


def test_a_crash_noise_sweep_judges_nothing() -> None:
    e = _entry(state=BodyState.LANDED)
    before, after = _views(({"test_new"}, set()), (set(), {"test_new"}),
                           intact=False)
    state, _ = classify(e, attributed={"test_new"}, before=before, after=after,
                        body_now_sha=e.body_sha)
    assert state is BodyState.UNMEASURED


def test_tests_that_ran_and_are_all_still_red_are_a_graded_empty_delta() -> None:
    """Nothing regressed, so this is not a refutation — but every attributed
    test is still failing, so the body has been shown not to have BROKEN
    anything, which is not the same as being right. `proven` has to mean a
    convicting test passes; an empty delta on tests that ran is `flat`."""
    e = _entry(state=BodyState.LANDED)
    before, after = _views((set(), {"test_new"}), (set(), {"test_new"}))
    state, _ = classify(e, attributed={"test_new"}, before=before, after=after,
                        body_now_sha=e.body_sha)
    assert state is BodyState.FLAT
    assert not replace(e, state=state).reseedable


def _shared_name(passed: tuple[TestKey, ...],
                 failed: tuple[TestKey, ...]) -> SweepView:
    """The sweep's name sets union every copy of one leaf. The verdicts
    keep each copy, which is what `classify` has to read."""
    return SweepView.of_sweep(
        passed={k.test for k in passed}, failed={k.test for k in failed},
        vanished=(), intact=True, epoch=0, positions={},
        aborted=(), aborted_silent=(),
        verdicts=KeyedVerdicts(passed=frozenset(passed),
                               failed=frozenset(failed)))


def test_one_name_in_two_binaries_is_not_lost_when_its_red_copy_stayed_red() -> None:
    """The name sets hold the leaf in passed and in failed at once, which
    is the reading that refutes a landing: passing before, failing after.
    Those are two copies. The copy that passed still passes, and the copy
    that failed still fails, so the landing is flat. The copy that passed
    turning red refutes it."""
    e = _entry(state=BodyState.LANDED)
    green, red = TestKey("bin_a", "t_shared"), TestKey("bin_b", "t_shared")
    before = _shared_name((green,), (red,))
    stayed = _shared_name((green,), (red,))
    assert before.passed & stayed.failed == frozenset({"t_shared"})
    state, delta = classify(e, attributed={"t_shared"}, before=before,
                            after=stayed, body_now_sha=e.body_sha)
    assert state is BodyState.FLAT and delta == ()
    lost = _shared_name((), (green, red))
    state, delta = classify(e, attributed={"t_shared"}, before=before,
                            after=lost, body_now_sha=e.body_sha)
    assert state is BodyState.REFUTED and delta == (("t_shared", "lost"),)


def test_a_passing_sibling_does_not_prove_a_body_whose_convicting_tests_fail(
) -> None:
    """Run `9872-51b20f30` judged a landing `proven` with an empty delta
    because passing siblings sat in `attributed`. The convicting test (red
    at dispatch, still red after) is the one that decides."""
    e = _entry(state=BodyState.LANDED)
    before, after = _views(({"test_green"}, {"test_red"}),
                           ({"test_green"}, {"test_red"}))
    state, delta = classify(
        e, attributed={"test_red", "test_green"},
        before=before, after=after, body_now_sha=e.body_sha)
    assert state is BodyState.FLAT
    assert delta == ()


def test_a_landing_with_no_red_attributed_test_is_flat_not_proven() -> None:
    """Run `3040-51eab48c` (2026-10-02): the keeps after a fill's last red
    test turned green landed with every attributed test already passing.
    Nothing convicted the dispatch and the delta is empty, so the landing
    is a graded try that bought nothing. `proven` here ranked the unit as
    paying at the next schedule. Negative control: the same landing when
    one of those tests was red before it is `proven`, with the delta."""
    e = _entry(state=BodyState.LANDED)
    before, after = _views(({"test_green"}, set()), ({"test_green"}, set()))
    state, delta = classify(e, attributed={"test_green"}, before=before,
                            after=after, body_now_sha=e.body_sha)
    assert state is BodyState.FLAT
    assert delta == ()
    before, after = _views((set(), {"test_green"}), ({"test_green"}, set()))
    state, delta = classify(e, attributed={"test_green"}, before=before,
                            after=after, body_now_sha=e.body_sha)
    assert state is BodyState.PROVEN
    assert delta == (("test_green", "gained"),)


def test_a_measured_non_regression_is_proven_and_names_what_moved() -> None:
    e = _entry(state=BodyState.LANDED)
    before, after = _views((set(), {"test_new", "test_kept"}),
                           ({"test_new", "test_kept"}, set()))
    state, delta = classify(e, attributed={"test_new", "test_kept"},
                            before=before, after=after,
                            body_now_sha=e.body_sha)
    assert state is BodyState.PROVEN
    assert set(delta) == {("test_new", "gained"), ("test_kept", "gained")}


# --- selection --------------------------------------------------------------

def test_the_standing_claim_is_the_latest_word_but_proven_outranks_unmeasured() -> None:
    proven = _entry(state=BodyState.PROVEN)
    other = _entry(body_sha="deadbeef", bodies=("different",),
                   state=BodyState.UNMEASURED)
    assert latest_by_unit([proven, other])[_KEY] is proven
    refuted = replace(proven, state=BodyState.REFUTED)
    assert latest_by_unit([proven, refuted])[_KEY] is refuted, (
        "a later word about the SAME body wins — that is what append-only is "
        "for")
    later_other = _entry(body_sha="cafef00d", bodies=("third",),
                         state=BodyState.UNMEASURED)
    assert latest_by_unit([other, later_other])[_KEY] is later_other, (
        "two different unmeasured bodies: the later row is the standing claim")


def test_a_word_that_measured_nothing_does_not_take_a_refutation_back() -> None:
    """A crash settlement refutes the body it puts back to a stub; the next
    sweep, finding that body off the tree, writes `unmeasured` about it. The
    refutation must stand, or the re-seed splices the convicted body back
    (run `1057870-b427a3a2`, 2026-10-15). A later MEASURED word still wins."""
    refuted = _entry(state=BodyState.REFUTED,
                     verdict_delta=(("t", "crash_origin"),))
    later = replace(refuted, state=BodyState.UNMEASURED, verdict_delta=())
    assert latest_by_unit([refuted, later])[_KEY] is refuted
    assert not plan_reseed([refuted, later], wanted=[_KEY],
                           file_fp={_KEY.file: "fp-original"},
                           sig_sha={_KEY: refuted.sig_sha},
                           oracle_fp=_ORACLE).take
    proven = replace(refuted, state=BodyState.PROVEN)
    assert latest_by_unit([refuted, proven])[_KEY] is proven
    # Negative control: the same body's later word read without the rule.
    assert later.reseedable


def test_the_plan_answers_only_about_the_units_it_was_asked_about() -> None:
    """The line between this store and a dispatch decider: it never adds a unit
    to anything, it answers about the stub set it is handed."""
    e = _entry()
    other = LedgerKey("anyhow", "stable", "src/kind.rs", "from")
    plan = plan_reseed([e], wanted=[_KEY, other],
                       file_fp={"src/error.rs": "fp-original"},
                       sig_sha={_KEY: e.sig_sha, other: "x"}, oracle_fp=_ORACLE)
    assert [t.key for t in plan.take] == [_KEY]
    assert plan.skipped == []


# --- re-buy detector --------------------------------------------------------

def test_rebuy_hits_a_prior_process_empty_delta_and_skips_the_rest() -> None:
    """The join `pattern_census.py` computed after the fact, at dispatch time.
    Run `349271-d876e4a9` re-bought 7 of 7 accessors a prior process had
    already graded with an empty verdict delta. Detector only."""
    loc = FnLocator(_KEY.file, _KEY.fn_name)
    prior = {_KEY: _entry(run_id="349271-d876e4a9", verdict_delta=())}
    hits = _rebuy([loc], prior, project="anyhow", cluster="stable",
                      run_id="this-process")
    assert [u for u, _ in hits] == [loc]
    # Empty process id cannot express "a prior process".
    assert _rebuy([loc], prior, project="anyhow", cluster="stable",
                      run_id="") == ()
    # Same process: this process just wrote the row.
    same = {_KEY: _entry(run_id="this-process", verdict_delta=())}
    assert _rebuy([loc], same, project="anyhow", cluster="stable",
                      run_id="this-process") == ()
    # A verdict actually moved: not a re-buy.
    moved = {_KEY: _entry(run_id="349271-d876e4a9",
                          verdict_delta=(("t", "gained"),))}
    assert _rebuy([loc], moved, project="anyhow", cluster="stable",
                      run_id="this-process") == ()
    # First dispatch: no prior row.
    assert _rebuy([loc], {}, project="anyhow", cluster="stable",
                      run_id="this-process") == ()
    # Prior row with no run_id (every live row before this field): skip.
    blank = {_KEY: _entry(run_id="", verdict_delta=())}
    assert _rebuy([loc], blank, project="anyhow", cluster="stable",
                      run_id="this-process") == ()
    # LANDED is not a grade: fill returned, no sweep has spoken (a kill
    # after fill-return, before the sweep).
    landed = {_KEY: _entry(state=BodyState.LANDED, run_id="349271-d876e4a9",
                           verdict_delta=())}
    assert _rebuy([loc], landed, project="anyhow", cluster="stable",
                      run_id="this-process") == ()
    # UNMEASURED is a grade (the sweep ran; the tests did not).
    unmeas = {_KEY: _entry(state=BodyState.UNMEASURED,
                           run_id="349271-d876e4a9", verdict_delta=())}
    assert [u for u, _ in _rebuy(
        [loc], unmeas, project="anyhow", cluster="stable",
        run_id="this-process")] == [loc]


def test_rebuy_by_grade_names_the_standing_state_not_flat_vs_rest() -> None:
    """A proven empty-delta hit is not labeled unmeasured. The live
    2965-453dd7a4 iter-1 cut was 4 FLAT / 12 UNMEASURED / 0 other, so a
    not-FLAT bucket happened to print the right sentence; the detector's
    own prior-process fixture is PROVEN."""
    proven = (FnLocator("src/error.rs", "new"),
              _entry(state=BodyState.PROVEN, run_id="349271-d876e4a9",
                     verdict_delta=()))
    unmeas = (FnLocator("src/error.rs", "construct"),
              _entry(state=BodyState.UNMEASURED, run_id="57079-a5f83827",
                     verdict_delta=()))
    flat = (FnLocator("src/error.rs", "from"),
            _entry(state=BodyState.FLAT, run_id="57079-a5f83827",
                   verdict_delta=()))
    by = rebuy_by_grade((proven, unmeas, flat))
    assert [u.fn_name for u in by["proven"]] == ["new"]
    assert [u.fn_name for u in by["unmeasured"]] == ["construct"]
    assert [u.fn_name for u in by["flat"]] == ["from"]


def test_ledger_helpers_the_index_and_fingerprint_actually_call() -> None:
    assert _KEY.as_str() == "anyhow/stable/src/error.rs::new"
    assert text_sha("Error::from(e)") == text_sha("  Error::from(e)\n")
    nested = "/* outer /* inner */ still */ code // trailing\nkept"
    assert "code" in strip_comments(nested)
    assert "inner" not in strip_comments(nested)
    assert "kept" in strip_comments(nested)
    assert strip_comments("ok // no newline").startswith("ok")
    deps = body_layout_deps(
        "Error", "fn new", {"Error": ("struct", "struct Error {}")})
    assert deps and deps[0].name == "Error"
    proven = _entry(state=BodyState.PROVEN, body_sha="aaa")
    later = _entry(state=BodyState.UNMEASURED, body_sha="bbb",
                   bodies=("other",))
    stand = latest_by_unit([proven, later])
    assert stand[_KEY].state is BodyState.PROVEN
    plan = ReseedPlan()
    plan.take.append(proven)
    assert "1 unit(s)" in plan.render() and "1 proven" in plan.render()
    e = _entry(state=BodyState.LANDED)
    before, after = _views((set(), {"test_old"}), ({"test_new"}, set()))
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=after, body_now_sha=e.body_sha)
    assert state is BodyState.UNMEASURED and delta == ()


# --- the oracle clause --------------------------------------------------------

def test_a_proven_row_graded_under_oracle_a_is_refused_on_oracle_b_and_taken_on_a() -> None:
    """The plan's sentence. A body is `proven` BY tests; on a tree scored by
    other tests the word carries no evidence. The negative control is the
    pre-fix rule — layout and signature only — which the same row passes:
    it is exactly and only the oracle clause that refuses."""
    e = _entry(state=BodyState.PROVEN, oracle_fp="oracle-A")
    assert reseed_allowed(e, file_fp="fp-original", sig_sha=e.sig_sha,
                          oracle_fp="oracle-A").allowed
    refused = reseed_allowed(e, file_fp="fp-original", sig_sha=e.sig_sha,
                             oracle_fp="oracle-B")
    assert not refused.allowed and refused.reason == "oracle moved"
    # The pre-fix rule, injected: every clause but the oracle. Layout matches,
    # signature matches, state is proven — the old gate took this row.
    assert e.gate_ver == GATE_VERSION and e.layout_fp == "fp-original"
    assert e.state is BodyState.PROVEN and e.reseedable
    # An EMPTY stored fingerprint refuses too — even against an empty live
    # one: "nobody recorded the oracle" is not "the same oracle".
    blank = replace(e, oracle_fp="")
    assert reseed_allowed(blank, file_fp="fp-original", sig_sha=e.sig_sha,
                          oracle_fp="").reason == "oracle moved"
    assert reseed_allowed(blank, file_fp="fp-original", sig_sha=e.sig_sha,
                          oracle_fp="oracle-A").reason == "oracle moved"
    # `unmeasured` rows are gated by the same clause.
    assert not reseed_allowed(replace(e, state=BodyState.UNMEASURED),
                              file_fp="fp-original", sig_sha=e.sig_sha,
                              oracle_fp="oracle-B").allowed


_FIXTURE = Path(__file__).parent / "fixtures" / "body_ledger" / "stable_latest_10.jsonl"
_LIVE_ORACLE = "46bd0150dc5c"   # the live stable oracle, 60 files, 2026-08-26


def _recorded_rows() -> list[LedgerEntry]:
    rows = [LedgerEntry.from_dict(json.loads(ln))
            for ln in _FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert all(r is not None for r in rows) and len(rows) == 10
    return [r for r in rows if r is not None]


def test_the_recorded_stable_rows_are_refused_once_and_the_layout_only_rule_took_them() -> None:
    """Ten latest rows copied from the live `logs/body_ledger/anyhow` index on
    2026-08-26 (`scripts/ledger_reseed_exposure.py` on the full 568-row
    stable ledger: TAKE 73 of 85 under the layout-only rule, TAKE 0 under the
    real oracle `46bd0150dc5c`; RE-BUY 3 vs 0). Every recorded row carries an
    empty oracle — nobody recorded which tests graded it — and the layout
    fingerprints all MATCHED the live skeleton, so `file_fp` here is the row's
    own fingerprint: the layout condition passes exactly as it did live."""
    rows = _recorded_rows()
    assert all(r.oracle_fp == "" for r in rows), "pre-field rows carry no oracle"
    keys = [r.key for r in rows]
    file_fp = {r.key.file: r.layout_fp for r in rows}
    plan = plan_reseed(rows, wanted=keys, file_fp=file_fp, sig_sha={},
                       oracle_fp=_LIVE_ORACLE)
    assert plan.take == [] and {why for _k, why in plan.skipped} == {"oracle moved"}
    assert len(plan.skipped) == 10
    # THE NEGATIVE CONTROL — the pre-fix rule: stamp every row with the live
    # oracle so the clause is vacuous, and the layout-only gate takes all ten.
    stamped = [replace(r, oracle_fp=_LIVE_ORACLE) for r in rows]
    control = plan_reseed(stamped, wanted=keys, file_fp=file_fp, sig_sha={},
                          oracle_fp=_LIVE_ORACLE)
    assert len(control.take) == 10 and control.skipped == []
    # The re-buy probe: the three units the live probe named, and none under
    # the real oracle.
    named = [FnLocator("src/context.rs", "context"),
             FnLocator("src/context.rs", "ext_context"),
             FnLocator("src/kind.rs", "new")]
    stand = latest_by_unit(rows)
    assert rebuy_hits(named, stand, project="anyhow", cluster="stable",
                      run_id="cold-run-probe", oracle_fp=_LIVE_ORACLE) == ()
    hits = rebuy_hits(named, latest_by_unit(stamped), project="anyhow",
                      cluster="stable", run_id="cold-run-probe",
                      oracle_fp=_LIVE_ORACLE)
    assert [u for u, _ in hits] == named
    assert all(e.run_id == "82157-476ddc16" and e.verdict_delta == ()
               for _u, e in hits)


def test_the_oracle_fingerprint_is_order_free_path_bound_and_never_empty() -> None:
    a = [("src/a.gen_tests_1.rs", "fn t() { assert!(x) }"),
         ("tests/t.rs", "fn u() {}")]
    assert oracle_fingerprint(a) == oracle_fingerprint(list(reversed(a)))
    assert oracle_fingerprint(a) == oracle_fingerprint(
        [("src/a.gen_tests_1.rs", "fn t()  {\n assert!(x) }"), ("tests/t.rs", "fn u() {}")]), (
        "whitespace is folded: a reformatted test is the same oracle")
    assert oracle_fingerprint(a) != oracle_fingerprint(
        [("src/a.gen_tests_1.rs", "fn t() { assert!(y) }"), ("tests/t.rs", "fn u() {}")])
    assert oracle_fingerprint(a) != oracle_fingerprint(
        [("src/b.gen_tests_1.rs", "fn t() { assert!(x) }"), ("tests/t.rs", "fn u() {}")]), (
        "a sibling is attached to ITS parent module: the path is identity")
    assert oracle_fingerprint([]) and len(oracle_fingerprint([])) == 12, (
        "an empty oracle set is a recorded fact, distinct from `` (unrecorded)")


def test_a_row_without_the_oracle_field_parses_to_the_empty_fingerprint() -> None:
    e = _entry(oracle_fp="abc123abc123")
    assert LedgerEntry.from_dict(e.to_dict()) == e
    raw = e.to_dict()
    del raw["oracle_fp"]
    back = LedgerEntry.from_dict(raw)
    assert back is not None and back.oracle_fp == ""


# --- the FLAT state -----------------------------------------------------------

def test_flat_is_a_grade_and_never_a_seed() -> None:
    flat = _entry(state=BodyState.FLAT)
    assert not flat.reseedable
    assert reseed_allowed(flat, file_fp="fp-original", sig_sha=flat.sig_sha,
                          oracle_fp=_ORACLE).reason == "state flat"
    # classify now produces `flat` when convicting tests ran and all still
    # fail (the fourth reading below). The other three readings are
    # unchanged.
    e = _entry(state=BodyState.LANDED)
    cases: tuple[tuple[tuple[set[str], set[str]],
                       tuple[set[str], set[str]], BodyState], ...] = (
        ((set(), set()), ({"o"}, set()), BodyState.UNMEASURED),
        (({"t"}, set()), (set(), {"t"}), BodyState.REFUTED),
        ((set(), {"t"}), (set(), {"t"}), BodyState.FLAT),
        ((set(), {"t"}), ({"t"}, set()), BodyState.PROVEN),
    )
    for before, after, want in cases:
        b, a = _views(before, after)
        state, _ = classify(e, attributed={"t"}, before=b, after=a,
                            body_now_sha=e.body_sha)
        assert state is want


def test_rebuy_fires_on_a_flat_prior_process_row_and_not_a_same_process_one() -> None:
    """The row the `82157-476ddc16` constructors never had. A FLAT row from a
    prior process is a graded try with an empty delta, so the detector fires
    on it with no rule of its own; the same row from THIS process is the
    audit that just wrote it."""
    loc = FnLocator(_KEY.file, _KEY.fn_name)
    prior = {_KEY: _entry(state=BodyState.FLAT, run_id="82157-476ddc16",
                          verdict_delta=())}
    assert [u for u, _ in rebuy_hits(
        [loc], prior, project="anyhow", cluster="stable",
        run_id="next-process", oracle_fp=_ORACLE)] == [loc]
    assert rebuy_hits([loc], prior, project="anyhow", cluster="stable",
                      run_id="82157-476ddc16", oracle_fp=_ORACLE) == ()


def test_rebuy_skips_a_row_graded_under_another_oracle() -> None:
    loc = FnLocator(_KEY.file, _KEY.fn_name)
    prior = {_KEY: _entry(run_id="82157-476ddc16", verdict_delta=(),
                          oracle_fp="oracle-A")}
    assert [u for u, _ in rebuy_hits(
        [loc], prior, project="anyhow", cluster="stable", run_id="next",
        oracle_fp="oracle-A")] == [loc]
    assert rebuy_hits([loc], prior, project="anyhow", cluster="stable",
                      run_id="next", oracle_fp="oracle-B") == ()
    blank = {_KEY: _entry(run_id="82157-476ddc16", verdict_delta=(),
                          oracle_fp="")}
    assert rebuy_hits([loc], blank, project="anyhow", cluster="stable",
                      run_id="next", oracle_fp="oracle-A") == ()


def test_a_flat_row_about_the_same_body_supersedes_a_standing_proven_row() -> None:
    """Same body, later word: the PROVEN was the weak-assertion Display pass,
    the FLAT is the escalated rewrite that came back identical with the
    convicting tests still red. Not special-cased — the same clause a
    refutation uses. A FLAT row about a DIFFERENT body does not outrank a
    standing PROVEN one, exactly as an UNMEASURED one does not."""
    proven = _entry(state=BodyState.PROVEN, run_id="prior")
    flat = replace(proven, state=BodyState.FLAT, run_id="82157-476ddc16")
    assert latest_by_unit([proven, flat])[_KEY] is flat
    other = _entry(state=BodyState.FLAT, body_sha="other", bodies=("x",))
    assert latest_by_unit([proven, other])[_KEY] is proven


def test_standing_on_tree_reads_the_row_about_the_bytes_on_disk() -> None:
    """Dispatch question, not the re-seed question. Computed shape of
    `src/context.rs::with_context` on run `9872-51b20f30`: tree body
    `997c5614` (flat), latest_by_unit standing `proven` on off-tree
    `cb41090c`. The on-tree view is the flat row; the re-seed view stays
    proven. NEGATIVE CONTROL: latest_by_unit on the same rows is proven."""
    proven = _entry(state=BodyState.PROVEN, body_sha="cb41090c",
                    bodies=("old",), run_id="prior")
    flat = _entry(state=BodyState.FLAT, body_sha="997c5614",
                  bodies=("cur",), run_id="9872-51b20f30")
    rows = [proven, flat]
    assert latest_by_unit(rows)[_KEY] is proven
    assert standing_on_tree(rows, {_KEY: "997c5614"})[_KEY] is flat
    assert standing_on_tree(rows, {_KEY: "cb41090c"})[_KEY] is proven
    assert standing_on_tree(rows, {_KEY: "not-on-ledger"}) == {}
    assert standing_on_tree(rows, {}) == {}


def test_flat_exclusion_holds_only_while_the_rows_premise_holds() -> None:
    """A FLAT row is a claim about a moment — this body, these convicting
    tests, this oracle — and licenses exclusion from dispatch only while
    that premise holds. Dispatch is the only producer of the new body that
    could clear the grade, so an exclusion keyed on the bare grade is a
    permanent lock (the pass-81 readmission deadlock shape). NEGATIVE
    CONTROL inline: the grade-alone read names every flat row below."""
    from pipeline_core.domain.body_ledger import flat_standing_unchanged
    same = _entry(state=BodyState.FLAT, convicting=("test_altdebug",),
                  evidence_fp="ev-1")
    standing = {_KEY: same}
    fresh = {_KEY: "ev-1"}
    # Same oracle, same convicting set, same question — already measured.
    assert flat_standing_unchanged(
        standing, oracle_fp_now=_ORACLE, evidence_now=fresh,
        convicting_now={_KEY: frozenset({"test_altdebug"})}) == {_KEY}
    # A convicting test got FIXED: the remainder was in the premise — exclude.
    assert flat_standing_unchanged(
        standing, oracle_fp_now=_ORACLE, evidence_now=fresh,
        convicting_now={_KEY: frozenset()}) == {_KEY}
    # A NEW test convicts the unit: moved evidence — re-admit.
    assert flat_standing_unchanged(
        standing, oracle_fp_now=_ORACLE, evidence_now=fresh,
        convicting_now={_KEY: frozenset({"test_altdebug",
                                         "test_trailer"})}) == set()
    # The oracle moved (tests regenerated): every grade is stale — re-admit.
    assert flat_standing_unchanged(
        standing, oracle_fp_now="oracle-B", evidence_now=fresh,
        convicting_now={_KEY: frozenset({"test_altdebug"})}) == set()
    # A row written before the premise field: evidence unknown — re-admit
    # once, so the re-grade mints a premised row instead of a forever-lock.
    legacy = _entry(state=BodyState.FLAT, convicting=())
    assert flat_standing_unchanged(
        {_KEY: legacy}, oracle_fp_now=_ORACLE, evidence_now=fresh,
        convicting_now={_KEY: frozenset({"test_altdebug"})}) == set()
    # Only FLAT rows are the exclusion's business.
    proven = _entry(state=BodyState.PROVEN, convicting=("test_altdebug",),
                    evidence_fp="ev-1")
    assert flat_standing_unchanged(
        {_KEY: proven}, oracle_fp_now=_ORACLE, evidence_now=fresh,
        convicting_now={_KEY: frozenset({"test_altdebug"})}) == set()
    # NEGATIVE CONTROL — the pre-fix read (state alone) excludes all four
    # flat shapes above, including the moved-evidence and legacy ones.
    for e in (same, legacy):
        assert {k for k, r in {_KEY: e}.items()
                if r.state is BodyState.FLAT} == {_KEY}


def test_a_flat_grade_lapses_once_the_next_prompt_would_differ() -> None:
    """THE FOURTH CLAUSE. The three clauses above all describe the world
    AROUND the unit — the body on the tree, the oracle, the convicting
    set — and on a stalled tree none of them can move: clause 1 holds
    BECAUSE the flat body was kept (it compiles and regresses nothing,
    which is why it was kept), the oracle has one identity per tree, and
    the same tests fail every sweep. So a unit that lands a compiling,
    non-regressing, non-paying body is retired by its own success. Run
    `837207-ce5b1647` (2026-09-15) excluded `src/fmt.rs::debug` from
    iteration 2 and `src/error.rs::fmt` from iteration 3 and then read
    `dispatching 0 of 0 (0/0 this pass)` against `frontier=3 fn(s)` for
    three hours.

    The plan's sentence, pinned here: the exclusion stands while the
    evidence the next prompt would carry is unchanged, and lapses once for
    a unit whose next prompt would differ. NEGATIVE CONTROL inline — the
    pre-fix read (the three clauses without this one) excludes the
    moved-evidence row too."""
    from pipeline_core.domain.body_ledger import flat_standing_unchanged
    row = _entry(state=BodyState.FLAT, convicting=("test_altdebug",),
                 evidence_fp="ev-at-grade")
    standing = {_KEY: row}
    convicting = {_KEY: frozenset({"test_altdebug"})}

    def _excluded(evidence_now: dict) -> set:
        return flat_standing_unchanged(
            standing, oracle_fp_now=_ORACLE, convicting_now=convicting,
            evidence_now=evidence_now)

    # Same question — refuse to spend.
    assert _excluded({_KEY: "ev-at-grade"}) == {_KEY}
    # A sibling landed a body / the retro store gained a round / a fault
    # now names the unit: a DIFFERENT question, asked once.
    assert _excluded({_KEY: "ev-moved"}) == set()
    # The loop could read none of the evidence channels: nothing observed,
    # so nothing is claimed — the lock is what refusing here prevents.
    assert _excluded({_KEY: ""}) == set()
    assert _excluded({}) == set()
    # A row written before the field: same reason, from the other side.
    assert flat_standing_unchanged(
        {_KEY: _entry(state=BodyState.FLAT, convicting=("test_altdebug",))},
        oracle_fp_now=_ORACLE, convicting_now=convicting,
        evidence_now={_KEY: "ev-at-grade"}) == set()
    # NEGATIVE CONTROL — the three-clause read excludes on moved evidence,
    # which is the lock this clause exists to break.
    assert (row.state is BodyState.FLAT and row.oracle_fp == _ORACLE
            and convicting[_KEY] <= frozenset(row.convicting))


def test_flat_rows_carry_the_convicting_premise_they_are_scoped_to() -> None:
    """`rows_for_units` stamps the tests whose failure bought the dispatch,
    per unit, so the exclusion above has a premise to check."""
    recs = [{"file": "src/error.rs", "fn_name": "new",
             "sig_text": "pub fn new(e: E) -> Self",
             "body_text": "Error::from(e)"}]
    rows = rows_for_units(
        [("src/error.rs", "new")], recs, project="anyhow", cluster="stable",
        state=BodyState.FLAT, items={}, file_of={}, file_fp={},
        run_id="r", iteration=1, epoch=0, oracle_fp="oracle-A",
        convicting_of={("src/error.rs", "new"): ["test_b", "test_a"]},
        evidence_of={("src/error.rs", "new"): "ev-at-dispatch"})
    assert rows[0].convicting == ("test_a", "test_b")
    # ...and the evidence the dispatch that bought this try carried, so the
    # exclusion can tell an unchanged question from a changed one.
    assert rows[0].evidence_fp == "ev-at-dispatch"
    # Round-trips through the index encoding; a pre-field raw dict parses
    # to the unrecorded premise, on both halves.
    assert LedgerEntry.from_dict(rows[0].to_dict()).convicting == (
        "test_a", "test_b")
    assert LedgerEntry.from_dict(
        rows[0].to_dict()).evidence_fp == "ev-at-dispatch"
    raw = rows[0].to_dict()
    del raw["convicting"]
    del raw["evidence_fp"]
    assert LedgerEntry.from_dict(raw).convicting == ()
    assert LedgerEntry.from_dict(raw).evidence_fp == ""
    # A caller that supplies no evidence map leaves the premise unrecorded
    # rather than minting a fingerprint nothing observed.
    bare = rows_for_units(
        [("src/error.rs", "new")], recs, project="anyhow", cluster="stable",
        state=BodyState.FLAT, items={}, file_of={}, file_fp={},
        run_id="r", iteration=1, epoch=0, oracle_fp="oracle-A")
    assert bare[0].evidence_fp == ""


_ESC_UNCHANGED_82157_ITER_1 = (
    # `logs/pipeline_anyhow.log`, run 82157-476ddc16, iter 1's landed-change
    # audit: "ESCALATED, unchanged (a FAILED try — the instruction was
    # rewrite)", nine units, all `src/error.rs`.
    "construct", "construct_from_adhoc", "construct_from_boxed",
    "construct_from_context", "construct_from_display", "construct_from_std",
    "context", "msg", "new")


def test_rows_for_units_builds_the_nine_flat_rows_of_the_recorded_audit() -> None:
    """The recorded shape, through the one constructor both audit arms share.
    Nine escalated units came back unchanged and left no row; the FLAT arm
    builds one per unit from the CURRENT records, stamped like a landing."""
    recs = [{"file": "src/error.rs", "fn_name": n,
             "sig_text": f"    pub fn {n}(e: E) -> Self",
             "body_text": f"\n        // {n}\n        Error::from(e)\n    "}
            for n in _ESC_UNCHANGED_82157_ITER_1]
    # `src/context.rs::source` is two impls: an ordered TUPLE of occurrences.
    recs += [{"file": "src/context.rs", "fn_name": "source",
              "sig_text": "fn source(&self)", "body_text": "None"},
             {"file": "src/context.rs", "fn_name": "source",
              "sig_text": "fn source(&self)", "body_text": "Some(&self.0)"}]
    units = [("src/error.rs", n) for n in _ESC_UNCHANGED_82157_ITER_1]
    units += [("src/context.rs", "source"), ("src/error.rs", "not_extracted")]
    items = {"Error": ("struct", "pub struct Error { inner: Own }")}
    rows = rows_for_units(
        units, recs, project="anyhow", cluster="stable", state=BodyState.FLAT,
        items=items, file_of={"Error": "src/error.rs"},
        file_fp={"src/error.rs": "fp-e"}, run_id="82157-476ddc16",
        iteration=1, epoch=0, oracle_fp="oracle-A")
    assert [r.key.fn_name for r in rows if r.key.file == "src/error.rs"] == list(
        _ESC_UNCHANGED_82157_ITER_1), (
        "one row per unit, sorted; none for a unit the extractor cannot see")
    assert all(r.state is BodyState.FLAT and r.verdict_delta == () for r in rows)
    assert all(r.run_id == "82157-476ddc16" and r.iteration == 1
               and r.oracle_fp == "oracle-A" and r.layout_fp == "fp-e"
               for r in rows if r.key.file == "src/error.rs")
    first = next(r for r in rows if r.key.fn_name == "construct")
    assert first.bodies == ("\n        // construct\n        Error::from(e)\n    ",), (
        "RAW body text — the folded identity would comment out its own remainder")
    assert first.body_sha == body_sha(first.bodies)
    assert [d.name for d in first.layout_deps] == ["Error"]
    two = next(r for r in rows if r.key.file == "src/context.rs")
    assert two.bodies == ("None", "Some(&self.0)")
    # The same construction as a landing: only the state differs.
    landed = rows_for_units(
        units, recs, project="anyhow", cluster="stable",
        state=BodyState.LANDED, items=items, file_of={"Error": "src/error.rs"},
        file_fp={"src/error.rs": "fp-e"}, run_id="82157-476ddc16",
        iteration=1, epoch=0, oracle_fp="oracle-A")
    assert [replace(r, state=BodyState.LANDED) for r in rows] == landed


def test_judgment_attributed_joins_the_dispatch_premise_and_only_that() -> None:
    """An escalation-only unit has no reverse-map attribution; its LANDED
    row carries the tests whose failure bought the dispatch, and the
    judgment must be scoped to them. A row without a premise judges on the
    reverse map exactly as before."""
    from pipeline_core.domain.body_ledger import judgment_attributed
    row = _entry(state=BodyState.LANDED, convicting=("test_a", "test_b"))
    assert judgment_attributed(frozenset(), row) == frozenset({"test_a", "test_b"})
    assert judgment_attributed({"test_c"}, row) == frozenset({"test_a", "test_b", "test_c"})
    bare = _entry(state=BodyState.LANDED, convicting=())
    assert judgment_attributed({"test_c"}, bare) == frozenset({"test_c"})
    assert judgment_attributed(frozenset(), bare) == frozenset()
    # The premise is a subset of the pre-fill failing set by construction,
    # so the join can only add FLAT / PROVEN outcomes, never REFUTED.
    before = SweepView(passed=frozenset({"test_c"}), failed=frozenset({"test_a", "test_b"}))
    after = SweepView(passed=frozenset({"test_c"}), failed=frozenset({"test_a", "test_b"}))
    state, delta = classify(row, attributed=judgment_attributed(frozenset(), row),
                            before=before, after=after, body_now_sha=row.body_sha)
    assert state is BodyState.FLAT and delta == ()
    state, _ = classify(row, attributed=frozenset(), before=before, after=after,
                        body_now_sha=row.body_sha)
    assert state is BodyState.UNMEASURED


# --- assertion progress -----------------------------------------------------

_LIBTEST_RED = """\
     Running tests/test_x.rs (target/debug/deps/test_x-0123)

running 1 test
test test_new ... FAILED

failures:

---- test_new stdout ----

thread 'test_new' panicked at tests/test_x.rs:{line}:5:
assertion `left == right` failed

failures:
    test_new

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


def _test_src(open_: int = 86, close: int = 97) -> str:
    lines = ["// filler"] * 200
    lines[open_ - 1] = "fn test_new() {"
    for k in range(open_, close - 1):
        lines[k] = "    assert!(check());"
    lines[close - 1] = "}"
    return "\n".join(lines)


def _oracle(rel: str) -> str | None:
    """The invented oracle: every `tests/*.rs` holds `test_new` at 86..97."""
    return _test_src() if rel.startswith("tests/") else None


def _red_at(line: int, *, file: str = "tests/test_x.rs") -> SweepView:
    """A sweep view built the way the loop builds it: the failing test's
    assertion position read off the libtest capture and the oracle file."""
    from pipeline_core.domain.assertion_position import assertion_positions
    text = _LIBTEST_RED.format(line=line).replace("tests/test_x.rs", file)
    return SweepView(frozenset(), frozenset({"test_new"}), True, 0,
                     positions=assertion_positions(text, _oracle))


def test_a_convicting_test_that_now_fails_a_later_assertion_is_advanced() -> None:
    """Plan sentence: a landing whose convicting test still fails, but now
    at a later line of its own test body, got the test past an assertion —
    `advanced`, not `flat`. It is re-seedable, it carries a non-empty delta
    (so the re-buy detector does not fire on it), and the flat exclusion
    does not retire it.

    Negative controls: the same line, another binary and a failure that
    printed no location all stay `flat`. An EARLIER line is not flat — it
    is `refuted` (below)."""
    from pipeline_core.domain.assertion_position import TestKey
    e = _entry(state=BodyState.LANDED)
    before = _red_at(90)
    assert before.positions == {TestKey("tests/test_x.rs", "test_new"): 90}
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=_red_at(91), body_now_sha=e.body_sha)
    assert state is BodyState.ADVANCED
    assert delta == (("test_new", "advanced"),)
    assert replace(e, state=state).reseedable
    for after in (_red_at(90), _red_at(95, file="tests/other.rs"),
                  SweepView(frozenset(), frozenset({"test_new"}), True, 0)):
        flat, flat_delta = classify(e, attributed={"test_new"}, before=before,
                                    after=after, body_now_sha=e.body_sha)
        assert flat is BodyState.FLAT and flat_delta == ()


def test_a_gain_beside_an_earlier_panic_stands() -> None:
    """The decided rule (2026-09-29): a verdict outranks a position. One
    convicting test starting to pass makes the landing proven even though
    another convicting test stops earlier. Run `273278-22529e44` refuted
    eight landings with `gained` deltas on exactly this reading. Negative
    control: the pre-rule grade ("an earlier panic refuses whatever the
    others did") sees the same earlier move and would refute."""
    from pipeline_core.domain.assertion_position import TestKey, compare_positions
    e = _entry(state=BodyState.LANDED)
    k = TestKey("tests/test_x.rs", "test_early")
    before = SweepView(failed=frozenset({"test_gain", "test_early"}),
                       positions={k: 91})
    earlier = SweepView(passed=frozenset({"test_gain"}),
                        failed=frozenset({"test_early"}), positions={k: 90})
    state, delta = classify(e, attributed={"test_gain", "test_early"},
                            before=before, after=earlier, body_now_sha=e.body_sha)
    assert state is BodyState.PROVEN
    assert ("test_gain", "gained") in delta
    assert ("test_early", "regressed") not in delta
    moved_earlier, _later = compare_positions(before.positions, earlier.positions)
    assert moved_earlier            # what the pre-rule grade refuted on


def test_a_convicting_test_that_now_fails_an_earlier_assertion_is_refuted() -> None:
    """The 24951 shape: a landing whose convicting test was red before it and
    is red after it, now at an EARLIER line of its own body, nothing gained —
    `refuted` with a `(test, "regressed")` delta, never `flat`. Run
    `24951-a72c4642` (2026-09-29) graded exactly this shape `flat`
    (`:91` -> `:90`) and the flat row then excluded the unit.

    Negative control: the same landing read at the SAME line stays `flat`."""
    e = _entry(state=BodyState.LANDED)
    before = _red_at(91)
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=_red_at(90), body_now_sha=e.body_sha)
    assert state is BodyState.REFUTED
    assert delta == (("test_new", "regressed"),)
    assert not replace(e, state=state).reseedable
    same, _d = classify(e, attributed={"test_new"}, before=before,
                        after=_red_at(91), body_now_sha=e.body_sha)
    assert same is BodyState.FLAT


def test_a_stub_site_moving_earlier_in_src_is_not_a_position() -> None:
    """The run `273278-22529e44` shape: filling a fn lets a test reach a
    still-stubbed sibling declared earlier in the same `src/` file
    (`43 -> 23`). That is progress; the site is in the crate, has no
    assertion position, and grades `flat`, not `refuted`. Negative control:
    the raw sites the pre-fix grade compared are in one file and ordered
    earlier."""
    from pipeline_core.domain.test_report import parse_test_output
    e = _entry(state=BodyState.LANDED)

    def _src_red(line: int) -> SweepView:
        text = _LIBTEST_RED.format(line=line).replace(
            "panicked at tests/test_x.rs", "panicked at src/context.rs")
        return _red_at_capture(text)

    before, after = _src_red(43), _src_red(23)
    assert before.positions == {} and after.positions == {}
    state, delta = classify(e, attributed={"test_new"}, before=before,
                            after=after, body_now_sha=e.body_sha)
    assert state is BodyState.FLAT and delta == ()
    raw = [(f.panic_file, f.panic_line) for v in (43, 23)
           for f in parse_test_output(_LIBTEST_RED.format(line=v).replace(
               "panicked at tests/test_x.rs", "panicked at src/context.rs")).failures]
    assert raw[0][0] == raw[1][0] and raw[1][1] < raw[0][1]


def _red_at_capture(text: str) -> SweepView:
    from pipeline_core.domain.assertion_position import assertion_positions
    return SweepView(frozenset(), frozenset({"test_new"}), True, 0,
                     positions=assertion_positions(text, _oracle))


def test_a_position_only_refutation_under_a_superseded_comparator_is_reseedable(
) -> None:
    """A REFUTED row graded by line order (no comparator) whose delta is
    only `regressed` no longer refuses the reseed; one that lost a test
    does, and so does a position-only refutation under the current
    comparator. No row is edited: the gate reads the fields."""
    from pipeline_core.domain.body_ledger import COMPARATOR_VERSION, reseed_allowed
    old = replace(_entry(state=BodyState.REFUTED),
                  verdict_delta=(("t", "regressed"),), comparator="",
                  oracle_fp="o", layout_fp="L")
    assert old.refutation_superseded
    assert reseed_allowed(old, file_fp="L", sig_sha="", oracle_fp="o").allowed
    lost = replace(old, verdict_delta=(("t", "lost"), ("u", "regressed")))
    assert not reseed_allowed(lost, file_fp="L", sig_sha="", oracle_fp="o").allowed
    current = replace(old, comparator=COMPARATOR_VERSION)
    assert not reseed_allowed(current, file_fp="L", sig_sha="",
                              oracle_fp="o").allowed
    back = type(old).from_dict(current.to_dict())
    assert back is not None and back.comparator == COMPARATOR_VERSION


def test_a_landing_row_carries_the_bodies_it_replaced() -> None:
    """The sweep reverts a site-regressed landing to `prior_bodies`; the row
    must round-trip them, and a row written before the field reads as ()."""
    from pipeline_core.domain.body_ledger import LedgerEntry, raw_bodies_of
    recs = [{"file": "src/a.rs", "fn_name": "f", "body_text": " old(); // c "},
            {"file": "src/a.rs", "fn_name": "f", "body_text": " two() "},
            {"file": "src/b.rs", "fn_name": "g", "body_text": " g() "}]
    prior = raw_bodies_of(recs, [("src/a.rs", "f")])
    assert prior == {("src/a.rs", "f"): (" old(); // c ", " two() ")}
    e = replace(_entry(state=BodyState.LANDED),
                prior_bodies=prior[("src/a.rs", "f")])
    back = LedgerEntry.from_dict(e.to_dict())
    assert back is not None and back.prior_bodies == e.prior_bodies
    legacy = e.to_dict()
    legacy.pop("prior_bodies")
    old = LedgerEntry.from_dict(legacy)
    assert old is not None and old.prior_bodies == ()


def test_an_advanced_row_is_not_a_flat_exclusion_or_a_rebuy() -> None:
    from pipeline_core.domain.body_ledger import flat_standing_unchanged
    row = _entry(state=BodyState.ADVANCED, convicting=("test_new",),
                 evidence_fp="ev", verdict_delta=(("test_new", "advanced"),),
                 run_id="prior")
    standing = {_KEY: row}
    assert flat_standing_unchanged(
        standing, oracle_fp_now=_ORACLE,
        convicting_now={_KEY: frozenset({"test_new"})},
        evidence_now={_KEY: "ev"}) == set()
    assert flat_standing_unchanged(
        {_KEY: replace(row, state=BodyState.FLAT)}, oracle_fp_now=_ORACLE,
        convicting_now={_KEY: frozenset({"test_new"})},
        evidence_now={_KEY: "ev"}) == {_KEY}
    loc = FnLocator(_KEY.file, _KEY.fn_name)
    assert rebuy_hits([loc], standing, project=_KEY.project,
                      cluster=_KEY.cluster, run_id="now",
                      oracle_fp=_ORACLE) == ()


def test_a_landing_is_credited_only_with_what_its_own_keep_turned_green() -> None:
    """A convicting test turned green between two sweeps. The landing whose
    keep recorded that test is `proven`; a landing whose keep recorded other
    tests (or none) is a graded try that bought nothing, `flat`. Control: a
    row with no recorded keep comparison keeps the old reading."""
    from pipeline_core.domain.body_ledger import WaveKept, kept_green_of

    before, after = _views(({"t_other"}, {"t_conv"}), ({"t_other", "t_conv"}, set()))
    paid = _entry(kept_green=("t_conv",))
    sibling = _entry(kept_green=())
    legacy = _entry()
    assert classify(paid, attributed={"t_conv"}, before=before, after=after,
                    body_now_sha="")[0] is BodyState.PROVEN
    assert classify(sibling, attributed={"t_conv"}, before=before, after=after,
                    body_now_sha="")[0] is BodyState.FLAT
    assert classify(legacy, attributed={"t_conv"}, before=before, after=after,
                    body_now_sha="")[0] is BodyState.PROVEN
    assert LedgerEntry.from_dict(paid.to_dict()) == paid
    assert LedgerEntry.from_dict(legacy.to_dict()) == legacy

    u = FnLocator("src/a.rs", "f")
    kept = WaveKept({u}, [ReportKeep(frozenset({u}), frozenset({"t_conv"}))])
    assert kept == frozenset({u})
    assert kept_green_of(kept, u, {u: {"t_conv"}}) == ("t_conv",)
    assert kept_green_of(frozenset({u}), u, {u: {"t_conv"}}) is None
    # A legacy row: the only report that kept `u` recorded no comparison.
    unrecorded = WaveKept({u}, [ReportKeep(frozenset({u}), None)])
    assert kept_green_of(unrecorded, u, {u: {"t_conv"}}) is None


def test_a_test_of_one_container_is_not_credited_to_the_shared_locator() -> None:
    """The ledger row is one unit for every body of `(file, fn_name)`.
    When convicting names two containers, a test that convicts only one
    of them is not credited to that collapsed row. A test in every
    container, and a test the map does not split, stay. One container,
    and an empty-container site, leave the locator's tests unchanged."""
    from pipeline_core.domain.body_ledger import tests_creditable_to_locator
    from pipeline_core.domain.models import FnSite

    debug = FnSite("src/error.rs", "fmt", "impl fmt::Debug for Error")
    display = FnSite("src/error.rs", "fmt", "impl fmt::Display for Error")
    convicting = {debug: {"test_debug"}, display: {"test_display"}}
    assert tests_creditable_to_locator(
        {"test_debug", "test_display", "test_shared"},
        convicting, "src/error.rs", "fmt") == frozenset({"test_shared"})
    both = {debug: {"test_both", "test_debug"},
            display: {"test_both", "test_display"}}
    assert tests_creditable_to_locator(
        {"test_both", "test_debug"}, both, "src/error.rs", "fmt") == (
            frozenset({"test_both"}))
    assert tests_creditable_to_locator(
        {"test_debug"}, {debug: {"test_debug"}}, "src/error.rs", "fmt") == (
            frozenset({"test_debug"}))
    assert tests_creditable_to_locator(
        {"test_debug"},
        {FnSite("src/error.rs", "fmt", ""): {"test_debug"}},
        "src/error.rs", "fmt") == frozenset({"test_debug"})
    # A locator-keyed map names no container. The row's tests stay.
    from pipeline_core.domain.models import FnLocator
    assert tests_creditable_to_locator(
        {"t2"}, {FnLocator("src/a.rs", "depth"): {"t2"}},
        "src/a.rs", "depth") == frozenset({"t2"})


def test_a_test_two_kept_units_share_is_credited_to_neither() -> None:
    """One file row keeps two units and its comparison
    turned `t` green. The row ran one cargo invocation for the spliced
    file, so it cannot see which body paid. The old writer copied `{t}` onto
    both and both graded PROVEN. Credit goes only to a unit that is the sole
    kept unit of that report attributed to the test."""
    from pipeline_core.domain.body_ledger import WaveKept, credited_green, kept_green_of

    a, b = FnLocator("src/a.rs", "f"), FnLocator("src/a.rs", "g")
    row = ReportKeep(frozenset({a, b}), frozenset({"tests/x.rs::t"}))
    both = credited_green([row], {a: {"t"}, b: {"t"}})
    assert both == {a: frozenset(), b: frozenset()}
    before, after = _views((set(), {"t"}), ({"t"}, set()))
    for u in (a, b):
        entry = _entry(kept_green=tuple(sorted(both[u] or ())))
        assert classify(entry, attributed={"t"}, before=before, after=after,
                        body_now_sha="")[0] is BodyState.FLAT
    # Only the first unit attributed to `t`: it is proven, the second flat.
    only_a = credited_green([row], {a: {"t"}, b: {"other"}})
    assert only_a == {a: frozenset({"t"}), b: frozenset()}
    # A second report with no recorded comparison does not erase `{t}`.
    unrec = ReportKeep(frozenset({a}), None)
    assert credited_green([row, unrec], {a: {"t"}, b: {"other"}})[a] == {"t"}
    assert credited_green([unrec, row], {a: {"t"}, b: {"other"}})[a] == {"t"}
    wave = WaveKept({a, b}, [row, unrec])
    assert kept_green_of(wave, a, {a: {"t"}, b: {"other"}}) == ("t",)


# ---- a crash settlement's refutation carries the crash ----
#
# Run `757853-e28f1a7d` (2026-10-16): iter 3 refuted `into_boxed_dyn_error`'s
# double drop and stubbed it; the iter-4 prompt did not say so and the reply
# wrote it again three times. The row now carries the crash, and the stub's
# prompt reads it.

def test_a_refutation_s_crash_survives_the_index() -> None:
    e = _entry(state=BodyState.REFUTED, crash_evidence="SIGSEGV in t1",
               verdict_delta=(("t1", "crash_origin"),))
    back = LedgerEntry.from_dict(json.loads(json.dumps(e.to_dict())))
    assert back is not None and back.crash_evidence == "SIGSEGV in t1"
    # Absent on every other row, and on rows written before the field.
    assert "crash_evidence" not in _entry().to_dict()


def test_the_stub_is_told_the_last_crash_refutation_under_its_oracle() -> None:
    crash = _entry(state=BodyState.REFUTED, crash_evidence="double free",
                   verdict_delta=(("t1", "crash_origin"),), iteration=3)
    later = replace(crash, crash_evidence="use after free", iteration=4)
    other_oracle = replace(crash, oracle_fp="oracle-B", iteration=5)
    regressed = _entry(state=BodyState.REFUTED, crash_evidence="",
                       verdict_delta=(("t1", "regressed"),), iteration=6)
    rows = [crash, later, other_oracle, regressed]
    got = latest_crash_refutation(rows, "src/error.rs", "new", oracle_fp=_ORACLE)
    assert got is later
    assert latest_crash_refutation(
        rows, "src/error.rs", "other", oracle_fp=_ORACLE) is None
    # A refutation with no crash recorded (any other refuting grade) is not one.
    assert latest_crash_refutation(
        [regressed], "src/error.rs", "new", oracle_fp=_ORACLE) is None
