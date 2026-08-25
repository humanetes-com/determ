"""The decision sequence of the fn-granular revert, without cargo or a tree.

The pathology it exists for: `src/error.rs` is split into groups of 16 and 13
fns, and across runs 1920662-40f711b2, 2370111-1800b8b9, 2452699-e19bc335,
2509252-f1f8c11b and 2571732-149bc3cf (2026-08-16/17) not one whole-group
attempt compiled — six file-first attempts in the last of those spent an hour
and landed nothing, on ordinary errors in three of sixteen bodies.

The two defects the peel itself then produced, both run `57025-6ba652f2`
(2026-08-24), are pinned below with their recorded instances: the name-keyed
implication that deleted a correct body's same-named sibling and returned a
total loss as a peel win, and the fixed round budget that could cut a
converging walk short.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelRefusal,
    PeelResult,
    peel_to_compiling,
)


def _bodies(*names: str) -> list[LandedBody]:
    return [LandedBody(n, "impl Error", f"fn {n}()") for n in names]


def _keys(*names: str) -> set[tuple[str, str]]:
    return {(n, "impl Error") for n in names}


def _run(landed, blames, rcs, reapply_ok=True, max_rounds=None):
    """Drive the peel with scripted blame KEY sets and check return codes."""
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
        reapply=_reapply, recheck=_recheck, max_rounds=max_rounds), applied


def test_keeps_the_bodies_the_compiler_does_not_blame() -> None:
    # The whole point: three bad bodies stop reverting thirteen good ones.
    landed = _bodies("a", "b", "c", "d")
    res, applied = _run(landed, blames=[_keys("b", "c")], rcs=[0])
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
        landed=landed,
        implicated_now=lambda: frozenset({("construct", "impl Error")}),
        reapply=_reapply, recheck=lambda: 0)
    assert isinstance(res, PeelResult)
    assert [k.fn_name for k in res.kept] == ["vtable"]
    assert seen[0].container == "" and "NonNull" in seen[0].sig_text
    assert seen[0].body == "core::ptr::null()"


def test_a_doomed_impl_does_not_delete_its_same_named_sibling() -> None:
    """Implication is per BODY, `(fn_name, one-line container)` — a bare name
    is not an identity. The recorded instance: run `57025-6ba652f2`'s
    context.rs reply held two `ext_context` impls; the Error-impl body failed
    E0277, the generic body was the first correct-shaped `ext_context` in the
    project's record, and the name-keyed drop deleted BOTH
    (`phase_c.file.fill.peeled` 17:40:17 UTC: reverted six, kept 0,
    rounds 1). Here only the blamed impl's body drops; the sibling ships."""
    landed = [
        LandedBody("ext_context", "impl StdError for E",
                   "fn ext_context<C>(self, context: C) -> Error",
                   "Error::construct_from_context(context, self, bt)"),
        LandedBody("ext_context", "impl StdError for Error",
                   "fn ext_context<C>(self, context: C) -> Error",
                   "Error::construct_from_context(context, self, bt)"),
        LandedBody("context", "impl<T, E> Context<T, E> for Result<T, E>",
                   "fn context<C>(self, context: C) -> Result<T, Error>"),
    ]
    res, applied = _run(
        landed,
        blames=[{("ext_context", "impl StdError for Error")}],
        rcs=[0])
    assert isinstance(res, PeelResult)
    assert [(k.fn_name, k.container) for k in res.kept] == [
        ("ext_context", "impl StdError for E"),
        ("context", "impl<T, E> Context<T, E> for Result<T, E>")]
    assert res.reverted == ("ext_context",)
    assert applied == [["ext_context", "context"]]


def test_implication_matches_across_the_two_container_printers() -> None:
    """The implication keys come from the EXTRACTOR's records (generics list
    omitted, token-spaced, no where clause); the landed body's container is
    SOURCE-shaped (generics list, source spacing, possibly a where clause).
    On the peel's first live outing the two never intersected and every
    context.rs failure refused `none_implicated` — the whole-reply revert
    again, on the build that carried the fix (run `264297-dd681686`,
    2026-08-24 ~22:5x UTC, `peel refused (none_implicated) after 1
    round(s)`). `container_key` now normalizes both printers to one form."""
    landed = [
        LandedBody("context",
                   "impl<T, E> Context<T, E> for Result<T, E>\nwhere\n"
                   "    E: ext::StdError + Send + Sync + 'static,",
                   "fn context<C>(self, context: C) -> Result<T, Error>"),
        LandedBody("ext_context", "impl<E> StdError for E\nwhere\n"
                   "    E: crate::StdError + Send + Sync + 'static,",
                   "fn ext_context<C>(self, context: C) -> Error"),
    ]
    from pipeline_core.domain.attribution import container_key
    extractor_shaped = container_key(
        "impl Context < T , E > for Result < T , E >")
    res, applied = _run(
        landed, blames=[{("context", extractor_shaped)}], rcs=[0])
    assert isinstance(res, PeelResult)
    assert res.reverted == ("context",)
    assert [k.fn_name for k in res.kept] == ["ext_context"]
    assert applied == [["ext_context"]]


def test_all_bodies_implicated_refuses_whatever_the_name_count_is() -> None:
    """The all-implicated guard compares the same units the drop uses. Six
    bodies over three names slipped the old name-vs-body comparison (3 != 6),
    every body was dropped, the empty subset re-applied onto the pristine
    snapshot compiled, and a TOTAL loss returned as `PeelResult(kept=())` —
    the recorded `PEELED 6 … remaining 0 compile`. Same shape now refuses."""
    landed = []
    for cont in ("impl A", "impl B"):
        for name in ("context", "with_context", "ext_context"):
            landed.append(LandedBody(name, cont, f"fn {name}()"))
    all_keys = {(b.fn_name, b.container) for b in landed}
    res, applied = _run(landed, blames=[all_keys], rcs=[0])
    assert isinstance(res, PeelRefusal)
    assert res.reason == "all_implicated"
    assert applied == []


def test_no_peel_when_the_error_blames_nothing_this_reply_wrote() -> None:
    # The error is in the scaffold, an import or a sibling. Re-stubbing on a
    # guess would discard good bodies, so the caller's whole revert stands.
    res, applied = _run(_bodies("a", "b"),
                        blames=[{("elsewhere", "impl Error")}], rcs=[0])
    assert isinstance(res, PeelRefusal) and res.reason == "none_implicated"
    assert applied == []


def test_gives_up_when_the_subset_cannot_be_re_spliced() -> None:
    # A kept body that will not land leaves the file in a state this module
    # did not intend; the caller's verbatim revert is the safe answer.
    res, _ = _run(_bodies("a", "b"), blames=[_keys("a")], rcs=[0],
                  reapply_ok=False)
    assert isinstance(res, PeelRefusal) and res.reason == "reapply_failed"
    assert res.dropped == ("a",)


def test_peels_across_rounds_when_the_first_peel_is_not_enough() -> None:
    # Reverting `b` uncovers a second error naming `c`; the second peel
    # compiles. Both are reported as reverted.
    landed = _bodies("a", "b", "c")
    res, _ = _run(landed, blames=[_keys("b"), _keys("c")], rcs=[101, 0])
    assert isinstance(res, PeelResult)
    assert [k.fn_name for k in res.kept] == ["a"]
    assert sorted(res.reverted) == ["b", "c"] and res.rounds == 2


def test_converges_past_the_old_three_round_budget() -> None:
    """rustc reports errors in WAVES — each drop can surface diagnostics the
    previous abort masked — and every proceeding round drops at least one
    body, so the walk terminates on its own within len(landed) rounds. The
    old fixed `max_rounds=3` returned a total loss at exactly this point
    while the walk was still converging: the give-up mid-progress the
    module's own contract forbids. Four waves over five bodies now land the
    survivor."""
    landed = _bodies("a", "b", "c", "d", "e")
    res, _ = _run(landed,
                  blames=[_keys("a"), _keys("b"), _keys("c"), _keys("d")],
                  rcs=[101, 101, 101, 0])
    assert isinstance(res, PeelResult)
    assert [k.fn_name for k in res.kept] == ["e"]
    assert res.rounds == 4
    # The pre-fix budget, passed back explicitly, reproduces the old refusal:
    # the negative control for the structural bound.
    res_old, _ = _run(landed,
                      blames=[_keys("a"), _keys("b"), _keys("c"), _keys("d")],
                      rcs=[101, 101, 101, 0], max_rounds=3)
    assert isinstance(res_old, PeelRefusal)
    assert res_old.reason == "exhausted_rounds"
    assert res_old.dropped == ("a", "b", "c")


def test_single_body_attempt_has_no_subset_to_keep() -> None:
    res, _ = _run(_bodies("a"), blames=[_keys("a")], rcs=[0])
    assert isinstance(res, PeelRefusal) and res.reason == "all_implicated"


def test_reapply_replays_the_overwrite_authorization(tmp_path) -> None:
    """The re-splice must land refills with the SAME `allow_nonstub` the
    original splice had: a refill's pristine body is non-stub by definition,
    and a reapply without the flag refuses `target_not_stub` on the first
    kept refill — so every peel of an all-refill file died `reapply_failed`
    the moment the implication finally worked (run `349271-d876e4a9`,
    2026-08-25: context.rs dropped a proper subset of 2 for the first time
    and lost the whole reply anyway). The negative control is the same walk
    with the authorization stripped — the recorded live shape."""
    from pipeline_core.application.reconstruct.peel_revert import (
        peel_file_first,
    )
    f = tmp_path / "context.rs"
    f.write_text("pristine", encoding="utf-8")

    def _splice(_path, _cont, _fn, _body, _wc, allow_nonstub=False,
                sig_text=""):
        # Every target in the pristine snapshot is a compiled refill body:
        # the splice refuses a non-stub overwrite without the authorization.
        if not allow_nonstub:
            return False, "target_not_stub", 0
        return True, "", 1

    def _drive(authorized: bool):
        landed = [
            LandedBody("context", "impl<T, E> Context<T, E> for Result<T, E>",
                       "fn context()", "bad", None,
                       overwrite_authorized=authorized),
            LandedBody("ext_context", "impl<E> StdError for E",
                       "fn ext_context()", "good", None,
                       overwrite_authorized=authorized),
        ]
        return peel_file_first(
            file_path=f, snapshot="pristine", file_rel="src/context.rs",
            landed=landed, diags=[], target_src_dir=tmp_path,
            check_cmd="cargo check", cluster_env={},
            implicated_keys_from_diags=lambda _d, _s: frozenset(
                {("src/context.rs", "context",
                  "impl Context<T,E>for Result<T,E>")}),
            run_cargo_check_diag=lambda *_a: (0, "", []),
            splice_fn_with_where=_splice,
            norm_src_rel=lambda s: s)

    res, _rc, _out, _diags = _drive(authorized=True)
    assert isinstance(res, PeelResult)
    assert [k.fn_name for k in res.kept] == ["ext_context"]
    assert res.reverted == ("context",)
    res_old, _rc2, _o2, _d2 = _drive(authorized=False)
    assert isinstance(res_old, PeelRefusal)
    assert res_old.reason == "reapply_failed"
