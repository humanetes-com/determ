"""Settle a crash under a write whose measured culprit is an unproven body
outside it (application layer; the decision is `domain/crash_settlement`).

Every route that judges a write by running tests can see a test process die
under it. Until 2026-10-16 each of them convicted the write: the per-fn U2
gate reverted it and fed the retro, and the joint routes' regression gate
reverted the block. Run `1057870-b427a3a2` is the cost — a correct accessor
convicted four attempts running while the constructor that handed it a
dangling pointer stood on the tree, kept on tests that had never run. This
module is the one place a crash may be settled against such a body instead,
and every gate calls it the same way (`CrashSettler.settle`).

The loop, per reading of the suite:

1. localize the crashes the write could have caused (`localize_crashes` with
   the crashes the baseline already showed excluded);
2. `settle_step`: OWN or UNSETTLED ends the loop and the write is judged as
   before; NO_CRASH ends it with the stubs that are in;
3. STUB: record the culprits' current bodies as refuted, put back the one
   occurrence of each the stack ran (`occurrence_of_frame`), and re-run the
   whole suite — a culprit off this binary's stack (another caller of the
   same reader) shows up as the next reading's crash.

At the end the reading is compared to the write's baseline with the gates'
own comparison: a settlement that loses a test that passed before the write
is given back byte for byte (`TreeTxn.rollback`), as is every outcome but
SETTLED. A settlement that holds is committed: the write stands, its tests now
stop in the stubs, and those stubs are the next held units dispatch offers.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.crash_localizer import crash_keys
from pipeline_core.application.reconstruct.tree_txn import TreeTxn
from pipeline_core.domain.crash_settlement import (
    SettlementOutcome,
    frame_locator,
    occurrence_of_frame,
    settle_step,
)
from pipeline_core.domain.crash_site import CrashFrame, CrashSite
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import locate_all_fns
from pipeline_core.domain.test_results import (
    TestStateReading,
    compare_readings,
    parse_binary_results,
)

#: The body a settled culprit gets back — the form the strip writes, which
#: `rust_scan.is_hollow_body` and the stub inventory read as a stub.
STUB_BODY = "\n    ::core::unimplemented!()\n"

#: Readings one settlement may take. Each round puts back at least one more
#: body, so the bound is reached only by a write whose crashes keep naming
#: new unproven culprits; it is then judged as before.
MAX_ROUNDS = 6


@dataclass(frozen=True, slots=True)
class Settlement:
    outcome: SettlementOutcome
    stubbed: tuple[CrashFrame, ...] = ()
    sites: tuple[CrashSite, ...] = ()
    rounds: int = 0
    reason: str = ""

    @property
    def settled(self) -> bool:
        return self.outcome is SettlementOutcome.SETTLED

    @property
    def stubbed_locators(self) -> tuple[FnLocator, ...]:
        return tuple(dict.fromkeys(frame_locator(f) for f in self.stubbed))

    def render(self) -> str:
        """The crashes as the localizer read them, for a retry prompt — the
        fault-class renderer's text (`CrashSite.render`), never a class
        sentence of the caller's own."""
        if not self.sites:
            return ""
        return "\n\n".join(s.render_evidence() for s in self.sites[:2])


@dataclass(frozen=True, slots=True)
class CrashSettlerDeps:
    #: `(capture, exclude) -> sites`: the composed `localize_crashes` bound to
    #: the cluster's test command, tree and env.
    localize: Callable[[str, Collection[str]], Sequence[CrashSite]]
    #: Re-run the cluster's whole suite on the tree as it stands; None when
    #: it did not run to a reading (the tree no longer builds, a timeout).
    run_suite: Callable[[], str | None]
    is_proven: Callable[[FnLocator], bool]
    src_dir: Path
    #: Write the culprits' refutation rows, called with their bodies still on
    #: the tree, only for a settlement that holds.
    record_settled: Callable[[Sequence[FnLocator], Sequence[CrashSite]], None]
    log: Callable[[str], None]
    emit: Callable[[str, Mapping[str, Any]], None] = field(
        default=lambda _e, _d: None)


class CrashSettler:
    """Bound once per fill by the driver; every gate calls :meth:`settle`."""

    def __init__(self, deps: CrashSettlerDeps) -> None:
        self.deps = deps

    def settle(
        self,
        capture: str,
        written: Collection[FnLocator],
        baseline: TestStateReading | None,
        *,
        tag: str = "",
    ) -> Settlement:
        d = self.deps
        before = (crash_keys(baseline.binaries) if baseline is not None
                  else frozenset())
        txn = TreeTxn(d.src_dir)
        stubbed: list[CrashFrame] = []
        first_sites: tuple[CrashSite, ...] = ()
        reading = capture
        outcome = SettlementOutcome.UNSETTLED
        reason = ""
        rounds = 0
        try:
            while rounds < MAX_ROUNDS:
                rounds += 1
                sites = tuple(d.localize(reading, before))
                if not first_sites:
                    first_sites = sites
                step = settle_step(sites, written, d.is_proven)
                reason = step.reason
                if step.outcome is not SettlementOutcome.STUB:
                    outcome = step.outcome
                    break
                fresh = [f for f in step.culprits
                         if (f.file_rel, f.fn_name, f.line) not in {
                             (s.file_rel, s.fn_name, s.line) for s in stubbed}]
                if not fresh or not self._stub(txn, fresh):
                    outcome = SettlementOutcome.UNSETTLED
                    reason = reason or "the culprit's body could not be put back"
                    break
                stubbed.extend(fresh)
                d.log(f"  {tag}[phase-c/crash-settle] round {rounds}: the "
                      "crash under the write was produced by unproven "
                      + ", ".join(f"{f.file_rel}::{f.fn_name} (line {f.line})"
                                  for f in fresh)
                      + " — put back to a stub, re-running the suite.")
                got = d.run_suite()
                if got is None:
                    outcome = SettlementOutcome.UNSETTLED
                    reason = "the suite did not run after the stubs went in"
                    break
                reading = got
            else:
                outcome = SettlementOutcome.UNSETTLED
                reason = f"still crashing after {MAX_ROUNDS} reading(s)"
            if outcome is SettlementOutcome.NO_CRASH and stubbed:
                outcome, reason = self._holds(reading, baseline)
            elif outcome is SettlementOutcome.NO_CRASH:
                # Nothing localizable in the write's own capture.
                outcome = SettlementOutcome.UNSETTLED
                reason = reason or "no crash the debugger could localize"
        except Exception as exc:  # noqa: BLE001 — a settlement never ends a fill; the write is judged as before
            outcome = SettlementOutcome.UNSETTLED
            reason = f"{type(exc).__name__}: {exc}"
        if outcome is SettlementOutcome.SETTLED:
            # The refutation is written from the bodies the stack ran, which
            # the transaction's pre-images still hold.
            locs = tuple(dict.fromkeys(frame_locator(f) for f in stubbed))
            committed = False
            try:
                txn.rollback()
                d.record_settled(locs, first_sites)
                if not self._stub(txn, stubbed):
                    raise RuntimeError("a culprit moved while it was recorded")
                txn.commit()
                committed = True
            except Exception as exc:  # noqa: BLE001 — see above
                txn.rollback()
                outcome = SettlementOutcome.UNSETTLED
                reason = f"refutation not recorded ({type(exc).__name__})"
            if committed:
                d.log(f"  {tag}[phase-c/crash-settle] SETTLED after {rounds} "
                      "reading(s): the write stands; "
                      + ", ".join(f"{loc.file}::{loc.fn_name}"
                                  for loc in locs)
                      + " went back to stubs (their crash origin is recorded "
                      "as a refutation, never re-seeded).")
        else:
            txn.rollback()
            if stubbed:
                d.log(f"  {tag}[phase-c/crash-settle] not settled "
                      f"({outcome}: {reason}) — {len(stubbed)} stub(s) given "
                      "back; the write is judged as before.")
        result = Settlement(outcome=outcome, stubbed=tuple(stubbed),
                            sites=first_sites, rounds=rounds, reason=reason)
        d.emit("phase_c.crash.settle", {
            "outcome": str(outcome), "rounds": rounds, "reason": reason,
            "written": sorted(f"{u.file}::{u.fn_name}" for u in written),
            "stubbed": [f"{f.file_rel}::{f.fn_name}:{f.line}"
                        for f in stubbed],
            "blame": [s.blame_label for s in first_sites],
        })
        return result

    def _holds(self, reading: str,
               baseline: TestStateReading | None,
               ) -> tuple[SettlementOutcome, str]:
        if baseline is None:
            return SettlementOutcome.UNSETTLED, "no baseline to compare with"
        after = TestStateReading(parse_binary_results(reading))
        cmp = compare_readings(baseline, after)
        lost = cmp.cur_pass < cmp.base_pass or bool(cmp.newly_red)
        if lost or cmp.newly_crashed or cmp.cur_executed < cmp.base_executed:
            return (SettlementOutcome.UNSETTLED,
                    "the stubs cost the baseline " + (cmp.summary or "a test"))
        return SettlementOutcome.SETTLED, ""

    def _stub(self, txn: TreeTxn, frames: Sequence[CrashFrame]) -> bool:
        """Put back the one occurrence of each frame's function the stack
        ran. False when any of them cannot be located (nothing is half-done:
        the caller rolls the transaction back)."""
        by_file: dict[str, list[CrashFrame]] = {}
        for f in frames:
            by_file.setdefault(f.file_rel, []).append(f)
        for rel, fs in by_file.items():
            try:
                text = txn.read(rel)
            except OSError:
                return False
            edits: list[tuple[int, int]] = []
            for f in fs:
                locs = locate_all_fns(text, f.fn_name)
                spans = [(s, e) for _sig, s, _b, e in locs]
                i = occurrence_of_frame(spans, text, f.line)
                if i is None:
                    return False
                _sig, _s, b, e = locs[i]
                edits.append((b, e))
            for b, e in sorted(set(edits), reverse=True):
                text = text[:b] + STUB_BODY + text[e:]
            txn.write(rel, text)
        return True


# The settler of the fill in progress. The driver binds it around the fill
# (`phase_c_tdd`) and every route's test gate reads it (`cargo_runner.
# u2_test_gate`), the way the sweep's observed cycles reach every write set
# (`write_set.bind_observed`): a route that judges a crash cannot be wired
# without it, because it is not a parameter any route passes. None outside a
# fill — the gate then judges the crash as before.
_bound: CrashSettler | None = None


def bind(settler: CrashSettler | None) -> None:
    global _bound
    _bound = settler


def bound() -> CrashSettler | None:
    return _bound
