"""The current sweep's coverage reading, and the instruction it licenses.

A body no test executes cannot move a verdict, and a branch no test reaches
cannot be the fill that was missing. The ranking consumes the dead set and
the red-dead set (`dispatch_batch.promote_by_service`); the prompt consumes
the sentences below, on the instruction slot (`_plan_line` / the per-fn
opener), not as another evidence section — a fill can carry the evidence and
still wrap.

`owners` is the third reading and the one that answers a different question:
which bodies does ONE failing test enter. The dead set is a whole-suite
merge, so it cannot tell a unit no test runs from a unit THIS test does not
run — and the second is what decides whether a fix loop entered on this
test's failure can ever succeed. Measured 2026-08-30 on cluster `stable`,
run `2450551-9a6a8683`: `src/lib.rs::Ok` was convicted by `test_trailer`,
which enters 24 bodies, two of them in `lib.rs` — `format_err` and
`must_use`, never `Ok` (`tests/test_ensure.rs` imports anyhow's `anyhow`,
`ensure`, `Chain`, `Error` and `Result`, so its `Ok(...)` is the prelude's).
The fix loop spent 4,550 s over five attempts and two retrospective rounds
rewriting a body that was already correct, and kept it. `Ok` is not in the
dead set either: it is generic and `#[inline]`, so with no caller there is
no instantiation and no region at all — absent from the reading, not zero.
That is why `convictors_never_enter` refuses on the POSITIVE evidence of a
non-empty owner set and never on absence.

Bound by the TDD driver for one sweep (the instrumented copy lives in a
scratch target dir; this module holds only the names). Unbound, every
helper returns empty or None and the loop behaves as it did before the
reading existed. The process-global bind is cleared when `run_phase_c_tdd`
returns, including on SystemExit, so a later phase cannot read a stale
slice.

Coverage reaches `convicting` through exactly one rule, `screen_guidance`'s
`convictors_never_execute`, and through the pre-loop gate in `fill_one_fn`.
Both refuse work; neither can select a unit or author a verdict.

The owner map reaches RANKING through `red_dead_units`, which only sinks a
unit in the order — it removes nothing from the frontier, authors no verdict
and files no exhaustion, so a unit demoted on a reading that later changes
comes back on the next sweep at its earned rank. Ranking was the missing
consumer: with the refusals in `fill_one_fn` alone, the loop still BUYS the
unreachable unit and only declines to keep spending on it, and the buy is
the expensive half — on run `2651653-7d5bce0f` `src/lib.rs::Ok` was ranked
first-bucket, carried to the file-first route by
`dispatch.refills_deferred_by_impl_route` and given a whole-file model call
before any per-fn gate could be consulted.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from pipeline_core.domain.models import FnLocator

DEAD_BODY = (
    "This body is executed by no test in the suite; whatever is wrong is "
    "in its caller."
)

RED_DEAD_BODY = (
    "No FAILING test executes this body — passing tests do, so it is not "
    "dead code; rewriting it cannot move a red verdict until whatever "
    "should reach it on the failing path does."
)

# Plan reading 3 selected dead regions by fraction (72%, 20%, 20%). A
# cfg-gated one-liner on a unit the ranking wants aimed well sits below this.
DEAD_FRACTION = 0.20


@dataclass(frozen=True, slots=True)
class CountedBody:
    """One instrumented body, named at the file+fn the loop can dispatch.

    `start` is the first region line — the adapter's body key, which is
    unambiguous. Two records with the same locator are the ambiguous unit
    key: `slice_from_counts` is the contract that collapses them, and a
    locator is dead only when every such body has count 0.
    """

    locator: FnLocator
    start: int
    count: int
    zero_lines: tuple[int, ...] = ()
    span_lines: int = 0


@dataclass(frozen=True, slots=True)
class CoverageSlice:
    """Names the current tree's coverage can speak about.

    `dead` is locators for which every body under the name ran in no test.
    A locator that is live at one line and dead at another is not dead —
    the loop dispatches by file+name, and the live body is the fill target.
    `dead_regions` maps an unambiguous live locator to the sentence about
    the lines that did not run — the missing-delegation class, where the
    slice under-covers because the caller is wrong. Ambiguous locators
    get no region sentence until the site key lands.

    `owners` maps a test name to the locators that test alone executed,
    measured by running it under `--exact` on its own binary. A test the
    adapter was not asked to profile, or could not find a binary for, is
    ABSENT from the mapping — which is not the same as an empty owner set,
    and the two must not be collapsed: absent means unmeasured, empty means
    measured and it entered no crate body (a test that panics in its own
    setup). Only a non-empty entry supports a refusal.
    """

    dead: frozenset[FnLocator] = field(default_factory=frozenset)
    dead_regions: Mapping[FnLocator, str] = field(default_factory=dict)
    owners: Mapping[str, frozenset[FnLocator]] = field(default_factory=dict)
    # The red set the owner map was ASKED for, which is not the same as the
    # set it answered: a test with no matching binary is missing from
    # `owners`, and the difference is exactly the ignorance every consumer
    # here refuses on. Carried so a consumer holding only the slice can
    # apply the same positive-evidence rule the ranking applies with
    # `cur_failed` in hand.
    failing: frozenset[str] = frozenset()


def contiguous_runs(lines: Iterable[int]) -> tuple[tuple[int, int], ...]:
    """Inclusive [start, end] ranges of consecutive line numbers."""
    ordered = sorted(set(lines))
    if not ordered:
        return ()
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for n in ordered[1:]:
        if n == prev + 1:
            prev = n
        else:
            runs.append((start, prev))
            start = prev = n
    runs.append((start, prev))
    return tuple(runs)


def region_sentence(zeros: Iterable[int]) -> str:
    """Name the unreachable lines as contiguous runs, not a spanning range.

    A min–max of non-contiguous zeros names reached lines between them as
    if they were dead. One line is singular; several runs are comma-joined.
    """
    runs = contiguous_runs(zeros)
    if not runs:
        return ""
    if len(runs) == 1:
        lo, hi = runs[0]
        if lo == hi:
            return (f"Line {lo} of the body you are editing is "
                    "never reached.")
        return (f"Lines {lo}–{hi} of the body you are editing are "
                "never reached.")
    parts: list[str] = []
    for lo, hi in runs:
        parts.append(str(lo) if lo == hi else f"{lo}–{hi}")
    return (f"Lines {', '.join(parts)} of the body you are editing "
            "are never reached.")


def slice_from_counts(
    bodies: Iterable[CountedBody],
    *,
    dead_fraction: float = DEAD_FRACTION,
    owners: Mapping[str, frozenset[FnLocator]] | None = None,
    failing: Iterable[str] = (),
) -> CoverageSlice:
    """The contract that writes the dead set the ranking and prompts consume.

    A locator is dead only when every body under it has count 0. Measured
    2026-08-30 on the filled stable tree: collapsing by file+name without
    this check reported `src/error.rs::backtrace` dead at line 918 (the
    ErrorImpl body) while the Error method at line 389 was live — an earned
    frontier unit for both backtrace tests, and the body that needs the
    vtable fallback. Subtracting any nonzero-count body's locator drops
    that unit out of the dead bucket.

    Dead-region sentences are omitted when more than one body shares the
    locator (a first-wins map would name a line outside the body being
    edited — `error.rs::fmt` stores the body at 667 and a fill of Debug
    at 690 would be told about line 678) and when the dead fraction of
    instrumented lines is below `dead_fraction` (the None arm of
    `if cfg(feature = "std")` on `error.rs::msg`).
    """
    grouped: dict[FnLocator, list[CountedBody]] = {}
    for body in bodies:
        grouped.setdefault(body.locator, []).append(body)
    dead: set[FnLocator] = set()
    regions: dict[FnLocator, str] = {}
    for loc, group in grouped.items():
        if group and all(b.count == 0 for b in group):
            dead.add(loc)
            continue
        if len(group) != 1:
            continue
        body = group[0]
        if body.count == 0 or not body.zero_lines or body.span_lines <= 0:
            continue
        if len(body.zero_lines) / body.span_lines < dead_fraction:
            continue
        sentence = region_sentence(body.zero_lines)
        if sentence:
            regions[loc] = sentence
    return CoverageSlice(dead=frozenset(dead), dead_regions=regions,
                         owners=dict(owners or {}),
                         failing=frozenset(failing))


_bound: CoverageSlice | None = None


def bind(reading: CoverageSlice | None) -> None:
    """Install (or clear) the sweep's reading. Driver-owned."""
    global _bound
    _bound = reading


def current() -> CoverageSlice | None:
    return _bound


def instruction_for(file_rel: str, fn_name: str) -> str:
    """The coverage sentences that belong on THIS unit's instruction line.

    Empty when nothing is bound or this unit is not in the reading. The
    key is `FnLocator(file, fn_name)`: a line-keyed owner degrades to the
    file+name key (three `fmt` bodies share one locator) until the site
    key lands.
    """
    reading = _bound
    if reading is None:
        return ""
    loc = FnLocator(file_rel, fn_name)
    parts: list[str] = []
    if loc in reading.dead:
        parts.append(DEAD_BODY)
    elif convictors_never_enter(loc, reading.failing, reading.owners):
        # The unit is alive and the ranking still dispatched it — an
        # escalated producer is exempt from the red-dead demotion precisely
        # because the chain breaks upstream of it. Say that in the prompt
        # rather than leave the model to infer from a failing test it cannot
        # see the path to. Same positive-evidence rule as the demotion: the
        # slice carries the red set it was ASKED for, so an unmeasured test
        # declines here exactly as it declines there.
        parts.append(RED_DEAD_BODY)
    region = reading.dead_regions.get(loc, "")
    if region:
        parts.append(region)
    return " ".join(parts)


def instructions_for(file_rel: str, names: Iterable[str]) -> str:
    """Join per-name instructions, naming the fn when more than one speaks."""
    bits: list[tuple[str, str]] = []
    for name in dict.fromkeys(names):
        text = instruction_for(file_rel, name)
        if text:
            bits.append((name, text))
    if not bits:
        return ""
    if len(bits) == 1:
        return bits[0][1]
    return " ".join(f"{n}: {t}" for n, t in bits)


def instruction_opener(file_rel: str, names: Iterable[str]) -> str:
    """The same sentences as a prompt-opener paragraph, or empty.

    Unbound (or a unit the reading does not name) returns "" so a builder
    that concatenates this next to `identity_must` stays byte-identical
    to a call that never knew about coverage.
    """
    text = instructions_for(file_rel, names)
    return f"{text}\n\n" if text else ""


def convictors_never_enter(
    unit: FnLocator, tests: Iterable[str],
    owners: Mapping[str, frozenset[FnLocator]],
) -> frozenset[str] | None:
    """The tests that PROVABLY do not execute `unit`, or None when the
    reading cannot decide — the measured counterpart of
    `dispatch.sibling_blocked_u2_failures`.

    That check asks whether a still-stubbed sibling holds every failing
    test, which is one reason a rewrite of this unit cannot pass them.
    This asks the general question the stub predicate is a special case of:
    does the test enter this body at all. Both failures are
    attempt-invariant, so both belong before the fix loop spends a call —
    but they are not the same set, and on the tree that named this one the
    stub check correctly declined (one stub left in 209 fillable units,
    so nothing was sibling-blocked) while 4,550 s went to a unit no
    convicting test executes.

    Conservative on ignorance, and asymmetrically so, because the two
    ignorance cases are not alike:

    - a test ABSENT from `owners` was never profiled (not requested, or no
      binary matched its name), so nothing is known and the answer is None;
    - a test with an EMPTY owner set was profiled and entered no crate body
      at all — a test that aborts in its own setup, or a binary that
      crashed before writing a profile. A crashing binary writes no
      profile, so "empty" is exactly the reading a crash produces, and
      convicting a unit on it would repeat the mistake this function
      exists to stop. Also None.

    So a refusal rests only on positive evidence: every named test was
    measured, each entered at least one crate body, and none of them
    entered this one. Anything else returns None and the caller proceeds
    exactly as it did before the reading existed."""
    names = list(dict.fromkeys(tests))
    if not names:
        return None
    for t in names:
        entered = owners.get(t)
        if not entered:
            return None
        if unit in entered:
            return None
    return frozenset(names)


def red_dead_units(
    units: Iterable[FnLocator],
    failing: Iterable[str],
    owners: Mapping[str, frozenset[FnLocator]],
    *,
    exempt: Collection[FnLocator] = (),
) -> frozenset[FnLocator]:
    """The units NO failing test executes — `dead` measured against the red
    set instead of the whole suite — minus the ones a chain argument owns.

    `dead` asks "does anything in the suite run this body". That is the
    right question for a body nothing calls and the wrong one for a body a
    hundred PASSING tests run and no failing test touches: rewriting it
    cannot move a red verdict this sweep, yet it ranks as a live
    never-served unit and takes a cap slot. `src/lib.rs::Ok` is the
    measured instance and it is invisible to `dead` by construction —
    generic and `#[inline]`, so with no caller there is no instantiation
    and no region at all.

    WHY `exempt` IS NOT OPTIONAL IN PRACTICE, and what happens without it.
    Computed on run `2651653-7d5bce0f`'s recorded iter-1 frontier with the
    owner map measured on its own filled tree (7 failing tests): 23 of the
    ranked units are entered by no failing test, and 9 of those are
    escalated additions that are NOT dead — `src/error.rs::construct_from_
    boxed`, `::construct_from_display`, `::deref`, `::deref_mut`,
    `::from_boxed`, `::new`, `src/chain.rs::len`,
    `src/context.rs::with_context`, `src/fmt.rs::write_str`. Five of them
    are in that iteration's ten never-served units. An escalated producer
    is unentered PRECISELY when the chain breaks upstream of it, which is
    the case escalation exists to serve: the family is rewritten together
    and the test reaches the later members only after the earlier ones are
    right. Demoting on the reading alone would sink half the never-served
    head and silently undo the constructor-first promotion. With the
    escalated cohort exempt the same computation demotes 11 units, 10 of
    which `dead` already covers; the one it adds is `src/lib.rs::Ok`.

    Conservative on ignorance through `convictors_never_enter`: an unmeasured
    test, an empty owner set (a crashed binary writes no profile) or an empty
    red set all decline, so a unit is demoted only on positive evidence that
    every red test was measured, each entered some crate body, and none
    entered this one."""
    reds = list(dict.fromkeys(failing))
    if not reds:
        return frozenset()
    skip = set(exempt)
    return frozenset(
        u for u in units
        if u not in skip and convictors_never_enter(u, reds, owners))


def unreached_by(unit: FnLocator, tests: Iterable[str]) -> frozenset[str] | None:
    """`convictors_never_enter` against the sweep's BOUND reading.

    None when nothing is bound, so an unbound loop behaves as it did
    before the reading existed."""
    reading = _bound
    if reading is None:
        return None
    return convictors_never_enter(unit, tests, reading.owners)


def unreached_by_test(unit: FnLocator, test: str) -> bool:
    """One test's positive-evidence read against the BOUND reading: True only
    when the test was measured, entered at least one crate body, and `unit`
    is not among them — the same asymmetric-ignorance rule as
    `convictors_never_enter`, for a caller holding a single test name.

    Name reconciliation is exact-first-then-leaf (`FailureFrontier._named`):
    the owner map is keyed as the sweep parsed the name (`gen_tests_2::t` or
    the bare leaf) while attribution may hold the other form, and an aliased
    miss must read as "unmeasured", never as "enters nothing". Unbound, or
    an unmeasured test, returns False and the caller behaves as it did
    before the reading existed."""
    reading = _bound
    if reading is None:
        return False
    key = test
    if key not in reading.owners:
        leaf = test.rsplit("::", 1)[-1]
        key_match = [n for n in reading.owners
                     if n.rsplit("::", 1)[-1] == leaf]
        if len(key_match) != 1:
            return False
        key = key_match[0]
    return bool(convictors_never_enter(unit, [key], reading.owners))


def measured_entry_sets(tests: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """The bound reading's measured entry set per test, rendered as sorted
    `file::fn` strings — the checkable premise a behavioural retrospective is
    stamped with at conviction time, and the current reading it is compared
    against on read.

    Same positive-evidence rule as `convictors_never_enter`: a test absent
    from the owner map was never profiled, and an empty owner set is what a
    crashed binary produces — both are omitted rather than recorded as "enters
    nothing". Unbound returns `{}`, so a caller without a sweep reading stamps
    no premise and judges none."""
    reading = _bound
    if reading is None:
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for t in dict.fromkeys(tests):
        entered = reading.owners.get(t)
        if entered:
            out[t] = tuple(sorted(f"{u.file}::{u.fn_name}" for u in entered))
    return out
