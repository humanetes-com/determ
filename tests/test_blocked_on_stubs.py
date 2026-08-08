"""Pins for the third way a stored conviction rots: it was earned against a
tree whose holes have since been filled.

`RetroProvenance` asks whether the verdict was earned in a comparable WORLD
(cluster, layout fingerprint, gate, routes) and `ConvictionEvidence` asks
whether the failures it was drawn from still convict. Neither can see the case
that held `anyhow` for two days — a unit convicted because the fns it CALLS were
`unimplemented!()`, with the cluster, layout, gate, routes and blame map all
unmoved when those fns filled.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.retros import (
    load_retrospectives,
    retire_unblocked_convictions,
    retro_key,
    save_retrospectives,
)
from pipeline_core.domain.retro_gate import (
    BlockedOnStubs,
    ConvictionEvidence,
    RetroProvenance,
    cleared_blockers,
    stamp_conviction,
)
from pipeline_core.domain.rust_scan import stubbed_fn_names

# The shape the live tree carries: the skeletoniser writes both spellings, and a
# body reverted after a failed attempt keeps the attempt and the cargo error as
# comments above the marker (`make_failed_attempt_body`), so it is 20-40 lines
# long and matching a BARE body would report it as filled.
_TREE = '''
pub struct ErrorVTable {
    object_drop: unsafe fn(Own<ErrorImpl>),
    object_drop_rest: unsafe fn(Own<ErrorImpl>, TypeId),
}

impl Error {
    pub fn new<E>(error: E) -> Self {
        // SKELETON_STUB_FAILED_ATTEMPT
        // Previous attempt failed compilation after 2 fix-loop attempts.
        // ATTEMPTED BODY:
        //   let vtable = ErrorVTable { object_drop: object_drop::<E> };
        //   unsafe { Self::construct(error, vtable, backtrace) }
        // CARGO ERROR (focused excerpt):
        //   error[E0425]: cannot find value `object_drop_rest` in this scope
        unimplemented!()
    }

    pub fn msg<M>(message: M) -> Self {
        Self::from_adhoc(message, backtrace!())
    }
}

unsafe fn object_drop<E>(e: Own<ErrorImpl>) {
    ::core::unimplemented!()
}

unsafe fn object_drop_front<E>(e: Own<ErrorImpl>, target: TypeId) {
    let unerased = e.cast::<ErrorImpl<ManuallyDrop<E>>>().boxed();
    drop(unerased);
}

trait StdError {
    fn source(&self) -> Option<&(dyn StdError + 'static)>;
}
'''


def test_a_hollow_body_is_found_in_both_spellings_and_under_comments() -> None:
    stubs = stubbed_fn_names(_TREE)
    assert "new" in stubs, "a reverted body is comments plus the marker"
    assert "object_drop" in stubs, "the `::core::` spelling counts"
    assert "msg" not in stubs and "object_drop_front" not in stubs


def test_a_bodyless_declaration_is_not_a_hole() -> None:
    """A trait method's `fn source(&self) -> ...;` declares no body to fill, and
    an `extern` block's fns are the same. Counting them would make every trait
    in the file look like a producer that never fills."""
    assert "source" not in stubbed_fn_names(_TREE)


def test_a_fn_pointer_field_declares_nothing() -> None:
    """`object_drop_rest: unsafe fn(Own<ErrorImpl>, TypeId)` is a vtable FIELD
    whose type happens to be a fn — the live shape, and the exact name the model
    kept mistaking for a function. It has no body and must not be reported, and
    walking past it must not consume the rest of the struct."""
    stubs = stubbed_fn_names(_TREE)
    assert "object_drop_rest" not in stubs
    # The items declared after the struct are still visible.
    assert "new" in stubs and "object_drop" in stubs


def test_an_empty_file_has_no_holes() -> None:
    assert stubbed_fn_names("") == frozenset()
    assert stubbed_fn_names("// just a comment\n") == frozenset()


def test_blockers_are_the_stubs_the_attempt_actually_called() -> None:
    """Narrow on purpose. A file mid-reconstruction is mostly holes, so "any
    stub in this file filled" would release every conviction almost every
    iteration and buy the retries with activity instead of evidence — the same
    defect `readmission_warranted` documents from the other direction."""
    body = "let vtable = ErrorVTable { object_drop: object_drop::<E> };"
    blockers = BlockedOnStubs.at_conviction(body, _TREE, exclude="new")
    assert blockers.stubs == frozenset({"object_drop"})


def test_a_conviction_does_not_list_itself() -> None:
    """The convicted fn's own body is a stub while the failed attempt is being
    judged. A record that listed itself would be released by its own eventual
    fill, which is not evidence about anything."""
    body = "Self::new(error)"
    assert BlockedOnStubs.at_conviction(body, _TREE, exclude="new").stubs == frozenset()
    # ...and with no `exclude` (the impl-block key, `*impl*`, has no single fn
    # of its own to leave out) it IS listed: the block's members are among the
    # stubs whose filling releases it.
    assert BlockedOnStubs.at_conviction(body, _TREE).stubs == frozenset({"new"})


def test_silence_is_not_a_release() -> None:
    """A record with no `blocked_on` stamp — earned before the stamp existed, or
    drawn from compile errors that named no sibling — asserts nothing about the
    tree and is left to the provenance and evidence gates."""
    assert cleared_blockers({}, frozenset()) == frozenset()
    assert cleared_blockers({"fully_exhausted": True}, frozenset()) == frozenset()
    assert cleared_blockers(None, frozenset({"x"})) == frozenset()
    # A malformed stamp is treated as no stamp, never as "everything cleared".
    assert cleared_blockers({"blocked_on": "object_drop"}, frozenset()) == frozenset()


def test_a_blocker_still_hollow_releases_nothing() -> None:
    rec = {"blocked_on": ["object_drop", "construct"]}
    assert cleared_blockers(rec, frozenset({"object_drop", "construct"})) == frozenset()
    assert cleared_blockers(rec, frozenset({"object_drop"})) == frozenset({"construct"})


def test_stamp_conviction_carries_all_three_axes() -> None:
    rec = stamp_conviction(
        {"fully_exhausted": True},
        RetroProvenance(cluster="stable", layout_fp="2b552a32"),
        ConvictionEvidence(frozenset({"test_convert"})),
        BlockedOnStubs(frozenset({"construct", "object_drop"})),
    )
    assert rec["cluster"] == "stable" and rec["layout_fp"] == "2b552a32"
    assert rec["convicted_by"] == ["test_convert"]
    assert rec["blocked_on"] == ["construct", "object_drop"]
    # A caller with no snapshot in hand still writes a well-formed record; it
    # simply carries no tree claim.
    plain = stamp_conviction(
        {}, RetroProvenance(cluster="stable", layout_fp="x"),
        ConvictionEvidence(frozenset()))
    assert "blocked_on" not in plain


# --------------------------------------------------------------------------- #
# The store-level release.
# --------------------------------------------------------------------------- #
_NEW_KEY = retro_key("src/error.rs", "new", "impl Error")
_IMPL_KEY = retro_key("src/error.rs", "*impl*", "impl Error")


def _seed(project_dir: Path, records: dict) -> None:
    """Every record carries the cluster stamp `stamp_conviction` writes, because
    the release is scoped to the sweeping cluster."""
    save_retrospectives(project_dir, {
        k: {"cluster": "stable", **v} for k, v in records.items()})


def test_the_live_deadlock_releases_when_the_vtable_fills(tmp_path: Path) -> None:
    """The reproduction, in the store's own terms.

    `src/error.rs::new` was convicted GENUINELY_HARD on 2026-08-07 with the
    guidance *"The vtable functions referenced in the body (`object_drop`,
    `object_ref`, `object_boxed`, `object_downcast`, `object_drop_front`, etc.)
    are not implemented in the crate — they panic with 'not implemented' ...
    this cannot be fixed by body-only changes."* Every fn that sentence names
    was filled on disk by the time it mattered, and the record still skipped the
    unit on every dispatch, because the only thing that could clear it was a
    cluster-wide score move the unit itself was blocking."""
    _seed(tmp_path, {_NEW_KEY: {
        "category": "GENUINELY_HARD", "fully_exhausted": True,
        "guidance": "The vtable functions referenced in the body are not "
                    "implemented in the crate.",
        "blocked_on": ["construct", "object_drop", "object_ref"],
    }})
    # The sweep's stub set: all three have filled.
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset()}, "stable") == 1
    rec = load_retrospectives(tmp_path)[_NEW_KEY]
    assert rec["fully_exhausted"] is False
    assert rec["blocked_on"] == []
    # The LESSON survives the release — the retry is meant to be informed.
    assert "vtable functions" in rec["guidance"]
    assert rec["category"] == "GENUINELY_HARD"


def test_a_release_is_bought_by_a_distinct_fill_and_runs_out(tmp_path: Path) -> None:
    """Bounded without an attempt cap, which is what lets it stay outside the
    score gate. Each release narrows the stored set to the fns still hollow, and
    a stub set only shrinks — so a record can be released at most once per fn it
    recorded, and the last release leaves nothing to trigger another."""
    _seed(tmp_path, {_NEW_KEY: {
        "fully_exhausted": True, "blocked_on": ["construct", "object_drop"]}})

    # `construct` fills; `object_drop` is still a hole.
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset({"object_drop"})}, "stable") == 1
    assert load_retrospectives(tmp_path)[_NEW_KEY]["blocked_on"] == ["object_drop"]

    # Re-exhausted by a fresh failed attempt, with nothing new having filled.
    retros = load_retrospectives(tmp_path)
    retros[_NEW_KEY]["fully_exhausted"] = True
    save_retrospectives(tmp_path, retros)
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset({"object_drop"})}, "stable") == 0
    assert load_retrospectives(tmp_path)[_NEW_KEY]["fully_exhausted"] is True

    # ...and the second, distinct fill buys the second and last release.
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset()}, "stable") == 1
    retros = load_retrospectives(tmp_path)
    retros[_NEW_KEY]["fully_exhausted"] = True
    save_retrospectives(tmp_path, retros)
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset()}, "stable") == 0


def test_a_record_that_is_not_convicted_is_left_alone(tmp_path: Path) -> None:
    """The release only ever clears a flag that is set. A record carrying a
    lesson but no conviction has nothing to release, and rewriting its
    `blocked_on` would silently spend a future release for nothing."""
    _seed(tmp_path, {_NEW_KEY: {
        "fully_exhausted": False, "blocked_on": ["construct"]}})
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset()}, "stable") == 0
    assert load_retrospectives(tmp_path)[_NEW_KEY]["blocked_on"] == ["construct"]


def test_a_file_with_no_stubs_left_is_absent_and_that_releases(tmp_path: Path) -> None:
    """The caller passes the WHOLE of what the sweep found, so a file absent
    from the map has nothing hollow left in it. That reading is what releases a
    record whose last blocker filled — there is no other signal for it."""
    _seed(tmp_path, {_NEW_KEY: {
        "fully_exhausted": True, "blocked_on": ["construct"]}})
    assert retire_unblocked_convictions(
        tmp_path, {"src/context.rs": frozenset({"with_context"})}, "stable") == 1


def test_another_clusters_conviction_is_not_released(tmp_path: Path) -> None:
    """The store is shared and the stub scan is cluster-scoped: `src/nightly.rs`
    is absent from every `stable` sweep while still being `unimplemented!()` on
    disk. Without the scope every `stable` sweep would release the whole of
    `nightly`'s convictions on evidence that was never about them."""
    save_retrospectives(tmp_path, {_NEW_KEY: {
        "cluster": "nightly", "fully_exhausted": True,
        "blocked_on": ["construct"]}})
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset({"other"})}, "stable") == 0
    # A record with NO cluster stamp is legacy — an unknown context is not this
    # one, the same rule the provenance gate applies to the verdict itself.
    save_retrospectives(tmp_path, {_NEW_KEY: {
        "fully_exhausted": True, "blocked_on": ["construct"]}})
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset({"other"})}, "stable") == 0


def test_a_sweep_that_found_no_stubs_at_all_releases_nothing(tmp_path: Path) -> None:
    """A mid-reconstruction cluster with no hollow fn ANYWHERE is far likelier
    to be a scan that failed than a tree that finished — this pipeline has
    already had a walk report a crate with 186 fns and no tests because it would
    not follow a symlink. The cost of being wrong is a full frontier of model
    calls on units already convicted; the cost of refusing is nothing, because a
    finished tree is about to exit the loop anyway."""
    _seed(tmp_path, {_NEW_KEY: {
        "fully_exhausted": True, "blocked_on": ["construct"]}})
    assert retire_unblocked_convictions(tmp_path, {}, "stable") == 0
    assert load_retrospectives(tmp_path)[_NEW_KEY]["fully_exhausted"] is True


def test_an_empty_store_is_a_no_op(tmp_path: Path) -> None:
    assert retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset()}, "stable") == 0


def test_the_store_stays_readable_json(tmp_path: Path) -> None:
    """`blocked_on` is persisted as a sorted list so the file diffs cleanly
    across runs — the store is read by hand when a run goes wrong."""
    _seed(tmp_path, {_IMPL_KEY: {
        "fully_exhausted": True, "blocked_on": ["zeta", "alpha"]}})
    retire_unblocked_convictions(
        tmp_path, {"src/error.rs": frozenset({"zeta"})}, "stable")
    raw = json.loads(
        (tmp_path / ".phase_c_logs" / "retrospectives.json").read_text("utf-8"))
    assert raw[_IMPL_KEY]["blocked_on"] == ["zeta"]
