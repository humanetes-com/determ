"""The decision sequence of the fn-granular revert, without cargo or a tree.

The pathology it exists for: `src/error.rs` is split into groups of 16 and 13
fns, and across runs 1920662-40f711b2, 2370111-1800b8b9, 2452699-e19bc335,
2509252-f1f8c11b and 2571732-149bc3cf (2026-08-16/17) not one whole-group
attempt compiled — six file-first attempts in the last of those spent an hour
and landed nothing, on ordinary errors in three of sixteen bodies.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelResult,
    peel_to_compiling,
)


def _bodies(*names: str) -> list[LandedBody]:
    return [LandedBody(n, "impl Error", f"fn {n}()") for n in names]


def _run(landed, blames, rcs, reapply_ok=True):
    """Drive the peel with scripted blame sets and check return codes."""
    blame_seq = list(blames)
    rc_seq = list(rcs)
    applied: list[list[str]] = []

    def _implicated():
        return frozenset(blame_seq.pop(0) if blame_seq else ())

    def _reapply(keep):
        applied.append([b.fn_name for b in keep])
        return reapply_ok

    def _recheck():
        return rc_seq.pop(0) if rc_seq else 101

    return peel_to_compiling(
        landed=landed, implicated_now=_implicated,
        reapply=_reapply, recheck=_recheck), applied


def test_keeps_the_bodies_the_compiler_does_not_blame() -> None:
    # The whole point: three bad bodies stop reverting thirteen good ones.
    landed = _bodies("a", "b", "c", "d")
    res, applied = _run(landed, blames=[{"b", "c"}], rcs=[0])
    assert isinstance(res, PeelResult)
    assert [k.fn_name for k in res.kept] == ["a", "d"]
    assert sorted(res.reverted) == ["b", "c"]
    # The kept subset is re-spliced onto the pristine file, never the dropped
    # ones written over with a stub.
    assert applied == [["a", "d"]]
    assert res.rounds == 1


def test_kept_bodies_carry_what_the_re_splice_needs() -> None:
    # A free fn's container is "", which cannot narrow a same-named impl
    # method at splice time — the signature is the only disambiguator, and the
    # body/where-clause are what the re-splice puts back.
    landed = [LandedBody("vtable", "", "unsafe fn vtable(p: NonNull<ErrorImpl>)",
                         "core::ptr::null()", None),
              LandedBody("construct", "impl Error", "fn construct()", "todo", "E: Send")]
    seen: list[LandedBody] = []

    def _reapply(keep):
        seen.extend(keep)
        return True

    res = peel_to_compiling(
        landed=landed, implicated_now=lambda: frozenset({"construct"}),
        reapply=_reapply, recheck=lambda: 0)
    assert res is not None and [k.fn_name for k in res.kept] == ["vtable"]
    assert seen[0].container == "" and "NonNull" in seen[0].sig_text
    assert seen[0].body == "core::ptr::null()"


def test_no_peel_when_the_error_blames_nothing_this_reply_wrote() -> None:
    # The error is in the scaffold, an import or a sibling. Re-stubbing on a
    # guess would discard good bodies, so the caller's whole revert stands.
    res, applied = _run(_bodies("a", "b"), blames=[{"elsewhere"}], rcs=[0])
    assert res is None and applied == []


def test_no_peel_when_every_body_is_implicated() -> None:
    # There is no proper subset to keep, and reverting them all is exactly
    # what the caller already does.
    res, applied = _run(_bodies("a", "b"), blames=[{"a", "b"}], rcs=[0])
    assert res is None and applied == []


def test_gives_up_when_the_subset_cannot_be_re_spliced() -> None:
    # A kept body that will not land leaves the file in a state this module
    # did not intend; the caller's verbatim revert is the safe answer.
    res, _ = _run(_bodies("a", "b"), blames=[{"a"}], rcs=[0], reapply_ok=False)
    assert res is None


def test_peels_across_rounds_when_the_first_peel_is_not_enough() -> None:
    # Reverting `b` uncovers a second error naming `c`; the second peel
    # compiles. Both are reported as reverted.
    landed = _bodies("a", "b", "c")
    res, _ = _run(landed, blames=[{"b"}, {"c"}], rcs=[101, 0])
    assert res is not None
    assert [k.fn_name for k in res.kept] == ["a"]
    assert sorted(res.reverted) == ["b", "c"] and res.rounds == 2


def test_bounded_when_every_round_names_a_fresh_body() -> None:
    # An error set that keeps naming new bodies is a wholly wrong reply —
    # each round costs a real compile, so it stops and lets the caller revert.
    landed = _bodies("a", "b", "c", "d", "e")
    res, _ = _run(landed, blames=[{"a"}, {"b"}, {"c"}, {"d"}],
                  rcs=[101, 101, 101, 0])
    assert res is None


def test_single_body_attempt_has_no_subset_to_keep() -> None:
    res, _ = _run(_bodies("a"), blames=[{"a"}], rcs=[0])
    assert res is None
