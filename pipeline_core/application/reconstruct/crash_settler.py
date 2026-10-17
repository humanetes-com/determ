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
2. `settle_step`: UNSETTLED ends the loop and the write is judged as before;
   OWN does too, unless the write is a :class:`JudgedWrite` and its culprits
   are a proper subset of it — then those bodies go back to their pre-write
   text and the suite is read again (NARROWED); NO_CRASH ends it with the
   stubs and withdrawals that are in;
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

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
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


def outcome_changes_tree(outcome: SettlementOutcome) -> bool:
    """A committed settlement: culprits went back and the tree now differs
    from the one the write left."""
    return outcome in (SettlementOutcome.SETTLED, SettlementOutcome.NARROWED)


@dataclass(eq=False)
class JudgedWrite(Collection[FnLocator]):
    """A write of several bodies under judgment, which a settlement may narrow.

    A route that can say what each of its bodies replaced passes one of these
    where it would pass the written locators. As a collection it is the
    locators still standing, so every reader of `written` takes it unchanged.
    `prior` is each touched file's text before the write (file path relative
    to the settler's tree). `size` is how many bodies the write put on the
    tree; two impls of one name are two bodies under one locator.

    The settler fills `withdrawn` (the culprit frames it put back to their
    pre-write text) and `evidence` (the crash as the localizer read it) only
    when it commits. A route that does not pass one cannot be narrowed: OWN
    convicts its whole write, as before. Run `757853-e28f1a7d` (2026-10-16):
    two four-body file attempts were restored whole on one culprit body."""

    written: frozenset[FnLocator]
    prior: Mapping[str, str]
    size: int
    withdrawn: list[CrashFrame] = field(default_factory=list)
    evidence: str = ""

    @property
    def withdrawn_locators(self) -> frozenset[FnLocator]:
        return frozenset(frame_locator(f) for f in self.withdrawn)

    @property
    def withdrawn_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.fn_name for f in self.withdrawn))

    def __iter__(self) -> Iterator[FnLocator]:
        return iter(self.written)

    def __len__(self) -> int:
        return len(self.written)

    def __contains__(self, item: object) -> bool:
        return item in self.written


@dataclass(frozen=True, slots=True)
class Settlement:
    outcome: SettlementOutcome
    stubbed: tuple[CrashFrame, ...] = ()
    sites: tuple[CrashSite, ...] = ()
    rounds: int = 0
    reason: str = ""
    #: The write's own bodies put back to their pre-write text (NARROWED, or
    #: SETTLED with stubs beside them).
    withdrawn: tuple[CrashFrame, ...] = ()
    #: The written bodies a terminal OWN step named: the culprit the write
    #: is convicted on, whether or not it could be withdrawn.
    own: tuple[CrashFrame, ...] = ()

    @property
    def settled(self) -> bool:
        return self.outcome is SettlementOutcome.SETTLED

    @property
    def changed_tree(self) -> bool:
        """The tree is not the one the write left: re-measure before judging."""
        return outcome_changes_tree(self.outcome)

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
        write = written if isinstance(written, JudgedWrite) else None
        written_locs = frozenset(written)
        stubbed: list[CrashFrame] = []
        # The write's own bodies put back so far, by (file, fn, occurrence):
        # an occurrence index survives the length changes a stub or a
        # withdrawal makes above it, a line number does not.
        withdrawn_at: dict[tuple[str, str, int], CrashFrame] = {}
        own: tuple[CrashFrame, ...] = ()
        first_sites: tuple[CrashSite, ...] = ()
        own_sites: list[CrashSite] = []
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
                step = settle_step(sites, written_locs, d.is_proven)
                reason = step.reason
                if step.outcome is SettlementOutcome.OWN:
                    own = step.culprits
                    narrowed = self._narrow(txn, write, withdrawn_at,
                                            step.culprits)
                    if isinstance(narrowed, str):
                        outcome = (SettlementOutcome.UNSETTLED if narrowed
                                   else SettlementOutcome.OWN)
                        reason = narrowed or reason
                        d.log(f"  {tag}[phase-c/crash-settle] the crash under "
                              "the write was produced by its own body "
                              + ", ".join(f"{f.file_rel}::{f.fn_name} "
                                          f"(line {f.line})"
                                          for f in step.culprits)
                              + (f" — {narrowed}" if narrowed else
                                 " — the write is judged as written."))
                        break
                    withdrawn_at.update(narrowed)
                    own_sites.extend(
                        s for s in sites
                        if any(frame_locator(f) in written_locs
                               for f in s.culprit_frames))
                    d.log(f"  {tag}[phase-c/crash-settle] round {rounds}: the "
                          "crash under the write was produced by its own body "
                          + ", ".join(f"{f.file_rel}::{f.fn_name} (line {f.line})"
                                      for f in narrowed.values())
                          + " — put back to its pre-write text, re-running "
                          "the suite with the rest of the write.")
                    got = d.run_suite()
                    if got is None:
                        outcome = SettlementOutcome.UNSETTLED
                        reason = "the suite did not run after the withdrawal"
                        break
                    reading = got
                    continue
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
            elif outcome is SettlementOutcome.NO_CRASH and withdrawn_at:
                # Only the write's own bodies went back: nothing outside it
                # changed, so no baseline test can be lost by the settlement;
                # the rest of the write is judged by the caller's gates.
                outcome = SettlementOutcome.NARROWED
            elif outcome is SettlementOutcome.NO_CRASH:
                # Nothing localizable in the write's own capture.
                outcome = SettlementOutcome.UNSETTLED
                reason = reason or "no crash the debugger could localize"
        except Exception as exc:  # noqa: BLE001 — a settlement never ends a fill; the write is judged as before
            outcome = SettlementOutcome.UNSETTLED
            reason = f"{type(exc).__name__}: {exc}"
        withdrawn = list(withdrawn_at.values())
        if outcome in (SettlementOutcome.SETTLED, SettlementOutcome.NARROWED):
            # The refutation is written from the bodies the stack ran, which
            # the transaction's pre-images still hold: a stubbed culprit's
            # kept body, and a withdrawn culprit's written one.
            locs = tuple(dict.fromkeys(
                frame_locator(f) for f in (*stubbed, *withdrawn)))
            committed = False
            try:
                txn.rollback()
                d.record_settled(locs, (*first_sites, *(
                    s for s in own_sites if s not in first_sites)))
                if not self._stub(txn, stubbed):
                    raise RuntimeError("a culprit moved while it was recorded")
                if write is not None and withdrawn_at and not self._withdraw(
                        txn, write, list(withdrawn_at)):
                    raise RuntimeError("a written culprit moved while it was "
                                       "recorded")
                txn.commit()
                committed = True
            except Exception as exc:  # noqa: BLE001 — see above
                txn.rollback()
                outcome = SettlementOutcome.UNSETTLED
                reason = f"refutation not recorded ({type(exc).__name__})"
            if committed and write is not None and withdrawn:
                write.withdrawn.extend(withdrawn)
                write.evidence = "\n\n".join(
                    s.render_evidence() for s in (own_sites or first_sites)[:2])
            if committed:
                d.log(f"  {tag}[phase-c/crash-settle] {str(outcome).upper()} "
                      f"after {rounds} reading(s): the write stands"
                      + (" without " + ", ".join(
                          f"{f.file_rel}::{f.fn_name}" for f in withdrawn)
                         + " (back to its pre-write text)" if withdrawn else "")
                      + ("; " + ", ".join(
                          f"{loc.file}::{loc.fn_name}" for loc in
                          dict.fromkeys(frame_locator(f) for f in stubbed))
                         + " went back to stubs" if stubbed else "")
                      + " (their crash origin is recorded as a refutation, "
                      "never re-seeded).")
        else:
            txn.rollback()
            if stubbed or withdrawn:
                d.log(f"  {tag}[phase-c/crash-settle] not settled "
                      f"({outcome}: {reason}) — {len(stubbed)} stub(s) and "
                      f"{len(withdrawn)} withdrawn body(ies) given back; the "
                      "write is judged as before.")
        if not outcome_changes_tree(outcome):
            withdrawn = []
        result = Settlement(outcome=outcome, stubbed=tuple(stubbed),
                            sites=first_sites, rounds=rounds, reason=reason,
                            withdrawn=tuple(withdrawn), own=own)
        d.emit("phase_c.crash.settle", {
            "outcome": str(outcome), "rounds": rounds, "reason": reason,
            "written": sorted(f"{u.file}::{u.fn_name}" for u in written_locs),
            "stubbed": [f"{f.file_rel}::{f.fn_name}:{f.line}"
                        for f in stubbed],
            "withdrawn": [f"{f.file_rel}::{f.fn_name}:{f.line}"
                          for f in withdrawn],
            "own": [f"{f.file_rel}::{f.fn_name}:{f.line}" for f in own],
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

    def _narrow(
        self, txn: TreeTxn, write: JudgedWrite | None,
        withdrawn_at: Mapping[tuple[str, str, int], CrashFrame],
        culprits: Sequence[CrashFrame],
    ) -> dict[tuple[str, str, int], CrashFrame] | str:
        """Put the write's own culprits back to their pre-write text.

        Returns the occurrences withdrawn, or a reason the step stays
        terminal: ``""`` convicts the whole write as before (OWN) — no
        :class:`JudgedWrite`, a culprit that cannot be located or did not
        change, or culprits that are the whole write — and any other text is
        UNSETTLED (the crash names a body already withdrawn, so the pre-write
        text crashes too and nothing here explains it)."""
        if write is None:
            return ""
        fresh: dict[tuple[str, str, int], CrashFrame] = {}
        for f in culprits:
            try:
                text = txn.read(f.file_rel)
            except OSError:
                return ""
            locs = locate_all_fns(text, f.fn_name)
            i = occurrence_of_frame(
                [(st, e) for _sig, st, _b, e in locs], text, f.line)
            if i is None:
                return ""
            key = (f.file_rel, f.fn_name, i)
            if key in withdrawn_at:
                return ("its body is already back to its pre-write text and "
                        "the crash still names it")
            fresh.setdefault(key, f)
        if not fresh or len(withdrawn_at) + len(fresh) >= write.size:
            return ""
        if not self._withdraw(txn, write, list(fresh)):
            return ""
        return fresh

    def _withdraw(self, txn: TreeTxn, write: JudgedWrite,
                  keys: Sequence[tuple[str, str, int]]) -> bool:
        """Replace each ``(file, fn, occurrence)`` body with the same
        occurrence's body in the write's pre-write text. All or nothing:
        False (nothing written) when a file has no pre-write text, the name's
        occurrences differ in number, or an occurrence is unchanged by the
        write (then it is not a body the write put there)."""
        by_file: dict[str, list[tuple[str, int]]] = {}
        for rel, name, i in keys:
            by_file.setdefault(rel, []).append((name, i))
        out: dict[str, str] = {}
        for rel, items in by_file.items():
            prior = write.prior.get(rel)
            if prior is None:
                return False
            try:
                text = txn.read(rel)
            except OSError:
                return False
            edits: list[tuple[int, int, str]] = []
            for name, i in items:
                cur = locate_all_fns(text, name)
                old = locate_all_fns(prior, name)
                if len(cur) != len(old) or i >= len(cur):
                    return False
                _s, _st, b, e = cur[i]
                _ps, _pst, pb, pe = old[i]
                if text[b:e] == prior[pb:pe]:
                    return False
                edits.append((b, e, prior[pb:pe]))
            for b, e, body in sorted(set(edits), reverse=True):
                text = text[:b] + body + text[e:]
            out[rel] = text
        for rel, text in out.items():
            txn.write(rel, text)
        return True

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


def withdraw_crashing_bodies(
    write: JudgedWrite,
    measure: Callable[[list[str]], TestStateReading | None],
    baseline: TestStateReading | None,
    *,
    tag: str = "",
) -> Settlement | None:
    """Before a write of several bodies is judged, take its own crash
    culprits back off it.

    Reads the whole suite once (``measure`` fills the raw capture it is
    handed, as `measure_test_state` does) and, when a test process the
    baseline did not see die dies under the write, settles that crash with
    the write passed as a :class:`JudgedWrite`: culprits outside it go back
    to stubs exactly as in every gate, and culprits that are a proper subset
    of it go back to their pre-write text. The caller then judges what is
    still standing (``write.withdrawn`` names the rest) with its usual
    gates, and those gates' own settlement finds nothing new to settle.

    It runs before the gates rather than inside them because the gates run
    in sequence on one tree — a scoped test gate first, the whole-suite
    regression gate after it — and a body withdrawn by the second would
    leave the first's verdict describing a tree that no longer exists. The
    cost is one more whole-suite reading per compiling write of two or more
    bodies. None when no settler is bound, there is no baseline, the write
    is a single body (its culprit is the write), the suite did not run, or
    nothing new crashed."""
    settler = bound()
    if settler is None or baseline is None or write.size < 2:
        return None
    raw: list[str] = []
    after = measure(raw)
    if after is None or not raw:
        return None
    if not crash_keys(after.binaries) - crash_keys(baseline.binaries):
        return None
    return settler.settle(raw[-1], write, baseline, tag=tag)


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
