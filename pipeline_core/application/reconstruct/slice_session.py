"""The red-slice debugging session — the harness loop.

One bounded conversation over one slice (`domain.slice_session.SessionSlice`):
the model returns change sets over the slice's units, the harness splices
them, compiles, runs the whole suite and writes back what the failing
assertions printed and what the probe observed, on the SAME transcript.
Acceptance is one transaction: every target green and no previously-passing
test red keeps the state; a session that ends otherwise peels the changed
units back one at a time until the suite holds, or restores the baseline.

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

from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelResult,
    peel_to_compiling,
)
from pipeline_core.application.reconstruct.tree_txn import PreImage, capture, restore
from pipeline_core.domain.attribution import container_key, site_payload
from pipeline_core.domain.llm_output import extract_fn_bodies
from pipeline_core.domain.models import FnRecord, FnSite
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
    render_budget_turn,
    render_compile_turn,
    render_rejected_only_turn,
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
    # (failing test names) -> test -> the units it executes on the tree as it
    # stands, measured; None disables the per-turn coverage statement.
    coverage: Callable[[Sequence[str]], Mapping[str, frozenset[FnSite]]] | None = None
    now: Callable[[], float] = time.monotonic
    # () -> whether the fill schedule's clock lets this session open ANOTHER
    # turn; None means unbounded. Polled before each turn beside the
    # graceful-stop check; a turn in flight is never interrupted.
    may_claim: Callable[[], bool] | None = None
    # (net verdict delta of the turn just judged) -> None; what the clock's
    # "budget follows payoff" rule reads.
    note_delta: Callable[[int], None] | None = None


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


def _leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


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
) -> SessionOutcome:
    """Run one session and leave the tree in its accepted, kept, or
    restored state. Never raises on a model or splice failure; a graceful
    stop propagates after the tree is restored to the last state the
    session judged clean."""
    label = slice_.label()
    files = slice_.files
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
    last_reading: TurnReading | None = None
    prev_observed: dict[str, str] = {}
    prev_judged_turn = 0
    # The tree state the session last judged (compiling); a non-clean judged
    # state stays on the tree for the next turn to build on and is what the
    # end-of-session decision peels or restores.
    last_judged_clean = True

    def _restore(pre_images: Mapping[str, PreImage]) -> None:
        for pre in pre_images.values():
            restore(pre)

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
        reading = TurnReading(
            targets=tuple(targets), newly_red=tuple(red_names),
            newly_red_said=outputs,
            newly_red_observed={n: observed[n] for n in red_names if n in observed},
            crashed=tuple(cmp.newly_crashed), pass_delta=cmp.net_pass_delta,
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
            if not parsed:
                followup = ports.answer_pulls(reply)
                if followup:
                    ports.emit("phase_c.session.turn", data={
                        "cluster": cluster_name, "turn": turns, "kind": "request"})
                    messages.append(_user(followup + "\n" + render_budget_turn(max_turns - turns)))
                    continue
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
            last_reading = reading
            failing = [t.test for t in reading.targets if not t.passing]
            ports.emit("phase_c.session.turn", data={
                "cluster": cluster_name, "turn": turns, "kind": "judged",
                "units": [site_payload(u) for u in sorted(cs.units)],
                "targets_failing": failing, "newly_red": list(reading.newly_red),
                "crashed": list(reading.crashed), "pass_delta": reading.pass_delta,
                "rejected": rejected})
            ports.log(f"  [phase-c/session] {label}: turn {turns} — {len(cs.routed)} "
                      f"body(ies) compiled; targets failing {len(failing)}/"
                      f"{len(slice_.tests)}, newly red {len(reading.newly_red)}, "
                      f"net {reading.pass_delta:+d}.")
            if ports.note_delta is not None and after is not None:
                ports.note_delta(reading.pass_delta)
            if after is None or baseline is None:
                # Inconclusive suite: keep nothing from this turn.
                _restore(pre)
                messages.append(_user("RESULT: the test suite could not be measured "
                                      "after this change set (build or timeout); it was "
                                      "not applied.\n" + render_budget_turn(max_turns - turns)))
                continue
            last_judged_clean = reading.clean
            if reading.clean:
                accepted = True
                ports.log(f"  [phase-c/session] {label}: ACCEPTED on turn {turns} — "
                          "every target passes, no regression.")
                break
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
                    owners = ports.coverage(failing)
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
                coverage_note=note) + "\n" + render_budget_turn(max_turns - turns)))
    except BaseException:
        # A stop mid-session: the tree must not hold an unjudged change set.
        if not accepted and not last_judged_clean:
            _restore(baseline_texts)
        raise

    current = dict(ports.enumerate_bodies())
    changed = frozenset(u for u in slice_.units
                        if current.get(u, baseline_bodies.get(u)) != baseline_bodies.get(u))
    outcome = "no_change"
    kept: frozenset[FnSite] = frozenset()
    reverted: frozenset[FnSite] = frozenset()
    newly_red: tuple[str, ...] = ()
    newly_red_output: Mapping[str, str] = {}
    if accepted:
        outcome, kept = "accepted", changed
    elif changed:
        if last_reading is not None:
            # Carried whether the set is kept or peeled: a kept net-positive
            # turn's newly-red tests are the next prompt's evidence.
            newly_red = last_reading.newly_red
            newly_red_output = dict(last_reading.newly_red_said)
        if last_reading is not None and last_reading.regressed:
            kept, reverted = _peel(ports, target_src_dir, slice_, records, baseline,
                                   baseline_bodies, baseline_texts, changed,
                                   peel_max=peel_max, label=label)
            outcome = "kept" if kept else "reverted"
        else:
            outcome, kept = "kept", changed
    ports.emit("phase_c.session.end", data={
        "cluster": cluster_name, "outcome": outcome, "turns": turns,
        "model_calls": model_calls, "compile_failures": compile_failures,
        "kept": [site_payload(u) for u in sorted(kept)],
        "reverted": [site_payload(u) for u in sorted(reverted)],
        "notes_chars": len(notes)})
    ports.log(f"  [phase-c/session] {label}: {outcome.upper()} after {turns} turn(s) "
              f"({model_calls} call(s), {compile_failures} compile failure(s)); "
              f"kept {len(kept)}, reverted {len(reverted)} unit(s)"
              + (f"; notes {len(notes)} chars." if notes else "."))
    return SessionOutcome(
        outcome=outcome, turns=turns, model_calls=model_calls, kept=kept,
        reverted=reverted, dispatched=frozenset(dispatched), notes=notes,
        final=last_reading, compile_failures=compile_failures,
        transcript_chars=sum(len(str(m.get("content") or "")) for m in messages),
        newly_red=newly_red, newly_red_output=newly_red_output)


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
