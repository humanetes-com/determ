"""The red-slice debugging session — the harness loop.

One bounded conversation over one slice (`domain.slice_session.SessionSlice`):
the model returns change sets over the slice's units, the harness splices
them, compiles, runs the whole suite and writes back what the failing
assertions printed and what the probe observed, on the SAME transcript.
Acceptance is one transaction: every target green and no previously-passing
test red keeps the state. Every other judged turn is compared with the
session's BEST judged state — its targets first, then losses beyond the
best's, then the net verdict count — and a turn that is worse is put back
to the best state's bytes before the next turn opens, with the transcript
told what it lost. A session that ends on a best state that still
regresses the opening suite peels the changed units back one at a time
until the suite holds, or restores the baseline.

What this replaces, measured on run `3003-93e9a9bf` (2026-09-02, cluster
`stable`): a two-fn pair attempted six times as six independent 88K-char
documents — four compile failures on the same diagnostic, two regressions
whose printed values reached no prompt — and 14 per-file dispatches of the
same tests' coverage slice judged one file at a time. The stateless route's
retry loop carried feedback for one call; this loop carries it for the
session.

Every collaborator is injected (`SessionPorts`): the model call, the
splicer, cargo, the suite measurement, the probe, the pull resolver. The
loop reads no live test or unit name; the slice is whatever the caller
planned from the sweep's own owner map.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import (
    admit_format_siblings_for_file,
)
from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelResult,
    peel_to_compiling,
)
from pipeline_core.application.reconstruct.tree_txn import PreImage, capture, restore
from pipeline_core.domain.attribution import (
    container_key,
    record_site,
    site_payload,
)
from pipeline_core.domain.context_pull import PullKind, extract_context_pulls
from pipeline_core.domain.llm_output import extract_fn_bodies
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.reasoning_cap import CallCapReading
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refill_evidence import quote_panic_site
from pipeline_core.domain.rust_idents import signature_fixed_by_trait
from pipeline_core.domain.slice_session import (
    ChangeSet,
    SessionSlice,
    TargetStatus,
    TurnReading,
    coverage_note_for,
    extract_notes,
    judge_against_best,
    render_budget_turn,
    render_capped_turn,
    render_compile_turn,
    render_rejected_only_turn,
    render_restored_note,
    render_test_turn,
    route_change_set,
)
from pipeline_core.domain.test_report import (
    failure_evidence_by_test,
    regressed_test_outputs,
)
from pipeline_core.domain.test_results import (
    TestStateReading,
    compare_readings,
)


@dataclass(frozen=True, slots=True)
class SessionPorts:
    """Everything the loop needs from outside, as callables."""

    ask_turns: Callable[[list[dict[str, Any]]], str]
    # (file_path, container, fn_name, body, where_clause, *, allow_nonstub,
    # sig_text) -> SpliceOutcome — the cluster-scoped splice the routes share.
    splice: Callable[..., SpliceOutcome]
    # () -> (rc, output, diagnostics) — cargo check on the tree.
    check: Callable[[], tuple[int, str, Sequence[Any]]]
    # (output, file_rel) -> the focused diagnostics for a file.
    focus_errors: Callable[[str, str], str]
    # (excerpt) -> excerpt with the blamed types' declared fns appended.
    blamed_type_api: Callable[[str], str]
    # (raw_sink) -> the whole suite's reading, None when inconclusive.
    measure_suite: Callable[[list[str]], TestStateReading | None]
    # crate-relative path -> text, for quoting a panic's source line.
    read_source: Callable[[str], str | None]
    # () -> current body text per slice unit.
    enumerate_bodies: Callable[[], Mapping[FnSite, str]]
    # (reply) -> the follow-up text answering its requests, or None.
    answer_pulls: Callable[[str], str | None]
    log: Callable[[str], None]
    emit: Callable[..., None]
    check_graceful_stop: Callable[[str], None]
    # (test names) -> rendered `observed:` lines per test; None disables.
    probe: Callable[[Sequence[str]], Mapping[str, str]] | None = None
    # (diagnostics) -> (file, fn_name, one-line container) per body the
    # compiler's spans resolve to — the peel's implication; None disables
    # the per-body peel (a failed change set is then restored whole).
    implicated: Callable[[Sequence[Any]], frozenset[tuple[str, str, str]]] | None = None
    # (failing test names, hung=label::test) -> test -> the units it executes
    # on the tree as it stands, measured; None disables the per-turn
    # coverage statement. ``hung`` is the judge's own reading of binaries
    # that did not return; the coverage adapter skips them.
    coverage: Callable[..., Mapping[str, frozenset[FnSite]]] | None = None
    now: Callable[[], float] = time.monotonic
    # () -> whether the fill schedule's clock lets this session open ANOTHER
    # turn; None means unbounded. Polled before each turn beside the
    # graceful-stop check; a turn in flight is never interrupted.
    may_claim: Callable[[], bool] | None = None
    # (net verdict delta of the turn just judged) -> None; what the clock's
    # "budget follows payoff" rule reads.
    note_delta: Callable[[int], None] | None = None
    # () -> what the transport recorded about the most recent `ask_turns`
    # call (reasoning-capped? follow-up fn blocks?); None when the transport
    # records nothing. Read after a reply that carried no change set and no
    # request: a capped reply is not a decision and is re-asked, not ended.
    call_reading: Callable[[], CallCapReading | None] | None = None
    # Extra slice units the session admitted after turn 0 (a FILE request
    # that held a called-qualifier complement). The caller's
    # enumerate_bodies must then return those sites too, or a kept body
    # on an admitted unit is invisible at close. None: admission still
    # grows the write set; close cannot see the new body.
    extend_body_watch: Callable[[Sequence[FnSite]], None] | None = None


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    outcome: str                      # accepted | kept | reverted | no_change | stopped
    turns: int
    model_calls: int
    kept: frozenset[FnSite]
    reverted: frozenset[FnSite]
    dispatched: frozenset[FnSite]
    notes: str
    final: TurnReading | None
    compile_failures: int
    transcript_chars: int
    newly_red: tuple[str, ...] = ()
    newly_red_output: Mapping[str, str] = field(default_factory=dict)
    #: The judged turn whose state the session ended on; None when no turn
    #: was judged. `final` is that turn's reading.
    best_turn: int | None = None
    #: Judged turns that were worse than the best state and were put back.
    restored_turns: int = 0
    #: Replies cut by the reasoning cap. One that carried no change set and
    #: no request is re-asked, never judged, never a `no_change`; one that
    #: carried a body is judged and counted in `judged_capped_turns`.
    capped_turns: int = 0
    #: Judged turns, and how many of them came from a capped reply (a
    #: forced completion's guess).
    judged_turns: int = 0
    judged_capped_turns: int = 0

    @property
    def unmeasured(self) -> bool:
        """The session measured nothing it can be graded on: every judged
        landing came from a reasoning-capped reply, or no turn was judged
        and every reply was capped. A truncated call is not a measured
        refusal (the batch routes' rule for an escalated rewrite), so such
        a session is not a FLAT re-buy for the next fill's ordering."""
        if self.outcome == "accepted":
            return False
        uncapped_judged = self.judged_turns - self.judged_capped_turns
        if uncapped_judged > 0 or not self.capped_turns:
            return False
        return (self.judged_turns > 0
                or self.capped_turns >= self.model_calls)


@dataclass(frozen=True, slots=True)
class _JudgedState:
    """A judged turn's reading and the bytes of the slice's files at that
    reading — what a worse later turn is put back to."""

    turn: int
    reading: TurnReading
    images: Mapping[str, PreImage]


def _leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _admit_from_file_requests(
    reply: str,
    records: dict[FnSite, FnRecord],
    tree: Sequence[FnRecord],
    files: list[str],
    baseline_texts: dict[str, PreImage],
    baseline_bodies: dict[FnSite, str],
    target_src_dir: Path,
    ports: SessionPorts,
) -> tuple[FnRecord, ...]:
    """Grow the write set from FILE requests in `reply`. Returns the
    newly admitted records, in request order then tree order."""
    if not tree:
        return ()
    found: list[FnRecord] = []
    seen: set[FnSite] = set()
    for pull in extract_context_pulls(reply):
        if pull.kind is not PullKind.FILE:
            continue
        for rec in admit_format_siblings_for_file(pull.arg, records, tree):
            site = record_site(rec)
            if site in records or site in seen:
                continue
            seen.add(site)
            records[site] = rec
            baseline_bodies.setdefault(site, rec.body_text)
            if rec.file not in baseline_texts:
                baseline_texts[rec.file] = capture(
                    target_src_dir / rec.file, None)
                files.append(rec.file)
            found.append(rec)
    if found:
        if ports.extend_body_watch is not None:
            ports.extend_body_watch(tuple(record_site(r) for r in found))
        ports.log(
            f"  [phase-c/session] FILE request admitted "
            f"{len(found)} unit(s) to the write set: "
            + ", ".join(f"{r.file}::{r.fn_name}" for r in found))
    return tuple(found)


def render_admitted_units(recs: Sequence[FnRecord]) -> str:
    """The sentence the request follow-up carries when a FILE request
    grew the write set: the units, with the block= header the next
    change set must name."""
    named = ", ".join(
        f"{r.fn_name} ({r.container or 'free fn'}) in {r.file}" for r in recs)
    return (f"\nThese units are now in the session's editable set; you may "
            f"return <FN> bodies for them: {named}.")


def run_slice_session(
    ports: SessionPorts,
    *,
    target_src_dir: Path,
    cluster_name: str,
    slice_: SessionSlice,
    records: Mapping[FnSite, FnRecord],
    turn0: str,
    max_turns: int = 8,
    max_minutes: float = 90.0,
    peel_max: int = 6,
    keep_best: bool = True,
    all_records: Sequence[FnRecord] = (),
) -> SessionOutcome:
    """Run one session and leave the tree in its accepted, kept, or
    restored state. Never raises on a model or splice failure; a graceful
    stop propagates after the tree is put back to the best judged state
    the end-of-session decision would have kept, or to the baseline.

    `keep_best=False` is the rule before the best-state reference (the
    opening suite as the only reference, a worse judged turn left on the
    tree for the next turn) — the negative control of the recorded
    replays, never a live setting."""
    label = slice_.label()
    files = list(slice_.files)
    # The write set starts as the planned slice and may grow when a FILE
    # request holds a called-qualifier complement the plan did not admit.
    records = dict(records)
    # Byte pre-images of the slice's files, restored through the
    # transaction primitive: the session's undo is a byte restore, never a
    # re-write of a text it read.
    baseline_texts = {f: capture(target_src_dir / f, None) for f in files}
    baseline_bodies = dict(ports.enumerate_bodies())
    base_raw: list[str] = []
    baseline = ports.measure_suite(base_raw)
    ports.emit("phase_c.session.start", data={
        "cluster": cluster_name, "tests": list(slice_.tests),
        "units": [site_payload(u) for u in slice_.units],
        "files": list(files), "max_turns": max_turns,
        "baseline_pass": baseline.pass_count if baseline is not None else None})
    if baseline is None:
        # Without a baseline the session cannot tell a keep from a
        # regression, so every compiling change would be an unconditional
        # keep. Do not spend the budget.
        ports.log(f"  [phase-c/session] {label}: opening suite inconclusive "
                  "— session not run.")
        return SessionOutcome(
            outcome="no_change", turns=0, model_calls=0, kept=frozenset(),
            reverted=frozenset(), dispatched=frozenset(), notes="",
            final=None, compile_failures=0, transcript_chars=0)
    ports.log(f"  [phase-c/session] {label}: opening — targets "
              f"{', '.join(slice_.tests)}; editable {len(slice_.units)} unit(s) "
              f"in {len(files)} file(s); budget {max_turns} turn(s).")

    messages: list[dict[str, Any]] = [_user(turn0)]
    started = ports.now()
    turns = 0
    model_calls = 0
    compile_failures = 0
    notes = ""
    dispatched: set[FnSite] = set()
    accepted = False
    prev_observed: dict[str, str] = {}
    prev_judged_turn = 0
    # The best judged state: the tree the next turn builds on and the state
    # the end-of-session decision reads. A judged turn that is at least as
    # good (its targets, then losses beyond the best's, then the net)
    # becomes it; a worse one is put back to its bytes before the next turn.
    best: _JudgedState | None = None
    restored_turns = 0
    capped_turns = 0
    judged_turns = 0
    judged_capped = 0

    def _restore(pre_images: Mapping[str, PreImage]) -> None:
        for pre in pre_images.values():
            restore(pre)

    def _images() -> dict[str, PreImage]:
        return {f: capture(target_src_dir / f, None) for f in files}

    def _reply_capped() -> CallCapReading | None:
        if ports.call_reading is None:
            return None
        try:
            return ports.call_reading()
        except Exception:  # noqa: BLE001 — a reading never ends a session
            return None

    def _apply(cs: ChangeSet) -> tuple[list[str], dict[str, PreImage]]:
        """Splice the routed bodies; returns (rejected reasons, pre-splice
        byte pre-images of the files touched)."""
        pre = {r.unit.file: capture(target_src_dir / r.unit.file, None)
               for r in cs.routed}
        rejected = list(cs.rejected)
        for r in cs.routed:
            wc = r.where_clause
            if wc and signature_fixed_by_trait(r.container):
                wc = None
            ok, reason, _delta = ports.splice(
                target_src_dir / r.unit.file, r.container, r.unit.fn_name,
                r.body, wc, allow_nonstub=True, sig_text=r.sig_text)
            if ok:
                dispatched.add(r.unit)
            else:
                rejected.append(f"`{r.unit.fn_name}` in {r.unit.file}: splice refused ({reason})")
        return rejected, pre

    def _read(raw: list[str]) -> tuple[TurnReading, TestStateReading | None]:
        after = ports.measure_suite(raw)
        if after is None or baseline is None:
            return TurnReading(targets=tuple(
                TargetStatus(t, False, "(suite inconclusive)") for t in slice_.tests)), after
        text = raw[-1] if raw else ""
        cmp = compare_readings(baseline, after)
        evidence = failure_evidence_by_test(text)
        failed = {_leaf(n) for n in after.failed_names}
        passed = {_leaf(n) for n in after.passed_names}
        outputs = regressed_test_outputs(text, cmp.newly_red) if cmp.newly_red else {}
        red_names = sorted(cmp.newly_red)
        want_obs = [t for t in slice_.tests if _leaf(t) in failed] + red_names
        observed: Mapping[str, str] = {}
        if ports.probe is not None and want_obs:
            try:
                observed = ports.probe(want_obs)
            except Exception as exc:  # noqa: BLE001 — a probe never ends a session
                ports.log(f"  [phase-c/session] {label}: probe failed ({type(exc).__name__}: {exc})")
                observed = {}
        targets: list[TargetStatus] = []
        for t in slice_.tests:
            leaf = _leaf(t)
            if leaf in passed and leaf not in failed:
                targets.append(TargetStatus(t, True))
                continue
            said = ""
            for printed, ev in evidence.items():
                if _leaf(printed) == leaf:
                    said = quote_panic_site(ev, ports.read_source)
                    break
            targets.append(TargetStatus(
                t, False, said or "(no captured section — the binary may have aborted)",
                observed.get(t) or observed.get(leaf, "")))
        hung = tuple(cmp.hung_tests)
        if after is not None:
            hung = tuple(dict.fromkeys(
                (*hung, *(f"{b.label}::{b.dangling or '?'}"
                          for b in after.hung_binaries))))
        reading = TurnReading(
            targets=tuple(targets), newly_red=tuple(red_names),
            newly_red_said=outputs,
            newly_red_observed={n: observed[n] for n in red_names if n in observed},
            crashed=tuple(cmp.newly_crashed), hung=hung,
            pass_delta=cmp.net_pass_delta,
            executed_shrank=cmp.cur_executed < cmp.base_executed)
        return reading, after

    try:
        while turns < max_turns:
            ports.check_graceful_stop(f"slice session turn {turns + 1}")
            if (ports.now() - started) / 60.0 > max_minutes:
                ports.log(f"  [phase-c/session] {label}: wall-clock budget "
                          f"({max_minutes:.0f} min) spent after {turns} turn(s).")
                break
            if ports.may_claim is not None and not ports.may_claim():
                ports.log(f"  [phase-c/session] {label}: fill share spent "
                          f"after {turns} turn(s) and the last turn moved no "
                          "verdict — no further turn opened.")
                break
            turns += 1
            try:
                reply = ports.ask_turns(messages)
            except Exception as exc:  # noqa: BLE001 — the loop reports, never dies
                ports.log(f"  [phase-c/session] {label}: model error on turn {turns}: {exc}")
                break
            model_calls += 1
            messages.append(_assistant(reply))
            turn_notes = extract_notes(reply)
            if turn_notes:
                notes = turn_notes
            parsed = extract_fn_bodies(reply)
            cap = _reply_capped()
            if cap is not None and cap.capped:
                capped_turns += 1
            if not parsed:
                followup = ports.answer_pulls(reply)
                if followup:
                    admitted = _admit_from_file_requests(
                        reply, records, all_records, files, baseline_texts,
                        baseline_bodies, target_src_dir, ports)
                    if admitted:
                        followup += render_admitted_units(admitted)
                    ports.emit("phase_c.session.turn", data={
                        "cluster": cluster_name, "turn": turns, "kind": "request",
                        "admitted": [site_payload(record_site(r))
                                     for r in admitted]})
                    messages.append(_user(followup + "\n" + render_budget_turn(max_turns - turns)))
                    continue
                if cap is not None and cap.empty_after_cap:
                    # The reply was cut by the reasoning cap before it
                    # decided anything and its forced completion salvaged
                    # no body: an answer dropped, not a decision to change
                    # nothing. The turn is spent (it made a call); the
                    # session is not ended on it while turns remain.
                    ports.emit("phase_c.session.turn", data={
                        "cluster": cluster_name, "turn": turns, "kind": "capped",
                        "followup_fn_blocks": cap.followup_fn_blocks,
                        "reasked": turns < max_turns})
                    if turns < max_turns:
                        ports.log(f"  [phase-c/session] {label}: turn {turns} — the "
                                  "reply was reasoning-capped and its follow-up "
                                  "carried no change set; not a decision — re-asked "
                                  "from the deliberation so far.")
                        messages.append(_user(render_capped_turn() + "\n"
                                              + render_budget_turn(max_turns - turns)))
                        continue
                    ports.log(f"  [phase-c/session] {label}: turn {turns} — the "
                              "reply was reasoning-capped with no change set and "
                              "no turn is left — session ends unmeasured.")
                    break
                ports.emit("phase_c.session.turn", data={
                    "cluster": cluster_name, "turn": turns, "kind": "no_change",
                    "notes": bool(turn_notes)})
                ports.log(f"  [phase-c/session] {label}: turn {turns} carried no "
                          "change set and no request — session ends.")
                break
            cs = route_change_set(parsed, records)
            if not cs.routed:
                ports.emit("phase_c.session.turn", data={
                    "cluster": cluster_name, "turn": turns, "kind": "rejected",
                    "rejected": list(cs.rejected)})
                messages.append(_user(render_rejected_only_turn(cs.rejected)
                                      + "\n" + render_budget_turn(max_turns - turns)))
                continue
            rejected, pre = _apply(cs)
            rc, out, diags = ports.check()
            dropped: list[str] = []
            dropped_diag = ""
            if rc != 0:
                focused = "\n".join(
                    s for s in (ports.focus_errors(out, f) for f in sorted(pre)) if s).strip()
                peeled = _peel_compile(ports, target_src_dir, cs, pre, list(diags))
                if peeled is None:
                    compile_failures += 1
                    _restore(pre)
                    diag = ports.blamed_type_api(focused or out[-4000:])
                    ports.emit("phase_c.session.turn", data={
                        "cluster": cluster_name, "turn": turns, "kind": "compile",
                        "units": [site_payload(u) for u in sorted(cs.units)],
                        "rejected": rejected})
                    ports.log(f"  [phase-c/session] {label}: turn {turns} — change set of "
                              f"{len(cs.routed)} body(ies) did not compile; restored, "
                              "diagnostics returned on the transcript.")
                    messages.append(_user(render_compile_turn(diag, rejected)
                                          + "\n" + render_budget_turn(max_turns - turns)))
                    continue
                dropped = list(peeled.reverted)
                dropped_diag = ports.blamed_type_api(focused or out[-4000:])
                cs = ChangeSet(
                    tuple(r for r in cs.routed if r.unit.fn_name not in peeled.reverted),
                    cs.rejected)
                ports.log(f"  [phase-c/session] {label}: turn {turns} — PEELED "
                          f"{len(dropped)} body(ies) the compiler blamed "
                          f"({', '.join(dropped)}); the remaining "
                          f"{len(cs.routed)} compile and are judged.")
            raw: list[str] = []
            reading, after = _read(raw)
            failing = [t.test for t in reading.targets if not t.passing]
            if after is None or baseline is None:
                # Inconclusive suite: keep nothing from this turn.
                ports.emit("phase_c.session.turn", data={
                    "cluster": cluster_name, "turn": turns, "kind": "inconclusive",
                    "units": [site_payload(u) for u in sorted(cs.units)],
                    "rejected": rejected})
                _restore(pre)
                messages.append(_user("RESULT: the test suite could not be measured "
                                      "after this change set (build or timeout); it was "
                                      "not applied.\n" + render_budget_turn(max_turns - turns)))
                continue
            judged_turns += 1
            if cap is not None and cap.capped:
                judged_capped += 1
            # The best-state decision, before anything reads the reading:
            # a turn at least as good as the best becomes it; a worse one
            # is put back to the best's bytes before the next turn opens.
            restored_to: int | None = None
            restored_note = ""
            restored_why = ""
            if best is not None and keep_best and not reading.clean:
                verdict = judge_against_best(reading, best.reading)
                if not verdict.holds:
                    restored_to = best.turn
                    restored_note = render_restored_note(verdict, best.turn)
                    restored_why = verdict.losses()
            if restored_to is None:
                best = _JudgedState(turns, reading, _images())
            ports.emit("phase_c.session.turn", data={
                "cluster": cluster_name, "turn": turns, "kind": "judged",
                "units": [site_payload(u) for u in sorted(cs.units)],
                "targets_failing": failing, "newly_red": list(reading.newly_red),
                "crashed": list(reading.crashed), "pass_delta": reading.pass_delta,
                "rejected": rejected, "capped": bool(cap is not None and cap.capped),
                "best_turn": best.turn if best is not None else None,
                "restored_to": restored_to})
            ports.log(f"  [phase-c/session] {label}: turn {turns} — {len(cs.routed)} "
                      f"body(ies) compiled; targets failing {len(failing)}/"
                      f"{len(slice_.tests)}, newly red {len(reading.newly_red)}, "
                      f"net {reading.pass_delta:+d}.")
            if reading.clean:
                accepted = True
                ports.log(f"  [phase-c/session] {label}: ACCEPTED on turn {turns} — "
                          "every target passes, no regression.")
                break
            if restored_to is not None and best is not None:
                restored_turns += 1
                _restore(best.images)
                ports.log(f"  [phase-c/session] {label}: turn {turns} — worse than "
                          f"turn {best.turn} ({restored_why}); the tree is back "
                          f"at turn {best.turn}'s bodies.")
            if ports.note_delta is not None and best is not None:
                # What the run's last LANDED change set bought: the state on
                # the tree, which after a restore is the best's, not this turn's.
                ports.note_delta(best.reading.pass_delta)
            # What the still-failing targets execute NOW: the one fact that
            # separates "wrong body" from "body the targets never reach".
            note = ""
            observed_now = {t.test: t.observed for t in reading.targets if not t.passing}
            unchanged_since = None
            if prev_observed and observed_now and observed_now == prev_observed:
                unchanged_since = prev_judged_turn
            prev_observed, prev_judged_turn = observed_now, turns
            if ports.coverage is not None and failing and cs.units:
                try:
                    owners = ports.coverage(failing, hung=reading.hung)
                except Exception as exc:  # noqa: BLE001 — a probe never ends a session
                    ports.log(f"  [phase-c/session] {label}: coverage failed "
                              f"({type(exc).__name__}: {exc})")
                    owners = {}
                if owners:
                    note = coverage_note_for(cs.units, owners, slice_.units,
                                             unchanged_since_turn=unchanged_since)
            elif unchanged_since is not None:
                note = (f"The observed values are byte-identical to turn "
                        f"{unchanged_since}'s: the bodies you changed did not alter "
                        "what the failing assertions see.")
            messages.append(_user(render_test_turn(
                reading, rejected, dropped=dropped, dropped_diagnostics=dropped_diag,
                coverage_note=note, restored_note=restored_note)
                + "\n" + render_budget_turn(max_turns - turns)))
    except BaseException:
        # A stop mid-session: the tree must not hold an unjudged change set.
        # It is put back to the best judged state when the end-of-session
        # decision would have kept that state as it stands (not regressed
        # against the opening suite — no peel needed), else to the baseline.
        if not accepted:
            if best is not None and not best.reading.regressed:
                _restore(best.images)
            else:
                _restore(baseline_texts)
        raise

    current = dict(ports.enumerate_bodies())
    watch = frozenset(slice_.units) | frozenset(records)
    changed = frozenset(
        u for u in watch
        if current.get(u, baseline_bodies.get(u)) != baseline_bodies.get(u))
    outcome = "no_change"
    kept: frozenset[FnSite] = frozenset()
    reverted: frozenset[FnSite] = frozenset()
    newly_red: tuple[str, ...] = ()
    newly_red_output: Mapping[str, str] = {}
    # The tree holds the best judged state (every worse turn was put back),
    # so the decision reads that state's reading, and the peel — needed only
    # when the best state itself regresses the opening suite — walks from it.
    final = best.reading if best is not None else None
    if accepted:
        outcome, kept = "accepted", changed
    elif changed:
        if final is not None:
            # Carried whether the set is kept or peeled: a kept net-positive
            # turn's newly-red tests are the next prompt's evidence.
            newly_red = final.newly_red
            newly_red_output = dict(final.newly_red_said)
        if final is not None and final.regressed:
            kept, reverted = _peel(ports, target_src_dir, slice_, records, baseline,
                                   baseline_bodies, baseline_texts, changed,
                                   peel_max=peel_max, label=label)
            outcome = "kept" if kept else "reverted"
        else:
            outcome, kept = "kept", changed
    result = SessionOutcome(
        outcome=outcome, turns=turns, model_calls=model_calls, kept=kept,
        reverted=reverted, dispatched=frozenset(dispatched), notes=notes,
        final=final, compile_failures=compile_failures,
        transcript_chars=sum(len(str(m.get("content") or "")) for m in messages),
        newly_red=newly_red, newly_red_output=newly_red_output,
        best_turn=best.turn if best is not None else None,
        restored_turns=restored_turns, capped_turns=capped_turns,
        judged_turns=judged_turns, judged_capped_turns=judged_capped)
    ports.emit("phase_c.session.end", data={
        "cluster": cluster_name, "outcome": outcome, "turns": turns,
        "model_calls": model_calls, "compile_failures": compile_failures,
        "kept": [site_payload(u) for u in sorted(kept)],
        "reverted": [site_payload(u) for u in sorted(reverted)],
        "notes_chars": len(notes),
        "best_turn": result.best_turn, "restored_turns": restored_turns,
        "capped_turns": capped_turns, "judged_turns": judged_turns,
        "judged_capped_turns": judged_capped, "unmeasured": result.unmeasured})
    ports.log(f"  [phase-c/session] {label}: {outcome.upper()} after {turns} turn(s) "
              f"({model_calls} call(s), {compile_failures} compile failure(s)); "
              f"kept {len(kept)}, reverted {len(reverted)} unit(s)"
              + (f"; ended on turn {result.best_turn}'s state"
                 + (f", {restored_turns} worse turn(s) put back" if restored_turns else "")
                 if result.best_turn is not None else "")
              + (f"; {capped_turns} reasoning-capped turn(s)"
                 + (" — unmeasured" if result.unmeasured else "") if capped_turns else "")
              + (f"; notes {len(notes)} chars." if notes else "."))
    return result


def _peel_compile(
    ports: SessionPorts,
    target_src_dir: Path,
    cs: ChangeSet,
    pre: Mapping[str, PreImage],
    diags: list[Any],
) -> PeelResult | None:
    """Drop the bodies the compiler blames and keep the rest, on the same
    contract as the file-first route's `peel_to_compiling`: re-splice the
    kept subset onto the pre-turn text, re-check, walk rustc's error waves.
    None when the peel is off or declines (nothing or everything
    implicated, a re-splice refused) — the caller restores the whole turn."""
    if ports.implicated is None or not cs.routed:
        return None
    touched = {r.unit.file for r in cs.routed}
    landed = [LandedBody(r.unit.fn_name, r.container, r.sig_text, r.body,
                         r.where_clause, overwrite_authorized=True)
              for r in cs.routed]
    by_key = {b.key: r for b, r in zip(landed, cs.routed, strict=True)}
    state: dict[str, Any] = {"diags": diags}

    def implicated_now() -> frozenset[tuple[str, str]]:
        keys = ports.implicated(state["diags"]) if ports.implicated else frozenset()
        return frozenset((fn, container_key(cont)) for f, fn, cont in keys
                         if f in touched or f"src/{f}" in touched or f.removeprefix("src/") in
                         {t.removeprefix("src/") for t in touched})

    def reapply(kept: Sequence[LandedBody]) -> bool:
        for pre_image in pre.values():
            restore(pre_image)
        ok_all = True
        for b in kept:
            r = by_key[b.key]
            wc = b.where_clause
            if wc and signature_fixed_by_trait(b.container):
                wc = None
            ok, _reason, _d = ports.splice(
                target_src_dir / r.unit.file, b.container, b.fn_name, b.body, wc,
                allow_nonstub=True, sig_text=b.sig_text)
            ok_all = ok_all and bool(ok)
        return ok_all

    def recheck() -> int:
        rc, _out, new_diags = ports.check()
        state["diags"] = list(new_diags)
        return rc

    result = peel_to_compiling(
        landed=landed, implicated_now=implicated_now, reapply=reapply, recheck=recheck)
    return result if isinstance(result, PeelResult) else None


def _peel(
    ports: SessionPorts,
    target_src_dir: Path,
    slice_: SessionSlice,
    records: Mapping[FnSite, FnRecord],
    baseline: TestStateReading | None,
    baseline_bodies: Mapping[FnSite, str],
    baseline_texts: Mapping[str, PreImage],
    changed: frozenset[FnSite],
    *,
    peel_max: int,
    label: str,
) -> tuple[frozenset[FnSite], frozenset[FnSite]]:
    """Restore changed units one at a time until the suite holds; the rest
    are kept. Restores the whole baseline when the budget runs out or
    nothing holds. The bar is the sweep's (`RegressionComparison.regressed`):
    a strict net gain holds even with a newly-red test, at or below zero any
    loss fails. It used to be "no previously-passing test red, whatever the
    net" — stricter than the sweep that judges the same tree one iteration
    later, so a session could revert what the ratchet would have kept."""
    remaining = sorted(changed, key=lambda u: (u.file, u.fn_name, u.container))
    reverted: list[FnSite] = []
    unrestored: list[FnSite] = []
    steps = 0
    while remaining and steps < peel_max:
        u = remaining.pop()
        steps += 1
        rec = records.get(u)
        base_body = baseline_bodies.get(u)
        if rec is None or base_body is None:
            unrestored.append(u)
            continue
        ok, _reason, _d = ports.splice(
            target_src_dir / u.file, rec.container, u.fn_name, base_body, None,
            allow_nonstub=True, sig_text=rec.sig_text)
        if not ok:
            unrestored.append(u)
            continue
        reverted.append(u)
        rc, _out, _diags = ports.check()
        if rc != 0:
            continue
        raw: list[str] = []
        after = ports.measure_suite(raw)
        if after is None or baseline is None:
            continue
        cmp = compare_readings(baseline, after)
        if not cmp.regressed:
            ports.log(f"  [phase-c/session] {label}: peeled {len(reverted)} unit(s) — "
                      f"the remaining {len(remaining) + len(unrestored)} hold "
                      "with no previously-passing test red.")
            return frozenset(remaining) | frozenset(unrestored), frozenset(reverted)
    for pre_image in baseline_texts.values():
        restore(pre_image)
    ports.log(f"  [phase-c/session] {label}: no subset of the change set holds "
              "within the peel budget — baseline restored.")
    return frozenset(), changed
