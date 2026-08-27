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

from pipeline_core.domain.body_ledger import (
    GATE_VERSION,
    BodyState,
    LayoutDep,
    LedgerEntry,
    LedgerKey,
    ReseedPlan,
    SweepView,
    authored_layout_fp,
    body_layout_deps,
    body_sha,
    classify,
    latest_by_unit,
    oracle_fingerprint,
    plan_reseed,
    rebuy_hits,
    reseed_allowed,
    rows_for_units,
    sig_sha_of,
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
    for bad in (None, [], {}, {"project": "anyhow"},
                {**e.to_dict(), "state": "hopeful"}):
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


def test_tests_that_ran_and_are_all_still_red_prove_nothing() -> None:
    """The subtler half of the same defect. Nothing regressed, so this is not a
    refutation — but every attributed test is still failing, so the body has
    been shown not to have BROKEN anything, which is not the same as being
    right. `proven` has to mean a test passes."""
    e = _entry(state=BodyState.LANDED)
    before, after = _views((set(), {"test_new"}), (set(), {"test_new"}))
    state, _ = classify(e, attributed={"test_new"}, before=before, after=after,
                        body_now_sha=e.body_sha)
    assert state is BodyState.UNMEASURED


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

def test_flat_is_a_grade_and_never_a_seed_and_classify_never_produces_it() -> None:
    flat = _entry(state=BodyState.FLAT)
    assert not flat.reseedable
    assert reseed_allowed(flat, file_fp="fp-original", sig_sha=flat.sig_sha,
                          oracle_fp=_ORACLE).reason == "state flat"
    # `classify` is untouched: over the four readings a sweep can give, the
    # verdict is one of the three it always was.
    e = _entry(state=BodyState.LANDED)
    for before, after in (((set(), set()), ({"o"}, set())),
                          (({"t"}, set()), (set(), {"t"})),
                          ((set(), {"t"}), (set(), {"t"})),
                          ((set(), {"t"}), ({"t"}, set()))):
        b, a = _views(before, after)
        state, _ = classify(e, attributed={"t"}, before=b, after=a,
                            body_now_sha=e.body_sha)
        assert state in (BodyState.UNMEASURED, BodyState.REFUTED,
                         BodyState.PROVEN)


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
