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
from dataclasses import dataclass, field, replace

from pipeline_core.domain.attribution import locator_of
from pipeline_core.domain.fn_execution import executed_by_key
from pipeline_core.domain.models import FnLocator, FnSite

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

    locator: FnSite
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

    `owners` maps a test name to the sites that test alone executed,
    measured by running it under `--exact` on its own binary. A test the
    adapter was not asked to profile, could not find a binary for, or whose
    process DID NOT COMPLETE is ABSENT from the mapping — which is not the
    same as an empty owner set, and the two must not be collapsed: absent
    means unmeasured, empty means measured and it entered no crate body (a
    test that panics in its own setup). Only a non-empty entry supports a
    refusal.

    `unmeasured_binaries` and `unmeasured_tests` name the part of the suite
    the reading could NOT take: a test binary killed by a signal (SIGSEGV,
    SIGABRT from an abort-in-destructor) flushes no LLVM profile, so every
    body only its tests execute counts 0 in the merge and the sentence
    "executed by no test" is false for them. The adapter re-runs such a
    binary's tests one process each so the tests that do complete are
    measured; the ones that still die are `unmeasured_tests`, and their
    executed set is unknown this sweep (`withhold_unmeasured`).
    Measured 2026-09-07 on run `1577437-c93d5c26`'s iter-4 tree: three of
    45 binaries died (SIGSEGV, SIGABRT, SIGABRT), 6 of 13 red tests had no
    completing process, and the shipped reading listed both
    `src/error.rs::reallocate_into_boxed_dyn_error_without_backtrace` and
    `::object_reallocate_boxed` — executed only by a SIGSEGV binary — as
    dead while the store already recorded the crashing test entering both.
    """

    dead: frozenset[FnLocator] = field(default_factory=frozenset)
    dead_regions: Mapping[FnLocator, str] = field(default_factory=dict)
    owners: Mapping[str, frozenset[FnSite]] = field(default_factory=dict)
    # Per failing test, the sites that ran ONLY after its premise failed —
    # while the failure was being reported (the panic message's Debug, the
    # harness formatting a returned `Err`) or unwound. Evidence, never
    # ownership: a prompt may quote it as "runs while the failure is
    # reported", and no consumer that turns execution into ownership reads
    # it. There is deliberately no field holding the union of the two, so a
    # new consumer cannot read it. Measured 2026-09-21 on run
    # `450977-5f48b68a`: of the 25 escalated additions for the two downcast
    # tests, 16 ran only after the panic began (the chain iterator, the
    # indenting writer, the pointer accessors) and fills had re-bought them
    # 10–31 times each.
    rendered: Mapping[str, frozenset[FnSite]] = field(default_factory=dict)
    # The red set the owner map was ASKED for, which is not the same as the
    # set it answered: a test with no matching binary is missing from
    # `owners`, and the difference is exactly the ignorance every consumer
    # here refuses on. Carried so a consumer holding only the slice can
    # apply the same positive-evidence rule the ranking applies with
    # `cur_failed` in hand.
    failing: frozenset[str] = frozenset()
    # Labels of the test binaries whose whole-suite process did not
    # complete (killed by a signal, or completed without a profile).
    unmeasured_binaries: frozenset[str] = frozenset()
    # Tests whose own process did not complete: asked red tests (keyed as
    # asked) and the tests of an unmeasured binary that still died when run
    # alone (keyed as the binary lists them). Never in `owners`.
    unmeasured_tests: frozenset[str] = frozenset()
    # ``label::test`` of a binary the bound cut while a test was in flight,
    # or that the caller's own reading already named as hung. Distinct from
    # ``unmeasured_tests`` (a SIGSEGV is unmeasured and not a hang).
    hung_tests: frozenset[str] = frozenset()


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
    owners: Mapping[str, frozenset[FnSite]] | None = None,
    rendered: Mapping[str, frozenset[FnSite]] | None = None,
    failing: Iterable[str] = (),
    unmeasured_binaries: Iterable[str] = (),
    unmeasured_tests: Iterable[str] = (),
    hung_tests: Iterable[str] = (),
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
        grouped.setdefault(locator_of(body.locator), []).append(body)
    # The join rule is shared with augmentation's function-execution
    # criterion (`fn_execution.executed_by_key`): one statement of "a key is
    # unexecuted only when every body under it counted 0".
    executed = executed_by_key(
        (loc, b.count) for loc, group in grouped.items() for b in group)
    dead: set[FnLocator] = set()
    regions: dict[FnLocator, str] = {}
    for loc, group in grouped.items():
        if group and not executed.get(loc, True):
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
                         rendered=dict(rendered or {}),
                         failing=frozenset(failing),
                         unmeasured_binaries=frozenset(unmeasured_binaries),
                         unmeasured_tests=frozenset(unmeasured_tests),
                         hung_tests=frozenset(hung_tests))


def matching_test_key(name: str, keys: Iterable[str]) -> str | None:
    """The key in `keys` that names `name`: exact first, then the unique
    leaf match — the reconciliation `unreached_by_test` applies between the
    sweep's `gen_tests_2::t` form and a bare `t`. None when no key or more
    than one leaf-matches (an aliased miss reads as unmeasured, never as a
    reading)."""
    pool = list(dict.fromkeys(keys))
    if name in pool:
        return name
    leaf = name.rsplit("::", 1)[-1]
    hits = [k for k in pool if k.rsplit("::", 1)[-1] == leaf]
    return hits[0] if len(hits) == 1 else None


def withhold_unmeasured(
    reading: CoverageSlice, truncated: Collection[str] = (),
) -> tuple[CoverageSlice, frozenset[FnLocator]]:
    """The reading with every negative claim withheld that an unmeasured run
    could contradict, plus the dead / region locators withheld.

    A run is unmeasured when it did not reach its end: its process died (the
    adapter's `unmeasured_tests`) or it failed at a panic inside the crate
    (``truncated``, from `test_outcome.truncated_failing` on the sweep's
    capture). Such a run says nothing about the bodies it would have entered
    next, and nothing bounds which bodies those are — the pipeline has no
    call graph — so while any run is unmeasured:

    - `dead` ("executed by no test in the suite") and `dead_regions` are
      withheld whole;
    - the unmeasured tests leave `owners`, so every refusal that reads the
      owner map (`convictors_never_enter`, the red-dead demotion, the
      per-fn pre-loop gate) sees them as absent, which is the owner map's
      own "unmeasured, never a refusal" contract.

    One rule for both sources. It replaces the recorded-executor exemption,
    which rescued only the bodies an earlier run of a crashed test had been
    recorded entering: that record was itself a truncated run, and on run
    `3383938-c5d24499` (2026-09-19) every one of 141 failing tests stopped
    at `construct`'s stub while the reading still called 58 bodies dead —
    `vtable`, `drop`, `downcast` among them — and told the model "whatever is
    wrong is in its caller" about the vtable functions the stub had kept
    every test from reaching. Positive evidence (what a truncated run DID
    enter) is the caller's to keep for admission and ranking; this function
    only withholds what the reading cannot know."""
    unmeasured = frozenset(reading.unmeasured_tests) | frozenset(truncated)
    if not unmeasured:
        return reading, frozenset()
    leaves = {t.rsplit("::", 1)[-1] for t in unmeasured}
    owners = {t: sites for t, sites in reading.owners.items()
              if t not in unmeasured and t.rsplit("::", 1)[-1] not in leaves}
    removed = frozenset(reading.dead) | frozenset(reading.dead_regions)
    return (replace(reading, dead=frozenset(), dead_regions={},
                    owners=owners, unmeasured_tests=unmeasured),
            removed)


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
    owners: Mapping[str, frozenset[FnSite]],
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
        if unit in {locator_of(s) for s in entered}:
            return None
    return frozenset(names)


def red_dead_units(
    units: Iterable[FnLocator],
    failing: Iterable[str],
    owners: Mapping[str, frozenset[FnSite]],
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


def entered_by_test(unit: FnLocator, test: str) -> bool:
    """True only when the bound reading measured ``test``, it entered at
    least one crate body, and ``unit`` is among them. Unbound, unmeasured,
    or an empty owner set is False — not proven entered. The inverse of
    :func:`unreached_by_test`'s positive-unentered case."""
    reading = _bound
    if reading is None:
        return False
    key = matching_test_key(test, reading.owners)
    if key is None:
        return False
    entered = reading.owners.get(key)
    if not entered:
        return False
    return unit in {locator_of(s) for s in entered}


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
            out[t] = tuple(sorted(f"{locator_of(u).file}::{locator_of(u).fn_name}"
                                   for u in entered))
    return out
