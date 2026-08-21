"""The proven-body ledger's domain rules: identity, the gate, the promotion.

The record this exists against: `.phase_c_logs/retrospectives.json` holds 39
records over 19 fields and not one of them is a positive claim, so every
deliberate destruction re-derived proven bodies through the model. Run
`259637-36db9aad` (2026-08-07) reverted 56 of 178 filled bodies and spent nine
hours, 148 landed fills and +5 tests without an exit — with the bodies on disk
the whole time.
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.body_ledger import (
    GATE_VERSION,
    BodyState,
    LayoutDep,
    LedgerEntry,
    LedgerKey,
    SweepView,
    authored_layout_fp,
    body_layout_deps,
    body_sha,
    classify,
    latest_by_unit,
    plan_reseed,
    reseed_allowed,
    sig_sha_of,
)
from pipeline_core.domain.retro_gate import layout_fingerprint

_KEY = LedgerKey(project="anyhow", cluster="stable", file="src/error.rs",
                 fn_name="new")


def _entry(**kw: object) -> LedgerEntry:
    base = {
        "key": _KEY, "body_sha": body_sha(("Error::from(e)",)),
        "bodies": ("Error::from(e)",), "sig_sha": sig_sha_of(("pub fn new",)),
        "layout_fp": "fp-original", "state": BodyState.PROVEN,
    }
    base.update(kw)
    return LedgerEntry(**base)      # type: ignore[arg-type]


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
    assert reseed_allowed(e, file_fp="fp-original",
                          sig_sha=e.sig_sha).allowed, "the fast path"
    # The file moved. Without a way to reach the tree the gate refuses…
    assert not reseed_allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha).allowed
    # …and with one, it asks whether the items THIS body references moved.
    assert reseed_allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha,
                          definition_present=lambda _d: True).allowed
    refused = reseed_allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha,
                             definition_present=lambda _d: False)
    assert not refused.allowed and refused.moved == ("ErrorImpl",)


def test_an_entry_with_no_dependency_list_is_gated_by_its_file_alone() -> None:
    """An empty check is not a pass. A body that names no authored item has
    nothing for the fallback to compare, so a moved file refuses it — costing
    a re-derivation rather than a tree that does not build."""
    e = _entry(layout_deps=())
    assert reseed_allowed(e, file_fp="fp-original", sig_sha=e.sig_sha).allowed
    refused = reseed_allowed(e, file_fp="fp-moved", sig_sha=e.sig_sha,
                             definition_present=lambda _d: True)
    assert not refused.allowed and refused.reason == "layout moved"


def test_the_gate_refuses_a_moved_signature_a_stale_version_and_a_refutation() -> None:
    e = _entry()
    assert not reseed_allowed(e, file_fp="fp-original",
                              sig_sha="other").allowed
    assert not reseed_allowed(replace(e, gate_ver="bl0"), file_fp="fp-original",
                              sig_sha=e.sig_sha).allowed
    assert not reseed_allowed(replace(e, state=BodyState.REFUTED),
                              file_fp="fp-original", sig_sha=e.sig_sha).allowed
    assert not reseed_allowed(replace(e, state=BodyState.LANDED),
                              file_fp="fp-original", sig_sha=e.sig_sha).allowed, (
        "a backfilled body no sweep has judged on THIS code is not re-seeded")
    assert reseed_allowed(replace(e, state=BodyState.UNMEASURED),
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


def test_the_plan_answers_only_about_the_units_it_was_asked_about() -> None:
    """The line between this store and a dispatch decider: it never adds a unit
    to anything, it answers about the stub set it is handed."""
    e = _entry()
    other = LedgerKey("anyhow", "stable", "src/kind.rs", "from")
    plan = plan_reseed([e], wanted=[_KEY, other],
                       file_fp={"src/error.rs": "fp-original"},
                       sig_sha={_KEY: e.sig_sha, other: "x"})
    assert [t.key for t in plan.take] == [_KEY]
    assert plan.skipped == []
