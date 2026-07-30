"""TDD-iter batch ratchet.

The per-fill regression guard (`regression_guard.py`) reverts any single fill
that reddens a passing test — but only when the crate is TEST-RUNNABLE at accept
time. While the crate is red mid-batch (the B3-keep convergence regime) tests
can't run, so a body kept there can still lower the cluster pass count once the
batch greens. This backstop operates at the TDD-iter boundary: snapshot the
cluster's src tree at each new pass-count high-water, and when a later iter's
fills NET-regress the count, restore the best-known tree. Result: pass_count is
monotone non-decreasing across iters and the loop always attacks from — and
exits at — its high-water mark, killing the 11↔14 oscillation ceiling.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.layout_retrigger import RefillGrace


def ratchet_breach(
    pass_count: int,
    best_pass: int,
    passing_now: frozenset[str],
    passing_at_snapshot: frozenset[str],
) -> str:
    """Which ratchet signal (if any) says the tree net-regressed vs the
    best-known snapshot: '' = no breach, else a printable description.

    Scored on the quantity the run actually optimises — the pass COUNT. A
    crashed test binary needs no rule of its own: its tests stop being counted
    as passing, so the crash costs exactly what that binary was passing and
    `pass_count` already carries the whole cost. A binary that was passing
    NOTHING can therefore start aborting without convicting the fill that
    exposed it, which matters because that is the state a mid-reconstruction
    tree lives in — every remaining stub still panics, and filling the funnel
    a whole family needs is precisely what makes its siblings' panics
    reachable. An "any binary newly crashed ⇒ revert" rule instead gets
    STRICTER as the tree improves (the crashed binary acquires its first green
    test) and ratchets the run shut at the exact moment it starts working.

    The one thing a count alone misses is a LATERAL swap: some tests go green
    while as many green ones die, so the count holds over a tree that really
    did churn. Comparing the passing NAME SETS catches that, and only a STRICT
    gain forgives a loss — otherwise the loop thrashes forever at a constant
    score. Names come from COMPLETED binaries only, so a crashed binary's
    nondeterministic partial output (observed live: 21..24 tests varying
    across sweeps of an unchanged tree) can manufacture neither signal.

    Either signal can still be perturbed by crash timing — the caller must
    confirm a breach on a fresh sweep before acting on it destructively."""
    if pass_count < best_pass:
        return f"pass_count {pass_count}<{best_pass}"
    lost = passing_at_snapshot - passing_now
    if pass_count == best_pass and lost:
        return (f"lateral swap at pass_count {pass_count} — {len(lost)} "
                f"test(s) green at the snapshot no longer pass: "
                f"{sorted(lost)[:10]}")
    return ""


def breach_withheld(breach: str, grace: RefillGrace) -> str:
    """Why this breach must not be ACTED on (''= charge it normally).

    `ratchet_breach` answers "did the tree net-regress"; this answers "is the
    previous iter's FILLS what regressed it", which is the ratchet's whole
    premise. A successful layout retrigger breaks that premise on purpose: it
    re-stubs every fn referencing the re-authored component so they re-fill
    under the new shape, and a stub panic in drop/vtable code aborts a whole
    test binary. The next sweep therefore scores BELOW the snapshot by
    construction — before a single re-fill has run, and whatever the new
    layout is worth.

    Charging it is not a harmless false positive. The revert is `restore_src_tree`
    over the whole `src/` tree, so it takes the re-authored DEFINITION down with
    the surgical revert: the crash the retrigger exists to fix comes back, the
    capped attempt is spent (the refund path covers only mechanical failures),
    and the next attempt repeats the identical cycle until the cap. Observed
    live: `ChainState` attempts 1 and 2 destroyed this way, one iter after each
    fired, while pass_count sat at its high-water mark.

    The window is `RefillGrace` and it is sized to the bodies the re-author
    authorised, because a FIXED one iter reproduced the very cycle above one iter
    later — `StdError` attempts 1 and 2, a 47-body re-stub that could land only 3
    re-fills per graced iter. The snapshot stays untouched throughout (a count
    below the high-water mark cannot re-snapshot), so a grace that expires short
    reverts to exactly the tree the one-iter rule would have reverted to: the
    experiment gets evidence proportional to the work it started, and the
    high-water mark stays absolute either way.

    Only a window with BODIES IN FLIGHT excuses anything. A fired-but-failed
    attempt re-stubbed nothing, so a breach on its sweep is the tree's own and
    withholding it would forgive a real regression."""
    if breach and grace.bodies_in_flight:
        return ("this sweep measures the layout retrigger's deliberate re-stub of "
                f"{grace.restubbed} body(ies), not last iter's fills — re-fills "
                f"get {grace.iters_left} more iter(s) while they keep landing")
    return ""


def snapshot_due(
    pass_count: int, best_pass: int, tree_sha: str, snap_sha: str | None,
) -> bool:
    """Should the current tree replace the best-known snapshot?

    Yes when there is no snapshot yet, when the count strictly improved, AND
    — crucially — when the count merely HELD but the tree moved. The ratchet's
    mandate is to undo NET REGRESSIONS; an iter that lands work at an equal
    pass count regressed nothing, so that work is the new baseline. Keeping the
    older snapshot instead makes a later breach revert destroy it.

    The case that made this non-optional: filling a stub (`unimplemented!()` →
    a compiling body) removes a guaranteed panic but often moves no test, so it
    reads as a plateau. Against a pre-fill snapshot the next breach re-stubs the
    file, the stub re-enters the frontier, and the loop re-fills the same fn
    every iter forever at zero payoff. An UNCHANGED tree still never re-snapshots
    (same sha) — that was the only case the old strict `>` got right.
    """
    return snap_sha is None or pass_count > best_pass or (
        pass_count == best_pass and tree_sha != snap_sha)


def snapshot_src_tree(src_root: Path) -> dict[str, str] | None:
    """Every `.rs` under `src_root` → {abs-path: text}. None when `src_root` is
    absent (ratchet self-disables for that workspace). Scoped to `src/` on
    purpose: fills only touch impl files there, never `tests/` (the oracle) or
    `target/` (build cache)."""
    if not src_root.is_dir():
        return None
    snap: dict[str, str] = {}
    for p in sorted(src_root.rglob("*.rs")):
        try:
            snap[str(p)] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return snap


def restore_src_tree(snapshot: dict[str, str]) -> int:
    """Rewrite each snapshotted file that has since changed back to its snapshot
    content; return the count reverted. Files created after the snapshot are left
    alone (a fill never adds a `.rs`, it fills stubs in existing files)."""
    reverted = 0
    for path_str, text in snapshot.items():
        p = Path(path_str)
        try:
            if p.read_text(encoding="utf-8", errors="replace") != text:
                p.write_text(text, encoding="utf-8")
                reverted += 1
        except OSError:
            continue
    return reverted
