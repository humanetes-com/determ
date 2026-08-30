"""The current sweep's coverage reading, and the instruction it licenses.

A body no test executes cannot move a verdict, and a branch no test reaches
cannot be the fill that was missing. The ranking consumes the dead set
(`dispatch_batch.promote_by_service`); the prompt consumes the sentences
below, on the instruction slot (`_plan_line` / the per-fn opener), not as
another evidence section — a fill can carry the evidence and still wrap.

Bound by the TDD driver for one sweep (the instrumented copy lives in a
scratch target dir; this module holds only the names). Unbound, every
helper returns empty and the loop behaves as it did before the reading
existed. Coverage never reaches `convicting`. The process-global bind is
cleared when `run_phase_c_tdd` returns, including on SystemExit, so a later
phase cannot read a stale slice.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pipeline_core.domain.models import FnLocator

DEAD_BODY = (
    "This body is executed by no test in the suite; whatever is wrong is "
    "in its caller."
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
    """

    dead: frozenset[FnLocator] = field(default_factory=frozenset)
    dead_regions: Mapping[FnLocator, str] = field(default_factory=dict)


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
    return CoverageSlice(dead=frozenset(dead), dead_regions=regions)


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
