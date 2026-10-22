"""TDD-iter batch ratchet.

The per-fill regression guard (`regression_guard.py`) reverts any single fill
that reddens a passing test — but only when the crate is TEST-RUNNABLE at accept
time. While the crate is red mid-batch (the keep-while-red convergence regime) tests
can't run, so a body kept there can still lower the cluster pass count once the
batch greens. This backstop operates at the TDD-iter boundary: snapshot the
cluster's src tree at each new pass-count high-water, and when a later iter's
fills NET-regress the count, restore the best-known tree. Result: pass_count is
monotone non-decreasing across iters and the loop always attacks from — and
exits at — its high-water mark, killing the 11↔14 oscillation ceiling.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.tree_txn import PreImage, restore
from pipeline_core.domain.layout_retrigger import RefillGrace
from pipeline_core.domain.rust_scan import stubbed_fn_names
from pipeline_core.domain.test_report import stub_only_crash
from pipeline_core.domain.test_results import (
    TestStateReading,
    compare_readings,
    parse_binary_results,
)


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


def hollow_count(src_texts: dict[str, str] | None) -> int:
    """How many fns in these file texts are still skeleton stubs.

    The closure withhold is for a shrinking hole set (hollow → body). Counted
    from file text so the ratchet and the fill gates share
    :func:`stubbed_fn_names` rather than a second scanner. ``None`` or an
    empty mapping is zero hollow, not "unknown"."""
    if not src_texts:
        return 0
    return sum(len(stubbed_fn_names(text)) for text in src_texts.values())


def closure_stub_sites(
    snapshot_reading: TestStateReading | None, output: str,
) -> frozenset[tuple[str, int]]:
    """The stub-panic sites :func:`closure_withheld` may forgive a breach on:
    `stub_only_crash` over exactly the binaries that were COMPLETE WITH A
    PASS at the ratchet's snapshot and CRASHED in this sweep
    (`compare_readings(...).newly_crashed`), plus the newly red tests whose
    own isolated process aborted (`newly_red_aborted`) — the processes whose
    lost tests are the breach. The fill gates judge their crash-only keep over
    the same set, so a keep is never a charge one iteration later.

    Not the whole capture: a `crashed_all_red` binary (no pass at the
    snapshot, so nothing of the breach is its) that dies on an assertion
    would otherwise veto the evidence of the binary that vanished the
    passes, and a completed binary's failures are verdicts, not crash
    evidence. Without a snapshot reading there is nothing to compare
    against, and nothing is forgiven."""
    if snapshot_reading is None:
        return frozenset()
    now = TestStateReading(parse_binary_results(output))
    cmp = compare_readings(snapshot_reading, now)
    return stub_only_crash(output, crashed=cmp.newly_crashed,
                           aborted_tests=cmp.newly_red_aborted)


def aborted_names(output: str) -> frozenset[str]:
    """The tests of ``output`` whose own process aborted (isolated re-run).
    They carry a red verdict, and for the closure exemption they vanished
    with their process exactly as a crashed binary's tests do: the caller
    removes them from the judged set it hands :func:`closure_withheld`."""
    out: set[str] = set()
    for b in parse_binary_results(output):
        out |= b.aborted
    return frozenset(out)


def closure_withheld(
    breach: str,
    lost: frozenset[str],
    judged_now: frozenset[str],
    stub_crash_sites: frozenset[tuple[str, int]],
    *,
    hollow_now: int,
    hollow_at_snapshot: int,
) -> str:
    """Why this breach is a coupled component CLOSING, not a regression
    ('' = charge it normally). The second standing withhold beside
    :func:`breach_withheld`'s re-fill window.

    The fill gates keep a compiling body whose only cost is a binary aborted
    by a still-hollow stub's panic (the crash names the stub's own line, so
    the evidence is the capture's, not a heuristic's). While a strongly
    coupled family — anyhow's `Error`/vtable web, where nearly every test
    constructs an `Error` and its `Drop` reaches every vtable slot — is being
    closed, those kept fills can legitimately drop the sweep's pass count:
    tests vanish with the crashed binary before the fills that un-crash it
    have landed. Charging that breach restores the pre-closure snapshot and
    erases exactly the fills the closure needs, so the component can only
    ever close inside one lottery sweep that fills every drop-reachable stub
    at once — the `pass=13` orbit of runs `800669-a17dabc4` and
    `1048052-2feafd52` (2026-08-09), where 25 per-fn fills landed in one
    iteration and the tree's hollow set net-moved by 2.

    Withheld ONLY while the evidence holds, and it expires by itself: every
    lost test must have VANISHED with a crash (a lost test that received a
    verdict in a completed binary really regressed — charge), the capture's
    panics must be stub panics exclusively (a segfault or an assertion panic
    is the fill's own fault — charge), AND the hollow set must have strictly
    shrunk versus the snapshot. A sweep that costs passing tests with the
    same hollow set is not a closure: run `1160135-7b10cdfa` (2026-08-29)
    peeled `construct_from_boxed` and `context` back to `unimplemented!()`,
    kept ten accessor rewrites, and 60 passes vanished with binaries dying
    on those same two stub lines — hollow 15 → 15, pass 83 → 23. Charging
    that restores the snapshot. Once the last drop-reachable stub fills, the
    binaries complete, the conditions turn false, and the ratchet judges the
    closed component against the snapshot with real verdicts."""
    if not (breach and lost and stub_crash_sites):
        return ""
    if lost & judged_now:
        return ""
    if hollow_now >= hollow_at_snapshot:
        return ""
    _locs = ", ".join(f"{f}:{ln}" for f, ln in sorted(stub_crash_sites))
    return (f"all {len(lost)} lost test(s) vanished with a binary whose only "
            f"panic(s) are stub panics at {_locs} — kept crash-blocked "
            "fill(s) closing a coupled component; the charge waits until the "
            "stub(s) fill and the tree is judgeable again")


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


class TreeSnapshot(dict[str, str]):
    """`{abs-path: text}` of every `.rs` under a `src/` root, over the bytes
    each was read from. The text view is what `hollow_count` and the
    ratchet's stub diffs read; the byte view (`pre_images`) is what
    `restore_src_tree` writes back through the transaction module's
    `restore` — a text round-trip folds `\r\n` to `\n` and was never a
    byte restore. A dict subclass so every reader typed on the text mapping
    keeps working unchanged."""

    __slots__ = ("pre_images",)

    def __init__(self, pre_images: dict[str, PreImage]) -> None:
        super().__init__({k: v.text("replace") for k, v in pre_images.items()})
        self.pre_images = pre_images


def snapshot_src_tree(src_root: Path) -> TreeSnapshot | None:
    """Every `.rs` under `src_root` → {abs-path: text}, each over its byte
    pre-image. None when `src_root` is absent (ratchet self-disables for
    that workspace). Scoped to `src/` on purpose: fills only touch impl
    files there, never `tests/` (the oracle) or `target/` (build cache).

    The pre-images are seq 0. `capture` would mint a journal seq, and
    `restore` would then splice every crash stub journalled after the
    snapshot back onto the scored tree. A scored-tree restore wants the
    bytes it read. An attempt's `capture` still mints a seq, and
    `TreeTxn.rollback` still re-applies edits made after that attempt
    started."""
    if not src_root.is_dir():
        return None
    pres: dict[str, PreImage] = {}
    for p in sorted(src_root.rglob("*.rs")):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        pres[str(p)] = PreImage(p, data, None)
    return TreeSnapshot(pres)


def restore_src_tree(snapshot: dict[str, str]) -> int:
    """Put each snapshotted file that has since changed back, byte for byte,
    through the transaction module's `restore`; return the count reverted.
    Files created after the snapshot are left alone (a fill never adds a
    `.rs`, it fills stubs in existing files). A plain text mapping (no byte
    view) is restored from its texts — the pre-byte-snapshot shape, kept
    for callers that build one by hand."""
    pres = (snapshot.pre_images if isinstance(snapshot, TreeSnapshot)
            else {k: PreImage(Path(k), v.encode("utf-8"), None)
                  for k, v in snapshot.items()})
    reverted = 0
    for pre in pres.values():
        try:
            if pre.path.read_bytes() != pre.data:
                restore(pre)
                reverted += 1
        except OSError:
            continue
    return reverted
