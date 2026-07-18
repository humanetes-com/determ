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
    pass_count: int, missing_count: int, best_pass: int, snap_missing: int,
) -> str:
    """Which ratchet signal (if any) says the tree net-regressed vs the
    best-known snapshot: '' = no breach, else a printable description of the
    breached condition. A crash-killed test binary drops a NONDETERMINISTIC
    subset of its tests from the output (observed live: 22 vs 23 vanished on
    back-to-back sweeps of an unchanged tree), so a single reading of either
    signal is noisy while any binary crashes — the caller must confirm a
    breach on a fresh sweep before acting on it destructively."""
    if pass_count < best_pass:
        return f"pass_count {pass_count}<{best_pass}"
    if missing_count > snap_missing:
        return f"oracle missing {missing_count}>{snap_missing}"
    return ""


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
