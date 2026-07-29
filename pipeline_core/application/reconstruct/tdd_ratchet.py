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
