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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import (
    admit_format_siblings_for_file,
)
from pipeline_core.application.reconstruct.crash_localizer import crash_keys
from pipeline_core.application.reconstruct.crash_settler import (
    CrashSettler,
    JudgedWrite,
    Settlement,
    crash_text_of,
)
from pipeline_core.application.reconstruct.crash_settler import (
    bound as bound_crash_settler,
)
from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelResult,
    peel_to_compiling,
    split_withdrawn,
)
from pipeline_core.application.reconstruct.pull_resolver import RequestAnswerer
from pipeline_core.application.reconstruct.tree_txn import PreImage, capture, restore
from pipeline_core.application.reconstruct.unit_conversation import (
    ConversationPorts,
    TranscriptBound,
    UnitConversation,
)
from pipeline_core.domain.assertion_position import (
    TestKey,
    TestOutcomes,
    outcomes_of,
    positions_for,
)
from pipeline_core.domain.attempt_site import quote_attempt_outputs
from pipeline_core.domain.attribution import (
    container_key,
    record_site,
    site_payload,
)
from pipeline_core.domain.context_pull import PullKind, extract_context_pulls
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.crash_site import CrashSite
from pipeline_core.domain.fill_ground import MeasureRole
from pipeline_core.domain.ignored_inputs import (
    declined_by_ignored_inputs,
    identity_name,
    ignored_inputs_feedback,
    vouched_for_keep,
)
from pipeline_core.domain.llm_output import extract_fn_bodies
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.reasoning_cap import CallCapReading
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refill_evidence import quote_panic_site
from pipeline_core.domain.rust_idents import signature_fixed_by_trait
from pipeline_core.domain.slice_session import (
    ChangeSet,
    RoutedBody,
    SessionSlice,
    TargetStatus,
    TurnReading,
    coverage_note_for,
    executing_tests,
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
    # (raw_sink, role) -> the whole suite's reading, None when inconclusive.
    # The opening baseline is the session's PREMISE; every reading after a
    # splice (a turn, a settle re-read, a peel step, the seed) is a JUDGMENT
    # (`domain/fill_ground.MeasureRole`).
    measure_suite: Callable[[list[str], MeasureRole], TestStateReading | None]
    # crate-relative path -> text, for quoting a panic's source line.
    read_source: Callable[[str], str | None]
    # () -> current body text per slice unit.
    enumerate_bodies: Callable[[], Mapping[FnSite, str]]
    # (reply, conversation=everything sent so far) -> the follow-up text
    # answering its requests, or None. The transcript lets a request for a
    # file the session already carries whole be answered by pointer.
    answer_pulls: RequestAnswerer
    log: Callable[[str], None]
    emit: Callable[..., None]
    check_graceful_stop: Callable[[str], None]
    # Whether a compiling turn left the crash it was written against
    # (`crash_keep.SameCrashJudge`). Required: the session cannot keep a
    # turn without saying which units the crash held when the fill
    # started, so a caller cannot reopen the rotation keep by omission.
    same_crash: SameCrashJudge
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
    #: The turn the session's own preference order chose (targets first).
    #: It differs from `best_turn` when that state is one the end-of-session
    #: keep gate refuses and a judged turn the gate accepts was measured:
    #: the session then ends on the accepted one.
    preferred_turn: int | None = None
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
    #: Replies that carried at least one function block (a change set the
    #: session routed, refused, compiled or judged). A request reply is
    #: not one: it spends no attempt turn and it is not a body.
    answered_turns: int = 0
    requests: int = 0
    #: Minutes from the session's opening (before its baseline suite) to the
    #: end of its first turn; None when no turn made a call. The fill
    #: schedule prices the first turn apart from the turns after it.
    first_turn_minutes: float | None = None
    #: `file::fn` -> body, snapshotted while a regressed turn that turned
    #: tests green is still on the tree. Empty when the session did not
    #: revert a gain.
    candidate_bodies: Mapping[str, str] = field(default_factory=dict)
    #: The slice carried a seed and the seed's write was measured (its own
    #: judged turn, kind `seed`). False on every unseeded session and on a
    #: seed that did not compile, could not be routed, or read inconclusive.
    seed_judged: bool = False
    #: The session's targets passing on the tree the session LEFT: the end
    #: state when it was kept whole, the peel's held subset when peeled, the
    #: opening suite when everything was put back. None when that tree was
    #: not measured. `final` is the best judged reading, taken before any
    #: peel, and describes a tree the session may have taken back; a joint
    #: ask classified on it stored `gains_held` for a seed the peel had
    #: already removed (review of the 2026-10-22 implementation).
    held_green: frozenset[str] | None = None

    @property
    def carried_body(self) -> bool:
        """A reply carried a function block, or the session accepted a
        state. False when the session ran and every reply was a request,
        notes or empty. The session route writes this onto the ledger
        row. A row written before the bit is read from its landing."""
        return self.answered_turns > 0 or self.outcome == "accepted"

    @property
    def unanswered(self) -> bool:
        """The model was asked and no reply carried a function block: the
        session ended on requests, on notes, on an empty or a capped reply.
        Nothing was written, so nothing about the slice was tried. A reply
        whose bodies did not compile, or were refused by routing, IS an
        answer and is graded like any other."""
        return (self.outcome != "accepted" and self.model_calls > 0
                and self.answered_turns == 0)

    @property
    def unmeasured(self) -> bool:
        """The session measured nothing it can be graded on: no reply
        carried a body (:attr:`unanswered`), or every judged landing came
        from a reasoning-capped reply, or no turn was judged and every reply
        was capped. A truncated call is not a measured refusal (the batch
        routes' rule for an escalated rewrite), and a session that wrote
        nothing did not try the slice, so neither is a FLAT re-buy for the
        next fill's ordering. Run `1430425-193a8f28` (2026-10-14) graded
        such a session FLAT because no reply was capped, and the next
        fill deferred the one-test slice on that test as `rebuy_flat`.
        Repeating an unanswered session does not make it a try; the
        fill's session cap and the budget bound how often it opens."""
        if self.outcome == "accepted":
            return False
        if self.unanswered:
            return True
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


def _red_target_positions(
    reading: TestStateReading, tests: Sequence[str],
) -> dict[TestKey, int]:
    """The assertion positions of each of `tests` that `reading` saw
    failing."""
    red = {_leaf(n) for n in reading.failed_names}
    return positions_for(reading.positions,
                         [t for t in tests if _leaf(t) in red])


def _target_outcomes(
    baseline: TestStateReading, after: TestStateReading, tests: Sequence[str],
    crash_sites: Sequence[CrashSite] = (),
) -> TestOutcomes:
    """The session's targets between the baseline and `after`.

    Position movement and a destroyed verdict are both on the result.
    `earlier_tests` is what the position rule names, and a caller empties
    it when something was gained. `aborted` is not emptied by a gain: a
    silent death removes the measurement. Read by the turn judge and by
    the peel, so a peeled subset is held to the bar the turn was.
    `crash_sites` locate an aborted target in the crate (the abort
    partition, `abort_verdict`); none, and every abort reads unlocated."""
    return outcomes_of(tests, before=baseline.keyed(), after=after.keyed(),
                       crash_sites=crash_sites)


def _abort_sites(settler: CrashSettler | None, settlement: Settlement | None,
                 outcomes: TestOutcomes, capture: str,
                 baseline: TestStateReading | None,
                 ) -> tuple[CrashSite, ...]:
    """The crash sites that judge `outcomes`' aborts: the settlement's when
    it read this very capture (it did not change the tree), else a
    read-only localization of `capture`, and only when a target aborted."""
    if not outcomes.aborted:
        return ()
    if settlement is not None and not settlement.changed_tree:
        return settlement.sites
    if settler is None or not capture:
        return ()
    return settler.localize(capture, baseline)


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


def seed_change_set(
    seed: Sequence[tuple[str, str]],
    records: Mapping[FnSite, FnRecord],
) -> tuple[ChangeSet, tuple[str, ...]]:
    """The seed's bodies routed to the slice's records: each `file::fn` to the
    one record of that file and name (either rooting of the file). A key
    with no record, or with several, is not routed and is named."""
    def _norm(f: str) -> str:
        return f.removeprefix("src/")
    routed: list[RoutedBody] = []
    missing: list[str] = []
    for key, body in seed:
        if "::" not in key or not body:
            missing.append(key)
            continue
        file_, fn_ = key.rsplit("::", 1)
        hits = [site for site in records
                if site.fn_name == fn_ and _norm(site.file) == _norm(file_)]
        if len(hits) != 1:
            missing.append(key)
            continue
        rec = records[hits[0]]
        routed.append(RoutedBody(unit=hits[0], container=rec.container,
                                 sig_text=rec.sig_text, body=body,
                                 where_clause=None))
    return ChangeSet(tuple(routed), ()), tuple(missing)


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
    max_transcript_chars: int = 0,
    peel_max: int = 6,
    keep_best: bool = True,
    end_on_keepable: bool = True,
    all_records: Sequence[FnRecord] = (),
    executed: Mapping[str, Collection[FnSite]] | None = None,
    request_allowance: int = 0,
) -> SessionOutcome:
    """Run one session and leave the tree in its accepted, kept, or
    restored state. Never raises on a model or splice failure; a graceful
    stop propagates after the tree is put back to the best judged state
    the end-of-session decision would have kept, or to the baseline.

    `keep_best=False` is the rule before the best-state reference (the
    opening suite as the only reference, a worse judged turn left on the
    tree for the next turn) — the negative control of the recorded
    replays, never a live setting.

    `end_on_keepable=False` is the rule before the session ended on the
    best state its own keep gate accepts (the preferred state alone, the
    peel from it the only way back) — the second negative control, also
    never a live setting.

    `max_transcript_chars` is the session's OWN bound on the transcript it
    sends, and it is a fourth budget beside the turn count, the wall clock
    and the fill share. The client that sends the turns does not fit them —
    `ask_turns` warns past 90% of the single-call limit and sends the list
    as it is, because the whole conversation is what the model must see —
    so nothing bounded the growth: one reply that the server cut at its
    token ceiling is appended like any other, and on run
    `366282-7c585f74` (2026-09-15, cluster `nightly`) a 135,396-char reply
    carrying no change set took the transcript from 160,389 chars to
    296,189 in one turn, past the 293,376-char limit, and the next turn was
    sent anyway. Checked BEFORE each call, so a turn is never sent over the
    bound; the session ends on its best judged state exactly as the
    wall-clock budget ends it. It deliberately does not trim or drop a
    reply already in the transcript: the user turn that answers a request
    refers to the turn that made it, and a conversation with a hole in it
    is not a cheaper conversation. 0 disables the bound — the behaviour
    before it, and the negative control.

    `executed` (test -> the units it executes, the map the slice was planned
    from) is the session's attribution: a body that ignores a declared input
    is kept only when a test executing THAT unit turned green on the turn.
    None attributes nothing, so such a body is never vouched.

    `request_allowance` is how many request replies (context pulls answered
    by `ports.answer_pulls`) the session takes without spending a turn of
    `max_turns`. The turn budget bounds attempts: change sets judged,
    refused or failing to compile. Charging retrieval to it let a session
    end before its first attempt: run `1430425-193a8f28` (2026-10-14,
    cluster `stable`, fill 6) opened a one-test session with the failing
    test's unit editable, and its replies 1-7 were request tags, four of
    them for a file already in its prompt; reply 8, told one turn was
    left, was deliberation and notes with no body, and the session ended
    with zero judged turns. Requests are already bounded by the answerer's
    own round-trip budget (it answers `max_pulls` and refuses once, then
    returns None); the allowance is the caller's statement of that bound,
    and a request past it is charged as a turn so the loop terminates
    whatever the answerer does. 0 charges every request, the behaviour
    before, and is the negative control.

    A slice with a `seed` (a joint ask) writes the seed after the opening
    baseline and judges it as a turn of its own, kind `seed`: no model call,
    no turn charged, measured as a JUDGMENT against the opening PREMISE and
    kept, peeled or restored by the same rule as every turn. Turn 0 then
    carries the seed's result (what each newly red test printed), and the
    model's turns start from the seeded tree, or from the baseline when the
    seed did not compile."""
    label = slice_.label()
    files = list(slice_.files)
    # The write set starts as the planned slice and may grow when a FILE
    # request holds a called-qualifier complement the plan did not admit.
    records = dict(records)
    # The first turn is priced from here, the baseline suite included: it is
    # the time the share must hold before a second turn can open.
    opened_at = ports.now()
    # Byte pre-images of the slice's files, restored through the
    # transaction primitive: the session's undo is a byte restore, never a
    # re-write of a text it read.
    baseline_texts = {f: capture(target_src_dir / f, None) for f in files}
    baseline_bodies = dict(ports.enumerate_bodies())
    base_raw: list[str] = []
    baseline = ports.measure_suite(base_raw, MeasureRole.PREMISE)
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

    # The session's transcript is a `UnitConversation`, the engine every
    # fill route talks through. The session keeps its own request handling
    # (a request may admit units, and past `request_allowance` it is a
    # counted turn), so it sends plain turns (`say`); the engine owns the
    # transcript and its bound.
    conv = UnitConversation(
        ConversationPorts(ask_turns=ports.ask_turns, log=ports.log,
                          emit=ports.emit,
                          max_transcript_chars=max_transcript_chars),
        label=label, route="session")
    pending = turn0
    started = ports.now()
    seed_judged = False
    turns = 0
    model_calls = 0
    first_turn_minutes: float | None = None
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
    _best_bodies: dict[str, str] = {}
    # The best judged state the END-OF-SESSION KEEP GATE would leave on the
    # tree. That gate is `TurnReading.regressed` (the sweep's rule: a strict
    # net gain forgives a newly-red test, at or below zero any loss
    # convicts), and it is a DIFFERENT order from the one that picks `best`
    # above (`judge_against_best` ranks the session's own targets first), so
    # a session can converge on a state the gate must refuse while every
    # state the gate would have kept is put back as "worse". Run
    # `504793-5774c226` (2026-09-16, cluster `nightly`, iter 1), the session
    # on `test_whitespace`: 8 turns over 32 min on one unit
    # (`src/ensure.rs::__dispatch_ensure`); turns 1/3/5/7 held the target
    # green at net -12 (13 reddened passengers) and turns 4 and 8 measured
    # net +1 with nothing newly red. Targets-first made 7 the best, 4 and 8
    # were restored away as worse, the gate refused 7, the peel emptied the
    # set and the session ended `kept 0`. The next fill re-bought the same
    # unit in 8.8 min for the same +1.
    keepable: _JudgedState | None = None
    restored_turns = 0
    capped_turns = 0
    judged_turns = 0
    judged_capped = 0
    answered_turns = 0
    requests = 0

    def _restore(pre_images: Mapping[str, PreImage]) -> None:
        for pre in pre_images.values():
            restore(pre)

    def _images() -> dict[str, PreImage]:
        return {f: capture(target_src_dir / f, None) for f in files}

    def _seed_turn() -> str:
        """Write and judge the slice's seed; return the text turn 0 carries
        about it ("" without a seed)."""
        nonlocal best, keepable, accepted, seed_judged
        cs, missing = seed_change_set(slice_.seed, records)
        if missing:
            ports.log(f"  [phase-c/session] {label}: seed — {len(missing)} "
                      "body(ies) have no single record in the slice and are "
                      "not written: " + ", ".join(missing[:6]))
        if not cs.routed:
            return ""
        rejected, pre = _apply(cs)
        names = ", ".join(f"{r.unit.file}::{r.unit.fn_name}" for r in cs.routed)
        preface = ("SEED: before your first turn, the bodies an earlier "
                   f"answer wrote for {names} were put on the tree. That "
                   "answer turned target tests green and was reverted "
                   "because it broke others.\n")
        rc, out, _diags = ports.check()
        if rc != 0:
            _restore(pre)
            ports.emit("phase_c.session.turn", data={
                "cluster": cluster_name, "turn": 0, "kind": "seed",
                "compiled": False,
                "units": [site_payload(u) for u in sorted(cs.units)]})
            ports.log(f"  [phase-c/session] {label}: seed of {len(cs.routed)} "
                      "body(ies) does not compile on this tree; restored, the "
                      "turns start from the opening state.")
            focused = "\n".join(
                x for x in (ports.focus_errors(out, f) for f in sorted(pre)) if x)
            return (preface + "They do not compile on this tree and were taken "
                    "back:\n" + (focused or out[-2000:]))
        raw: list[str] = []
        jw = JudgedWrite(
            frozenset(FnLocator(r.unit.file, r.unit.fn_name) for r in cs.routed),
            {f: img.text("replace") for f, img in pre.items()},
            size=len(cs.routed))
        reading, after = _read(raw, jw)
        if after is None or baseline is None:
            _restore(pre)
            ports.log(f"  [phase-c/session] {label}: seed read inconclusive; "
                      "restored.")
            return ""
        seed_judged = True
        images = _images()
        best = _JudgedState(0, reading, images)
        if not reading.regressed:
            keepable = best
        failing = [t.test for t in reading.targets if not t.passing]
        ports.emit("phase_c.session.turn", data={
            "cluster": cluster_name, "turn": 0, "kind": "seed",
            "compiled": True,
            "units": [site_payload(u) for u in sorted(cs.units)],
            "targets_failing": failing, "newly_red": list(reading.newly_red),
            "crashed": list(reading.crashed), "pass_delta": reading.pass_delta,
            "regressed": reading.regressed})
        ports.log(f"  [phase-c/session] {label}: seed turn — {len(cs.routed)} "
                  f"body(ies) judged; targets failing {len(failing)}/"
                  f"{len(slice_.tests)}, newly red {len(reading.newly_red)}, "
                  f"net {reading.pass_delta:+d}.")
        if reading.clean:
            accepted = True
            ports.log(f"  [phase-c/session] {label}: ACCEPTED on the seed — "
                      "every target passes, no regression.")
        return preface + "The result of that write:\n" + render_test_turn(
            reading, rejected)

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

    def _read(raw: list[str], written: Collection[FnLocator],
              ) -> tuple[TurnReading, TestStateReading | None]:
        after = ports.measure_suite(raw, MeasureRole.JUDGMENT)
        # A test process that died under this change set is settled by the
        # fill's bound settler (`crash_settler`) before the turn is read, the
        # same rule the per-fn and joint gates apply: a culprit outside the
        # change set and unproven goes back to a stub, and the suite is read
        # again. A later restore of this turn's files puts those bodies back
        # with the rest of the pre-image.
        _settler = bound_crash_settler()
        settlement: Settlement | None = None
        if (after is not None and baseline is not None
                and _settler is not None and written
                and crash_keys(after.binaries) - crash_keys(baseline.binaries)):
            settlement = _settler.settle(raw[-1] if raw else "", written,
                                         baseline, tag=f"{label} ")
            if settlement.changed_tree:
                raw.clear()
                after = ports.measure_suite(raw, MeasureRole.JUDGMENT)
        # Whatever the settlement decided, the crash it read is what the
        # model is shown beside the verdicts (`TurnReading.crash`).
        crash_text = crash_text_of(settlement)
        if after is None or baseline is None:
            return TurnReading(targets=tuple(
                TargetStatus(t, False, "(suite inconclusive)") for t in slice_.tests)), after
        text = raw[-1] if raw else ""
        cmp = compare_readings(baseline, after)
        evidence = failure_evidence_by_test(text)
        failed = {_leaf(n) for n in after.failed_names}
        passed = {_leaf(n) for n in after.passed_names}
        # Quoted while this turn's change set is still on the tree: the
        # next turn may be told about it after a restore (`attempt_site`).
        outputs = (quote_attempt_outputs(
            regressed_test_outputs(text, cmp.newly_red), ports.read_source)
            if cmp.newly_red else {})
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
        # A target that moved to an earlier line of the same file is a loss
        # the name sets cannot show (`TurnReading.earlier`). A target whose
        # process died without a panic is a different loss (`TurnReading.aborted`).
        positions_now = _red_target_positions(after, slice_.tests)
        outcomes = _target_outcomes(baseline, after, slice_.tests)
        _sites = _abort_sites(_settler, settlement, outcomes,
                              raw[-1] if raw else "", baseline)
        if _sites:
            outcomes = _target_outcomes(baseline, after, slice_.tests, _sites)
        earlier = () if outcomes.gained else outcomes.earlier_tests
        aborted = tuple(sorted(outcomes.aborted))
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
            newly_green=tuple(sorted(cmp.newly_green)),
            executed_shrank=cmp.cur_executed < cmp.base_executed,
            positions=positions_now, earlier=earlier, aborted=aborted,
            aborted_green=tuple(sorted(outcomes.aborted_green)),
            aborted_located=tuple(sorted(outcomes.aborted_located)),
            crash=crash_text)
        return reading, after

    try:
        if slice_.seed:
            _said = _seed_turn()
            if _said:
                pending = turn0 + "\n\n" + _said
        while turns < max_turns and not accepted:
            if model_calls and first_turn_minutes is None:
                first_turn_minutes = (ports.now() - opened_at) / 60.0
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
            try:
                reply = conv.say(pending, kind="turn")
            except TranscriptBound:
                sent_chars = conv.chars() + len(pending)
                ports.log(
                    f"  [phase-c/session] {label}: transcript "
                    f"{sent_chars:,} chars over the single-call bound "
                    f"({max_transcript_chars:,}) after {turns} turn(s) — no "
                    "further turn opened.")
                ports.emit("phase_c.session.transcript_stop", data={
                    "cluster": cluster_name, "turn": turns + 1,
                    "chars": sent_chars, "bound": max_transcript_chars})
                break
            except Exception as exc:  # noqa: BLE001 — the loop reports, never dies
                ports.log(f"  [phase-c/session] {label}: model error on turn "
                          f"{turns + 1}: {exc}")
                turns += 1
                break
            model_calls += 1
            turn_notes = extract_notes(reply)
            if turn_notes:
                notes = turn_notes
            parsed = extract_fn_bodies(reply)
            cap = _reply_capped()
            if cap is not None and cap.capped:
                capped_turns += 1
            followup = (ports.answer_pulls(reply, conversation=conv.text())
                        if not parsed else None)
            if followup:
                # A request is retrieval, not an attempt: within the
                # allowance it spends no turn, so the budget line keeps
                # counting the change sets the session may still try. Past
                # the allowance it is charged as a turn, which is what
                # bounds a session whose answerer never stops answering.
                requests += 1
                charged = requests > request_allowance
                if charged:
                    turns += 1
                admitted = _admit_from_file_requests(
                    reply, records, all_records, files, baseline_texts,
                    baseline_bodies, target_src_dir, ports)
                if admitted:
                    followup += render_admitted_units(admitted)
                ports.emit("phase_c.session.turn", data={
                    "cluster": cluster_name, "turn": turns, "kind": "request",
                    "request": requests, "charged": charged,
                    "admitted": [site_payload(record_site(r))
                                 for r in admitted]})
                pending = followup + "\n" + render_budget_turn(max_turns - turns)
                continue
            turns += 1
            if parsed:
                answered_turns += 1
            if not parsed:
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
                        pending = (render_capped_turn() + "\n"
                                   + render_budget_turn(max_turns - turns))
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
                pending = (render_rejected_only_turn(cs.rejected)
                           + "\n" + render_budget_turn(max_turns - turns))
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
                    pending = (render_compile_turn(diag, rejected)
                               + "\n" + render_budget_turn(max_turns - turns))
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
            # The change set as a write the crash settlement may narrow: a
            # body of it that a test process crashed on goes back to its
            # pre-turn text and the rest is judged, as the peel above does
            # for a body the compiler blamed.
            _jw = JudgedWrite(
                frozenset(FnLocator(r.unit.file, r.unit.fn_name)
                          for r in cs.routed),
                {f: img.text("replace") for f, img in pre.items()},
                size=len(cs.routed))
            reading, after = _read(raw, _jw)
            crashed: list[str] = []
            if _jw.withdrawn:
                _kept_r, _gone_r = split_withdrawn(
                    cs.routed,
                    locus=lambda r: (r.unit.file, r.unit.fn_name),
                    text=lambda r: r.body,
                    withdrawn=[(f.file_rel, f.fn_name) for f in _jw.withdrawn],
                    file_text=lambda f: (target_src_dir / f).read_text(
                        encoding="utf-8", errors="replace"))
                crashed = [r.unit.fn_name for r in _gone_r]
                cs = ChangeSet(tuple(_kept_r), cs.rejected)
                ports.log(f"  [phase-c/session] {label}: turn {turns} — "
                          f"WITHDREW {len(crashed)} body(ies) a test process "
                          f"crashed on ({', '.join(crashed)}); the remaining "
                          f"{len(cs.routed)} are judged.")
            failing = [t.test for t in reading.targets if not t.passing]
            if after is None or baseline is None:
                # Inconclusive suite: keep nothing from this turn.
                ports.emit("phase_c.session.turn", data={
                    "cluster": cluster_name, "turn": turns, "kind": "inconclusive",
                    "units": [site_payload(u) for u in sorted(cs.units)],
                    "rejected": rejected})
                _restore(pre)
                pending = ("RESULT: the test suite could not be measured "
                           "after this change set (build or timeout); it was "
                           "not applied.\n" + render_budget_turn(max_turns - turns))
                continue
            judged_turns += 1
            if cap is not None and cap.capped:
                judged_capped += 1
            _crash_out = raw[-1] if raw else ""
            _written = [FnLocator(u.file, u.fn_name) for u in cs.units]
            if ports.same_crash.refuses(
                    output=_crash_out, aimed=slice_.tests, written=_written):
                _restore(pre)
                _held = ", ".join(sorted(
                    f"{u.file}::{u.fn_name}"
                    for u in ports.same_crash.written_culprits(_written)))
                ports.log(
                    f"  [phase-c/session] {label}: turn {turns} — compiled, "
                    "but every aimed test is still a native crash and the "
                    f"crash's culprit(s) it wrote ({_held}) still are; not "
                    "kept.")
                ports.emit("phase_c.session.turn", data={
                    "cluster": cluster_name, "turn": turns, "kind": "same_crash",
                    "units": [site_payload(u) for u in sorted(cs.units)]})
                pending = (
                    "RESULT: this change compiled, and every aimed test is "
                    "still the same native crash. The crash's culprit(s) it "
                    f"wrote ({_held}) are still on the crashing stack after "
                    "it, so the change was not kept.\n"
                    + render_budget_turn(max_turns - turns))
                continue
            # A body that ignores a declared input stands only when the turn
            # turned green a test that executes THAT unit
            # (`domain/ignored_inputs`). A sibling's test, or a target that
            # was green before the session, vouches for nobody. Otherwise the
            # change set is not kept and the transcript says which inputs
            # were dropped.
            _ignoring = declined_by_ignored_inputs(
                ((r.unit.fn_name, r.sig_text, r.body, r.unit.container)
                 for r in cs.routed),
                vouched=vouched_for_keep(
                    {(r.unit.fn_name, r.unit.container): executing_tests(
                        executed or {}, r.unit)
                     for r in cs.routed},
                    reading.newly_green))
            if _ignoring:
                _restore(pre)
                ports.log(
                    f"  [phase-c/declined] session {label}: turn {turns} — "
                    + ", ".join(f"{identity_name(n)} ignores {', '.join(i)}"
                                for n, i in sorted(_ignoring.items()))
                    + " and no test turned green; not kept.")
                ports.emit("phase_c.session.turn", data={
                    "cluster": cluster_name, "turn": turns, "kind": "declined",
                    "units": [site_payload(u) for u in sorted(cs.units)],
                    "ignored": {identity_name(n): list(i)
                                for n, i in sorted(_ignoring.items())}})
                pending = (
                    "RESULT: this change compiled and was not kept.\n"
                    + "\n".join(ignored_inputs_feedback(identity_name(n), i)
                                for n, i in sorted(_ignoring.items()))
                    + "\n" + render_budget_turn(max_turns - turns))
                continue
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
            images: Mapping[str, PreImage] | None = None
            if restored_to is None:
                images = _images()
                best = _JudgedState(turns, reading, images)
                # The joint ask's candidate: this turn's changed bodies, read
                # while this turn is the one on the tree. Read at the end,
                # the tree could hold a later turn's bodies instead.
                _best_bodies = (
                    {f"{site_.file}::{site_.fn_name}": text_
                     for site_, text_ in ports.enumerate_bodies().items()
                     if text_ and text_ != baseline_bodies.get(site_)}
                    if (reading.regressed and reading.newly_green
                        and reading.newly_red) else {})
            if keep_best and end_on_keepable and not reading.regressed and (
                    keepable is None
                    or judge_against_best(reading, keepable.reading).holds):
                # Captured here, while this turn's bodies are still on the
                # tree: the restore below puts the best's bytes back.
                if images is None:
                    images = _images()
                keepable = _JudgedState(turns, reading, images)
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
            pending = (render_test_turn(
                reading, rejected, dropped=dropped, dropped_diagnostics=dropped_diag,
                crashed=crashed,
                coverage_note=note, restored_note=restored_note)
                + "\n" + render_budget_turn(max_turns - turns))
    except BaseException:
        # A stop mid-session: the tree must not hold an unjudged change set.
        # It is put back to the best judged state when the end-of-session
        # decision would have kept that state as it stands (not regressed
        # against the opening suite — no peel needed), else to the baseline.
        if not accepted:
            end_state = (best if best is not None and not best.reading.regressed
                         else keepable)
            if end_state is not None:
                _restore(end_state.images)
            else:
                _restore(baseline_texts)
        raise
    if model_calls and first_turn_minutes is None:
        first_turn_minutes = (ports.now() - opened_at) / 60.0

    # The tree holds the best judged state, but the gate below decides
    # whether it may stay by `TurnReading.regressed` — while the order that
    # picked it ranks the targets first. When the preferred state is one the
    # gate refuses, the session ends on the best state the gate ACCEPTS
    # instead of walking back toward the baseline: a measured non-regressing
    # reading is not evidence the session may throw away, and the peel can
    # only reach a subset of the refused state, never the other turn's bodies.
    ended: _JudgedState | None = best
    _candidate_bodies: dict[str, str] = (
        dict(_best_bodies) if best is not None else {})
    if (not accepted and best is not None and best.reading.regressed
            and keepable is not None and keepable.turn != best.turn):
        _restore(keepable.images)
        ended = keepable
        ports.log(f"  [phase-c/session] {label}: turn {best.turn}'s state "
                  f"({best.reading.pass_delta:+d}, "
                  f"{len(best.reading.newly_red)} newly red) is one the keep "
                  f"gate refuses — ended on turn {keepable.turn}'s state "
                  f"instead ({keepable.reading.pass_delta:+d}, no regression), "
                  "the best judged state the gate accepts.")
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
    final = ended.reading if ended is not None else None
    # Unless a branch below takes the end state back, the tree holds it.
    held_green: frozenset[str] | None = (
        final.green_targets if final is not None
        else _green_targets_in(baseline, slice_.tests))
    if accepted:
        outcome, kept = "accepted", changed
    elif changed:
        if final is not None:
            # Carried whether the set is kept or peeled: a kept net-positive
            # turn's newly-red tests are the next prompt's evidence.
            newly_red = final.newly_red
            newly_red_output = dict(final.newly_red_said)
        if final is not None and final.regressed:
            if final.hung:
                # A hang is not a peelable subset: later binaries never
                # ran, so no member's keep can be read. File-first already
                # reverts on hang; the session's peel kept one half of a
                # hung two-unit write (run `19980-f9ea6adb` fill 2).
                for pre_image in baseline_texts.values():
                    restore(pre_image)
                ports.log(f"  [phase-c/session] {label}: hung binary — "
                          "the whole change set restored (a hang is not a "
                          "peelable subset; later binaries never ran).")
                kept, reverted = frozenset(), changed
                outcome = "reverted"
                held_green = _green_targets_in(baseline, slice_.tests)
            else:
                kept, reverted, _peel_held = _peel(
                    ports, target_src_dir, slice_, records, baseline,
                    baseline_bodies, baseline_texts, changed,
                    peel_max=peel_max, label=label)
                outcome = "kept" if kept else "reverted"
                held_green = (_green_targets_in(_peel_held, slice_.tests) if kept
                              else _green_targets_in(baseline, slice_.tests))
        else:
            outcome, kept = "kept", changed
    result = SessionOutcome(
        outcome=outcome, turns=turns, model_calls=model_calls, kept=kept,
        reverted=reverted, dispatched=frozenset(dispatched), notes=notes,
        final=final, compile_failures=compile_failures,
        transcript_chars=conv.chars(),
        newly_red=newly_red, newly_red_output=newly_red_output,
        best_turn=ended.turn if ended is not None else None,
        preferred_turn=best.turn if best is not None else None,
        restored_turns=restored_turns, capped_turns=capped_turns,
        judged_turns=judged_turns, judged_capped_turns=judged_capped,
        answered_turns=answered_turns, requests=requests,
        first_turn_minutes=first_turn_minutes,
        candidate_bodies=_candidate_bodies, seed_judged=seed_judged,
        held_green=held_green)
    ports.emit("phase_c.session.end", data={
        "cluster": cluster_name, "outcome": outcome, "turns": turns,
        "model_calls": model_calls, "compile_failures": compile_failures,
        "kept": [site_payload(u) for u in sorted(kept)],
        "reverted": [site_payload(u) for u in sorted(reverted)],
        "notes_chars": len(notes),
        "best_turn": result.best_turn, "preferred_turn": result.preferred_turn,
        "restored_turns": restored_turns,
        "capped_turns": capped_turns, "judged_turns": judged_turns,
        "judged_capped_turns": judged_capped, "unmeasured": result.unmeasured,
        "answered_turns": answered_turns, "requests": requests,
        "unanswered": result.unanswered,
        "first_turn_minutes": (round(first_turn_minutes, 3)
                               if first_turn_minutes is not None else None)})
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
) -> tuple[frozenset[FnSite], frozenset[FnSite], TestStateReading | None]:
    """Restore changed units one at a time until the suite holds; the rest
    are kept, and the reading that held is returned with them (None when
    the baseline was restored). Restores the whole baseline when the budget runs out or
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
        after = ports.measure_suite(raw, MeasureRole.JUDGMENT)
        if after is None or baseline is None:
            continue
        cmp = compare_readings(baseline, after)
        # The turn judge's bar (`TurnReading.regressed`): a strict net gain
        # forgives a newly-red test; a target failing earlier than at
        # baseline is a loss only at or below zero; a destroyed verdict is
        # judged by the abort partition, on the peeled tree's own crash.
        _outcomes = _target_outcomes(baseline, after, slice_.tests)
        _sites = _abort_sites(bound_crash_settler(), None, _outcomes,
                              raw[-1] if raw else "", baseline)
        if _sites:
            _outcomes = _target_outcomes(baseline, after, slice_.tests, _sites)
        _earlier = () if _outcomes.gained else _outcomes.earlier_tests
        _aborts_refuse = _outcomes.abort.refuses(
            strict_gain=cmp.net_pass_delta > 0, lost=bool(cmp.newly_red))
        if (not cmp.regressed and not _aborts_refuse
                and not (_earlier and cmp.net_pass_delta <= 0)):
            ports.log(f"  [phase-c/session] {label}: peeled {len(reverted)} unit(s) — "
                      f"the remaining {len(remaining) + len(unrestored)} hold "
                      "with no previously-passing test red.")
            return (frozenset(remaining) | frozenset(unrestored),
                    frozenset(reverted), after)
    for pre_image in baseline_texts.values():
        restore(pre_image)
    ports.log(f"  [phase-c/session] {label}: no subset of the change set holds "
              "within the peel budget — baseline restored.")
    return frozenset(), changed, None


def _green_targets_in(reading: TestStateReading | None,
                      tests: Sequence[str]) -> frozenset[str] | None:
    """The targets `reading` saw passing and nowhere failing (bare names)."""
    if reading is None:
        return None
    passed = {n.rsplit("::", 1)[-1] for n in reading.passed_names}
    failed = {n.rsplit("::", 1)[-1] for b in reading.binaries for n in b.failed}
    return frozenset(t for t in tests if t.rsplit("::", 1)[-1] in passed
                     and t.rsplit("::", 1)[-1] not in failed)
