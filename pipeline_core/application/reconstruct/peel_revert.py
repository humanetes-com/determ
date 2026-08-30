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
`attribution.implicated_fn_keys` walks all three to the whole fn record. The
caller passes that mapping in.

Two defects this module itself produced, both on run `57025-6ba652f2`
(2026-08-24), both fixed here and pinned in `tests/test_peel_revert.py`:

- **Implication was keyed by fn NAME, and two impls legally share one.** The
  context.rs reply held six bodies over three names (two `ext_context` impls
  among them); the name-keyed drop removed BOTH impls per implicated name,
  the all-implicated guard compared 3 names against 6 bodies and did not
  fire, `reapply([])` restored the pristine snapshot (which compiled), and a
  TOTAL loss returned as a peel win (`phase_c.file.fill.peeled` 17:40:17 UTC:
  reverted six, kept 0, rounds 1) — deleting the first correct-shaped generic
  `ext_context` in the project's record alongside its doomed concrete-self
  sibling. Implication, drop and the guard are now all per BODY:
  `(fn_name, one-line container)`.
- **A fixed round budget returned a total loss mid-progress.** Every round
  that finds a proper implicated subset drops at least one body, so the loop
  terminates on its own in at most len(landed) rounds — each one cargo
  check, seconds — at compiling, none-implicated, or all-implicated. The
  old `max_rounds=3` cut that walk short while it was still converging,
  which is the one outcome the module's own contract forbids ("can convert
  a total loss into a partial win, never a partial win into a loss"). The
  cap is now structural; an explicit override remains for tests.

A refusal is the honest outcome whenever the diagnostics cannot isolate a
proper subset — nothing implicated, everything implicated, or a re-splice
that fails. The caller then reverts the whole file exactly as before, so this
is strictly additive. The refusal now says WHICH path declined and what had
been dropped by then: run `57025-6ba652f2`'s three error.rs reverts left no
record of their peel path at all (no event is emitted on a decline), and the
one-error record that survives — E0277 primary span at `context`, a proper
subset — proves only that round 1 progressed, not where the walk died.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.attribution import container_key
from pipeline_core.domain.rust_scan import stubbed_fn_names

__all__ = [
    "LandedBody",
    "PeelRefusal",
    "PeelResult",
    "peel_file_first",
    "peel_keep_allowed",
    "peel_to_compiling",
]


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
    # Whether the ORIGINAL splice was authorized to overwrite a non-stub body
    # (`allow_nonstub` — a refill target). The re-splice must replay the same
    # authorization: a refill's pristine body is non-stub by definition, and a
    # reapply without the flag refuses `target_not_stub` on the first kept
    # refill — every peel of an all-refill file then dies `reapply_failed`
    # (run `349271-d876e4a9`, 2026-08-25: context.rs implicated a proper
    # subset for the first time, dropped 2, and lost the whole reply anyway).
    overwrite_authorized: bool = False

    @property
    def key(self) -> tuple[str, str]:
        """The identity implication and drop run on: (fn_name, one-line
        container). A bare name is not an identity — two impls share one."""
        return (self.fn_name, container_key(self.container))


@dataclass(frozen=True, slots=True)
class PeelResult:
    """The bodies that survived, the ones re-stubbed to get there, and how many
    check rounds it took."""

    kept: tuple[LandedBody, ...]
    reverted: tuple[str, ...]
    rounds: int


@dataclass(frozen=True, slots=True)
class PeelRefusal:
    """Why the peel declined, so a decline leaves a record — the three
    error.rs reverts of run `57025-6ba652f2` left none, and which None-path
    they took is unrecoverable from that run.

    `reason` is one of: `none_implicated` (the compiler blames nothing this
    attempt wrote — scaffold, import or sibling), `all_implicated` (no proper
    subset to keep; whole-file revert is exactly right), `reapply_failed`
    (a kept body would not re-splice), `no_bodies`, `exhausted_rounds` (an
    explicit `max_rounds` override ran out — unreachable under the
    structural bound). `dropped` is what had been peeled before declining;
    the caller's whole-file revert restores those along with everything else.
    """

    reason: str
    rounds: int
    dropped: tuple[str, ...] = ()


def peel_keep_allowed(
    snapshot: str, current: str, reverted: Sequence[str],
) -> bool:
    """Whether peel survivors may take an unmeasurable keep.

    True when nothing was re-stubbed, or when this file's hollow set strictly
    shrank versus the pre-attempt snapshot (some stubs became bodies). False
    when the peel put names back as stubs and the hollow count did not drop:
    the survivors are rewrites of already-filled bodies, and keeping them is
    how run `1160135-7b10cdfa` (2026-08-29) took pass 83 to 23 —
    `construct_from_boxed` and `context` peeled, ten accessors kept, more
    binaries dying on those same two stub lines. The batch ratchet would
    claw the tree back a sweep later; refusing here saves the rest of the
    iteration."""
    if not reverted:
        return True
    return len(stubbed_fn_names(current)) < len(stubbed_fn_names(snapshot))


def peel_to_compiling(
    *,
    landed: Sequence[LandedBody],
    implicated_now: Callable[[], frozenset[tuple[str, str]]],
    reapply: Callable[[Sequence[LandedBody]], bool],
    recheck: Callable[[], int],
    max_rounds: int | None = None,
) -> PeelResult | PeelRefusal:
    """Drop the implicated bodies until the crate compiles, or refuse.

    `implicated_now` reports the `(fn_name, one-line container)` keys the
    CURRENT diagnostics blame; `reapply` restores the pristine file and
    re-splices exactly the subset it is handed, reporting whether all of them
    landed; `recheck` re-runs the compile and returns its return code. All
    three are injected so the decision sequence is testable without cargo or
    a tree.

    Re-splicing a subset onto the pristine text is deliberate — the alternative,
    writing `unimplemented!()` over the bodies to drop, is refused by the splice
    itself (`body_is_stub` exists to stop a model returning a stub as an
    answer), and it would reach the tree through a path no other fill uses.
    This way the kept bodies land by the same call that landed them the first
    time.

    Bounded structurally: every round that proceeds drops at least one body,
    so at most ``len(landed)`` rounds are possible, and rustc's error WAVES —
    each drop surfacing diagnostics the previous abort had masked — walk to
    completion instead of dying on a fixed cap. `max_rounds` remains as an
    explicit override for tests; passing it back at 3 restores the budget
    that returned mid-progress total losses.
    """
    kept = list(landed)
    reverted: list[str] = []
    bound = len(kept) if max_rounds is None else max_rounds
    for rnd in range(1, bound + 1):
        if not kept:
            return PeelRefusal("no_bodies", rnd - 1, tuple(reverted))
        bad = implicated_now()
        to_drop = [b for b in kept if b.key in bad]
        if not to_drop:
            # The compiler blames nothing this attempt wrote — the error is in
            # the scaffold, an import, or a sibling. Peeling this reply cannot
            # reach it, and dropping bodies on a guess would discard good ones.
            return PeelRefusal("none_implicated", rnd, tuple(reverted))
        if len(to_drop) == len(kept):
            # Every BODY is implicated; there is no proper subset to keep, and
            # "revert them all" is exactly what the caller already does. The
            # comparison is body-to-body: comparing implicated NAMES against
            # kept bodies is how six bodies over three names slipped this
            # guard and a total loss returned as a peel win.
            return PeelRefusal("all_implicated", rnd, tuple(reverted))
        reverted.extend(b.fn_name for b in to_drop)
        kept = [b for b in kept if b.key not in bad]
        if not reapply(kept):
            return PeelRefusal("reapply_failed", rnd, tuple(reverted))
        if recheck() == 0:
            return PeelResult(tuple(kept), tuple(reverted), rnd)
    return PeelRefusal("exhausted_rounds", bound, tuple(reverted))


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
    implicated_keys_from_diags: Callable[..., frozenset[tuple[str, str, str]]],
    run_cargo_check_diag: Callable[..., tuple[int, Any, Sequence[Any]]],
    splice_fn_with_where: Callable[..., tuple[Any, Any, Any]],
    norm_src_rel: Callable[[str], str],
    max_rounds: int | None = None,
) -> tuple[PeelResult | PeelRefusal, int, Any, Sequence[Any]]:
    """Wire :func:`peel_to_compiling` to a tree, returning the peel outcome
    together with the check state it leaves behind (`rc`, cargo output,
    diagnostics) so the caller can carry on with its own gates.

    `implicated_keys_from_diags` is `attribution.implicated_fn_keys` (or a
    test double): `(file, fn_name, one-line container)` per span-resolved fn,
    filtered here to the prompted file and projected to the peel's per-body
    key.

    Lives here rather than inline in the fill route because its collaborators
    are all reassigned inside that route's attempt loop, and closing over loop
    variables is how a late-binding bug gets written; taking them as arguments
    makes the binding explicit and the whole thing testable.
    """
    state: dict[str, Any] = {"rc": 101, "out": None, "diags": list(diags)}
    want = norm_src_rel(file_rel)

    def _implicated() -> frozenset[tuple[str, str]]:
        return frozenset(
            (fn, cont)
            for f, fn, cont in implicated_keys_from_diags(
                state["diags"], target_src_dir)
            if norm_src_rel(f) == want)

    def _reapply(keep: Sequence[LandedBody]) -> bool:
        # Pristine text, then exactly the kept subset through the same splice
        # that landed them the first time — INCLUDING the same overwrite
        # authorization: a refill's pristine body is non-stub, and replaying
        # the splice without `allow_nonstub` refuses `target_not_stub` on the
        # first kept refill (run `349271-d876e4a9`).
        file_path.write_text(snapshot, encoding="utf-8")
        for b in keep:
            ok, _reason, _delta = splice_fn_with_where(
                file_path, b.container, b.fn_name, b.body, b.where_clause,
                allow_nonstub=b.overwrite_authorized,
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
