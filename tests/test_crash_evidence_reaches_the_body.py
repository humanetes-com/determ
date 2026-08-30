"""A localized runtime fault must reach the body it blames — as a dispatch, as
a record, and as an instruction.

THE CLASS. Every mechanism in the chain below grades a unit by asking a
question whose answer a crash destroys. `collect_refill_evidence` earns DIRECT
from `frontier.convicting_for`; `declined_unchanged` withholds `declined` from
a unit some failing test convicts. A crashed binary reports NO verdict for
anything inside it, so the fault's own blamed frame answers "nothing convicts
this" — and was graded the weakest thing in each vocabulary. The louder the
failure, the more confidently the loop concluded the body was fine.

THE RECORDED INSTANCE, run `1519852-5d607329` (2026-08-30, cluster stable).
Five binaries died on `AddressSanitizer: heap-use-after-free`; the localizer
blamed `src/error.rs::drop` from the faulting frame and aimed
`src/error.rs::object_drop` and `src/ptr.rs::as_ptr` as evidence; 13 tests
reported no verdict for the whole run. The layout retrigger — the mechanism
that owns re-authoring a faulting component — was capped out at 2 of 2 for that
blame and printed its refusal in five consecutive processes (log lines 1556,
1914, 2182, 2419, 2676), naming the fallback: "the blamed fn stays in the fill
frontier with its crash report instead." All three parts of that fallback were
closed:

* dispatch — `promote_crash_evidence` put the three at ranks 0-2 and
  `take_batch` dropped them again, because the pass cursor had offered them
  once. The log said `0 of them are in this batch — the batch is full of
  higher-graded evidence`, which cannot be true: crash promotion runs last.
* the record — `drop` and `as_ptr` were written to the floor's `declined`,
  the one population `cursor_carry` keeps unconditionally across a relaunch.
* the instruction — `drop` reached the prompt tagged `[REFILL?] … Rewrite ONLY
  if wrong for these bounds, else return it unchanged`, and it did.

The fixture is that run's own persisted floor
(`tests/fixtures/dispatch/crash_cursor_1519852_2026-08-30.json`, 36 attempted
units, 13 declined), not a hand-made list. Every test below carries the pre-fix
call as its negative control and asserts the bad outcome still reproduces
there.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.dispatch_batch import promote_crash_evidence, take_batch
from pipeline_core.domain.landed_change import declined_unchanged
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    collect_refill_evidence,
    crashed_refill_names,
    inferred_refill_names,
    render_refill_evidence,
)
from pipeline_core.prompts.fix_prompts import BlockFillPlan, _plan_line
from pipeline_core.prompts.sections import format_impl_block_for_prompt

FIXTURE = (Path(__file__).parent / "fixtures" / "dispatch"
           / "crash_cursor_1519852_2026-08-30.json")

DROP = FnLocator("src/error.rs", "drop")
OBJECT_DROP = FnLocator("src/error.rs", "object_drop")
AS_PTR = FnLocator("src/ptr.rs", "as_ptr")


def _recorded() -> dict:
    return json.loads(FIXTURE.read_text())


def _locators(names: list[str]) -> list[FnLocator]:
    return [FnLocator(*n.split("::", 1)) for n in names]


def _live_frontier() -> tuple[list[FnLocator], list[FnLocator], dict[FnLocator, int]]:
    """The recorded sweep's (ordered frontier, attempted cursor, crash grades).

    The floor records the 36 units the pass had attempted; the event store keeps
    a frontier FINGERPRINT rather than the list, so the 10 never-attempted units
    are stand-ins. They carry no weight: what is under test is whether the three
    promoted units survive the `attempted` filter, and that turns only on the
    filter, the cap and the ranks — all of which are the recorded ones.
    """
    rec = _recorded()
    attempted = _locators(rec["attempted"])
    grades = {DROP: 0, OBJECT_DROP: 2, AS_PTR: 2}
    others = [FnLocator("src/other.rs", f"pending_{i}") for i in range(10)]
    frontier = list(dict.fromkeys([*grades, *attempted, *others]))
    assert len(frontier) == rec["frontier_size"]
    ordered = [k for k, _ in promote_crash_evidence(
        [(k, 0) for k in frontier], grades)]
    return ordered, attempted, grades


def test_the_recorded_floor_is_the_one_the_run_wrote() -> None:
    """The fixture's premises, so a later edit to it cannot quietly pass."""
    rec = _recorded()
    assert rec["cluster"] == "stable"
    assert rec["code_id"] == "099b7bc2ca93"
    assert len(rec["attempted"]) == 36
    # The three units the localizer named were all already spent by the cursor,
    # and two of them were also filed as correctly standing pat.
    for unit in ("src/error.rs::drop", "src/error.rs::object_drop",
                 "src/ptr.rs::as_ptr"):
        assert unit in rec["attempted"], unit
    assert "src/error.rs::drop" in rec["declined"]
    assert "src/ptr.rs::as_ptr" in rec["declined"]


def test_crash_blamed_units_are_dispatched_though_the_pass_already_spent_them() -> None:
    """The plan's sentence: on 1519852's own cursor the first batch contains all
    three crash-evidence units, where before it contained none of them."""
    ordered, attempted, grades = _live_frontier()
    cap = _recorded()["dispatch_cap"]
    assert [f"{k.file}::{k.fn_name}" for k in ordered[:3]] == [
        "src/error.rs::drop", "src/error.rs::object_drop", "src/ptr.rs::as_ptr"]

    before = take_batch(ordered, attempted, cap, cohort=tuple(ordered))
    assert [k for k in before.keys if k in grades] == [], (
        "negative control: the recorded sweep logged `0 of them are in this "
        "batch` and the pre-fix call must still reproduce it")

    after = take_batch(ordered, attempted, cap, cohort=tuple(ordered),
                       exempt=grades.keys())
    assert [k for k in after.keys if k in grades] == [DROP, OBJECT_DROP, AS_PTR]


def test_the_exemption_ends_with_the_fault_and_not_on_a_budget() -> None:
    """No crash this sweep is the whole off switch — the exemption is recomputed
    from evidence the sweep re-derives, so a fixed body stops being re-offered
    without anything having to remember that it was."""
    ordered, attempted, _ = _live_frontier()
    healed = take_batch(ordered, attempted, 12, cohort=tuple(ordered),
                        exempt=frozenset())
    assert [k for k in healed.keys if k in (DROP, OBJECT_DROP, AS_PTR)] == []


def test_a_unit_the_fault_names_is_not_recorded_as_correctly_standing_pat() -> None:
    """`declined` asserts the model was ASKED to return the body unchanged. The
    recorded floor says that about `drop` and `as_ptr` while ASAN was blaming
    `drop`'s frame."""
    unchanged = {DROP, AS_PTR, FnLocator("src/error.rs", "erase")}
    # A crashed binary reports no verdict, so nothing convicts these units —
    # which is exactly how they earned the label.
    convicting: dict[FnLocator, set[str]] = {}

    before = declined_unchanged(unchanged, convicting=convicting,
                                escalated=frozenset())
    assert DROP in before and AS_PTR in before, (
        "negative control: the recorded floor carries both in `declined`")

    after = declined_unchanged(unchanged, convicting=convicting,
                               escalated=frozenset(),
                               crash_evidence=frozenset({DROP, AS_PTR}))
    assert DROP not in after and AS_PTR not in after
    assert FnLocator("src/error.rs", "erase") in after, (
        "an ordinary unmeasured re-check is still a decline")


def test_an_earlier_decline_does_not_survive_the_fault_that_contradicts_it() -> None:
    """`already_declined` is the pass's record from earlier batches and from the
    resumed floor. A fault named on THIS sweep outranks it."""
    kept = declined_unchanged(
        {DROP}, convicting={}, escalated=frozenset(),
        already_declined=frozenset({DROP}),
        crash_evidence=frozenset({DROP}))
    assert kept == frozenset()


def _drop_block() -> ImplBlock:
    member = FnRecord(
        file="src/error.rs", fn_name="drop", sig_text="fn drop(&mut self)",
        body_text=("unsafe { let ptr = self.inner.ptr; "
                   "let vtable = (*ptr.as_ptr()).vtable; "
                   "std::mem::forget(self); let own = Own { ptr }; "
                   "(vtable.object_drop)(own); }"),
        has_unimplemented_body=False)
    return ImplBlock(file="src/error.rs", container="impl Drop for Error {",
                     members=(member,), refill_members=(member,))


def test_the_crash_erases_the_conviction_that_would_have_graded_the_body() -> None:
    """Why CRASHED cannot be folded into DIRECT: with the binary dead there is
    no convicting test to earn DIRECT with, so the grade below is what the unit
    gets on the evidence the sweep actually holds."""
    block = _drop_block()
    empty = FailureFrontier.build({DROP: {"test_convert"}}, {})

    (before,) = collect_refill_evidence(block, [block], empty)
    assert before.attribution is Attribution.NAMED, (
        "negative control: with no crash set the fault's own frame grades as a "
        "name match, which is what put `return it unchanged` in its prompt")

    (after,) = collect_refill_evidence(block, [block], empty,
                                       crashed=frozenset({DROP}))
    assert after.attribution is Attribution.CRASHED
    assert crashed_refill_names([after]) == frozenset({"drop"})
    assert inferred_refill_names([after]) == frozenset()


def test_the_blamed_body_is_never_told_to_return_itself_unchanged() -> None:
    """The instruction slot, on the per-impl render."""
    block = _drop_block()
    names = frozenset({"drop"})

    before = format_impl_block_for_prompt(block, refill_fn_names=names,
                                          inferred_fn_names=names)
    assert "[REFILL?]" in before and "return it unchanged" in before, (
        "negative control: this is the tag `drop` carried on every sweep")

    after = format_impl_block_for_prompt(block, refill_fn_names=names,
                                         inferred_fn_names=names,
                                         crashed_fn_names=names)
    assert "[REFILL!]" in after
    assert "[REFILL?]" not in after
    assert "do NOT return it unchanged" in after


def test_the_evidence_section_states_the_fault_instead_of_hedging() -> None:
    block = _drop_block()
    empty = FailureFrontier.build({DROP: {"test_convert"}}, {})
    (named,) = collect_refill_evidence(block, [block], empty)
    (crashed,) = collect_refill_evidence(block, [block], empty,
                                         crashed=frozenset({DROP}))

    assert "nothing singles out" in render_refill_evidence([named]), (
        "negative control: the INFERRED sentence the fault used to get")
    text = render_refill_evidence([crashed])
    assert "CRASHED" in text and "MEMORY FAULT" in text
    assert "return it UNCHANGED" not in text


def test_the_file_first_plan_line_names_the_crash_first() -> None:
    """The live fill used `build_file_fill_prompt`, so the plan line is the slot
    that actually carried the instruction for `src/error.rs::drop`."""
    plan = BlockFillPlan(header="impl Drop for Error {",
                         refills=("drop", "erase"), inferred=("drop", "erase"))
    before = _plan_line(plan)
    assert "re-check" in before and before.rstrip().endswith("drop, erase"), (
        "negative control: graded as a name match, the file route put `drop` on "
        "the re-check line — 'rewrite ONLY if it is wrong … else leave it "
        "unchanged'")

    crashed = BlockFillPlan(header="impl Drop for Error {",
                            refills=("drop", "erase"),
                            inferred=("drop", "erase"), crashed=("drop",))
    line = _plan_line(crashed)
    crash_part, _, recheck_part = line.partition("; re-check")
    assert "A TEST BINARY CRASHED INSIDE THIS BODY" in crash_part
    assert crash_part.rstrip().endswith("drop")
    # The crash grade CONSUMES the unit. `drop` must not also appear on the
    # re-check line, which would hand the model both instructions at once and
    # let it pick the one that costs nothing.
    assert "drop" not in recheck_part
    assert recheck_part.rstrip().endswith("erase"), (
        "an ordinary name match in the same block keeps its re-check line")


def test_the_plan_line_for_a_conversion_crash_forbids_self_dropping() -> None:
    """Run `1790133-2181e509`'s error.rs file-first prompt named `drop` on the
    crashed line. `into_boxed_dyn_error` was in CRASH_EVIDENCE and not on the
    write list. Once blamed, `_plan_line` (the live builder's instruction
    slot) must carry the ownership MUST. A `drop` crash must not — that body
    already forgets, and the MUST on it would re-instruct the innocent
    destructor.
    """
    drop_plan = BlockFillPlan(header="impl Drop for Error {",
                              refills=("drop",), crashed=("drop",))
    drop_line = _plan_line(drop_plan)
    assert "A TEST BINARY CRASHED INSIDE THIS BODY" in drop_line
    assert "self must not drop" not in drop_line, (
        "negative control: a destructor crash must not grow the conversion MUST")

    conv = BlockFillPlan(header="impl Error {",
                         refills=("into_boxed_dyn_error",),
                         crashed=("into_boxed_dyn_error",))
    line = _plan_line(conv)
    assert "into_boxed_dyn_error" in line
    assert "MUST:" in line
    assert "self must not drop" in line
    assert "let own = self.inner" in line


def test_a_re_offered_unit_does_not_hold_the_traversal_open() -> None:
    """The exemption buys a re-dispatch, not a second traversal credit.

    If exempt units counted as outstanding, a fault blaming more frames than
    the cap would make `completes_pass` unsatisfiable — and both terminators
    denominated in completed passes would go quietly dead while the loop kept
    spending GPU."""
    ordered, attempted, _ = _live_frontier()
    # Every unit already served, and more blamed frames than one batch can hold.
    wide = dict.fromkeys(ordered[:20], 0)
    batch = take_batch(ordered, ordered, 12, cohort=tuple(ordered),
                       exempt=wide.keys())
    assert len(batch.keys) == 12
    assert batch.completes_pass, (
        "a pass whose cohort is fully served closes even when the exemption "
        "re-offers more units than the cap can take")
