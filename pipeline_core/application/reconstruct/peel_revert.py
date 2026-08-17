"""Fn-granular revert of a multi-body splice that failed to compile.

The joint fill routes splice N bodies and commit them on ONE `cargo check`,
reverting all N when any single one fails. That is all-or-nothing at a width
the model does not reliably hit: `src/error.rs` in the anyhow reconstruction is
split into groups of 16 and 13 fns, and across runs 1920662-40f711b2,
2370111-1800b8b9, 2452699-e19bc335, 2509252-f1f8c11b and 2571732-149bc3cf
(2026-08-16/17) not one whole-group attempt ever compiled — six file-first
attempts in the last of those consumed an hour and landed nothing, leaving the
file on the same 39 `unimplemented!()` it had held for four runs. The errors
were ordinary and few: an invented `STD_ERROR_VTABLE`, `as_ptr` called on a
`Mut` that upstream reaches through `by_ref`, `ContextError` named at the crate
root. Three bad bodies were reverting thirteen good ones, every attempt.

Buying more attempts at the same width cannot fix that — the probability that
all N are simultaneously correct is what fails, so the expected yield per
attempt stays near zero however many are bought. This module makes an
attempt's yield proportional to what the model got right instead: revert only
the bodies the compiler's own diagnostics implicate, keep the rest, re-check.

The attribution is not invented here. `Diagnostic` already carries the primary
span, the macro invocation call-sites and every labelled secondary span, and
records in its own comment that those exist for "the real culprit a fn-granular
revert must reach"; `_frontier_from_diags` already walks all three to
`FnLocator(file, fn_name)`. The caller passes that mapping in.

`None` is the honest outcome whenever the diagnostics cannot isolate a proper
subset — nothing implicated, everything implicated, or a re-stub that fails.
The caller then reverts the whole file exactly as before, so this is strictly
additive: it can convert a total loss into a partial win, never a partial win
into a loss.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["LandedBody", "PeelResult", "peel_file_first", "peel_to_compiling"]


@dataclass(frozen=True, slots=True)
class LandedBody:
    """One body this attempt actually spliced, and where it went.

    `sig_text` is the declaration's verbatim signature. It is carried because a
    FREE fn's container is the empty string, which cannot narrow a same-named
    impl method at splice time — the same reason the routing tier carries it.
    """

    fn_name: str
    container: str
    sig_text: str = ""
    body: str = ""
    where_clause: str | None = None


@dataclass(frozen=True, slots=True)
class PeelResult:
    """The bodies that survived, the ones re-stubbed to get there, and how many
    check rounds it took."""

    kept: tuple[LandedBody, ...]
    reverted: tuple[str, ...]
    rounds: int


def peel_to_compiling(
    *,
    landed: Sequence[LandedBody],
    implicated_now: Callable[[], frozenset[str]],
    reapply: Callable[[Sequence[LandedBody]], bool],
    recheck: Callable[[], int],
    max_rounds: int = 3,
) -> PeelResult | None:
    """Drop the implicated bodies until the crate compiles, or give up.

    `implicated_now` reports the fn names the CURRENT diagnostics blame;
    `reapply` restores the pristine file and re-splices exactly the subset it
    is handed, reporting whether all of them landed; `recheck` re-runs the
    compile and returns its return code. All three are injected so the
    decision sequence is testable without cargo or a tree.

    Re-splicing a subset onto the pristine text is deliberate — the alternative,
    writing `unimplemented!()` over the bodies to drop, is refused by the splice
    itself (`body_is_stub` exists to stop a model returning a stub as an
    answer), and it would reach the tree through a path no other fill uses.
    This way the kept bodies land by the same call that landed them the first
    time.

    Bounded by `max_rounds` because each round costs a real compile, and an
    error set that keeps naming fresh bodies is one where the whole reply is
    wrong — precisely the case the caller's existing whole-file revert handles.
    """
    kept = list(landed)
    reverted: list[str] = []
    for rnd in range(1, max_rounds + 1):
        if not kept:
            return None
        bad = implicated_now() & {b.fn_name for b in kept}
        if not bad:
            # The compiler blames nothing this attempt wrote — the error is in
            # the scaffold, an import, or a sibling. Peeling this reply cannot
            # reach it, and dropping bodies on a guess would discard good ones.
            return None
        if len(bad) == len(kept):
            # Every body is implicated; there is no proper subset to keep, and
            # "revert them all" is exactly what the caller already does.
            return None
        reverted.extend(b.fn_name for b in kept if b.fn_name in bad)
        kept = [b for b in kept if b.fn_name not in bad]
        if not reapply(kept):
            return None
        if recheck() == 0:
            return PeelResult(tuple(kept), tuple(reverted), rnd)
    return None


def peel_file_first(
    *,
    file_path: Path,
    snapshot: str,
    file_rel: str,
    landed: Sequence[LandedBody],
    diags: Sequence[Any],
    target_src_dir: Path,
    check_cmd: str,
    cluster_env: Any,
    frontier_from_diags: Callable[..., Any],
    run_cargo_check_diag: Callable[..., tuple[int, Any, Sequence[Any]]],
    splice_fn_with_where: Callable[..., tuple[Any, Any, Any]],
    norm_src_rel: Callable[[str], str],
    max_rounds: int = 3,
) -> tuple[PeelResult | None, int, Any, Sequence[Any]]:
    """Wire :func:`peel_to_compiling` to a tree, returning the peel outcome
    together with the check state it leaves behind (`rc`, cargo output,
    diagnostics) so the caller can carry on with its own gates.

    Lives here rather than inline in the fill route because its collaborators
    are all reassigned inside that route's attempt loop, and closing over loop
    variables is how a late-binding bug gets written; taking them as arguments
    makes the binding explicit and the whole thing testable.
    """
    state: dict[str, Any] = {"rc": 101, "out": None, "diags": list(diags)}
    want = norm_src_rel(file_rel)

    def _implicated() -> frozenset[str]:
        return frozenset(
            k.fn_name for k in frontier_from_diags(state["diags"], target_src_dir)
            if norm_src_rel(k.file) == want)

    def _reapply(keep: Sequence[LandedBody]) -> bool:
        # Pristine text, then exactly the kept subset through the same splice
        # that landed them the first time.
        file_path.write_text(snapshot, encoding="utf-8")
        for b in keep:
            ok, _reason, _delta = splice_fn_with_where(
                file_path, b.container, b.fn_name, b.body, b.where_clause,
                sig_text=b.sig_text)
            if not ok:
                return False
        return True

    def _recheck() -> int:
        rc, out, d = run_cargo_check_diag(check_cmd, target_src_dir, cluster_env)
        state["rc"], state["out"], state["diags"] = rc, out, list(d)
        return rc

    result = peel_to_compiling(
        landed=landed, implicated_now=_implicated,
        reapply=_reapply, recheck=_recheck, max_rounds=max_rounds)
    return result, state["rc"], state["out"], state["diags"]
