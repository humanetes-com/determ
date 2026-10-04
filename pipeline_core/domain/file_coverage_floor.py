"""The per-file line floor of the task0c stop.

The module stop compares ONE crate-wide percent with the target, so a small
file can sit far under it inside a module that passes: reconstruction checks
each file's functions against the tests, and a large well-covered file does
not constrain a small one's unexecuted lines. The floor is read per file.

A file is read over the lines a test can reach. An uncovered line leaves the
denominator only when something already owns a verdict about it:

- it lies inside a function the compiler proved no test can execute
  (`domain.fn_reachability`, the record's `by_construction`);
- the pass's own filter calls it not actionable (an instrumentation
  artifact, or compiled out under the cluster's cfg) — the same set the
  generation pass leaves untargeted, so the floor never holds the stop on a
  line no prompt will ever be aimed at.

Without that, the floor turns proved-unreachable code into a permanent stall:
on anyhow (run `44032-0bf107ac`, 2026-10-04) `src/ptr.rs` read 79/88 = 89.8%
and `src/backtrace.rs` 0/4, every uncovered line in a proved function.

A file under the floor is `accounted`, not `held`, when every reachable line
still open lies in a function whose targeted-attempt budget is spent
(`unreached`), or when the generation pass gave the file up after its stall
limit. The floor never buys a function a second budget.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.fn_execution import FnTarget

FILE_MET = "met"
FILE_ACCOUNTED = "accounted"
FILE_HELD = "held"
FILE_VACUOUS = "vacuous"

_VERDICTS = (FILE_MET, FILE_ACCOUNTED, FILE_HELD, FILE_VACUOUS)


@dataclass(frozen=True, slots=True)
class UncoveredSplit:
    """A file's uncovered lines, split by why each one is or is not a target.

    `actionable` is what a generation prompt is aimed at; every other set is a
    reason a line is not, logged by the pass and left out of the floor's
    denominator."""

    actionable: frozenset[int]
    artifact: frozenset[int] = frozenset()
    cfg_incompatible: frozenset[int] = frozenset()
    cfg_excluded: frozenset[int] = frozenset()
    proved: frozenset[int] = frozenset()

    @property
    def not_actionable(self) -> frozenset[int]:
        return self.artifact | self.cfg_incompatible | self.cfg_excluded | self.proved


def lines_in(targets: Iterable[FnTarget]) -> frozenset[int]:
    return frozenset(ln for t in targets for ln in range(t.first_line, t.last_line + 1))


@dataclass(frozen=True, slots=True)
class FileFloorReading:
    """One file against the floor. `covered`/`coverable` are over reachable
    lines; `raw_*` are the backend's counts, kept so a report shows both."""

    rel: str
    covered: int
    coverable: int
    raw_covered: int
    raw_coverable: int
    verdict: str
    # Why an `accounted` file is not held: the spent functions, or `stalled`.
    reason: str = ""
    open_lines: tuple[int, ...] = ()

    @property
    def pct(self) -> float:
        return 100.0 * self.covered / self.coverable if self.coverable else 100.0

    @property
    def raw_pct(self) -> float:
        return 100.0 * self.raw_covered / self.raw_coverable if self.raw_coverable else 100.0

    def describe(self) -> str:
        base = f"{self.rel} {self.pct:.1f}% ({self.covered}/{self.coverable} reachable"
        if self.raw_coverable != self.coverable:
            base += f"; raw {self.raw_pct:.1f}%"
        base += ")"
        return f"{base} {self.verdict}" + (f": {self.reason}" if self.reason else "")


def read_file(rel: str, *, raw_covered: int, raw_coverable: int,
              uncovered: Iterable[int], not_actionable: Iterable[int],
              spent: Sequence[FnTarget], stalled: bool, floor: float) -> FileFloorReading:
    """One file's verdict.

    Only UNCOVERED lines leave the denominator: a covered line inside a proved
    function is a counterexample to the proof (`task0c.fn_reachability_contradicted`)
    and stays counted rather than hidden."""
    unc = frozenset(uncovered)
    excluded = unc & frozenset(not_actionable)
    open_lines = tuple(sorted(unc - excluded))
    coverable = max(0, raw_coverable - len(excluded))
    covered = min(raw_covered, coverable)

    def reading(verdict: str, reason: str = "") -> FileFloorReading:
        return FileFloorReading(rel, covered, coverable, raw_covered, raw_coverable,
                                verdict, reason, open_lines)

    if coverable == 0:
        return reading(FILE_VACUOUS)
    if 100.0 * covered / coverable >= floor:
        return reading(FILE_MET)
    spent_lines = lines_in(spent)
    if open_lines and all(ln in spent_lines for ln in open_lines):
        owners = sorted({t.label for t in spent
                         if any(t.first_line <= ln <= t.last_line for ln in open_lines)})
        return reading(FILE_ACCOUNTED, "attempt budget spent on " + ", ".join(owners))
    if stalled:
        return reading(FILE_ACCOUNTED, "stalled: the generation pass gave the file up")
    return reading(FILE_HELD)


@dataclass(frozen=True, slots=True)
class FileFloor:
    """Every file of one (cluster, module) reading against the floor."""

    floor: float
    files: tuple[FileFloorReading, ...] = ()

    @property
    def held(self) -> tuple[FileFloorReading, ...]:
        return tuple(f for f in self.files if f.verdict == FILE_HELD)

    @property
    def below(self) -> tuple[FileFloorReading, ...]:
        """Under the floor, held or accounted: what a report names."""
        return tuple(f for f in self.files
                     if f.verdict in (FILE_HELD, FILE_ACCOUNTED))

    @property
    def stalled(self) -> frozenset[str]:
        return frozenset(f.rel for f in self.files
                         if f.verdict == FILE_ACCOUNTED and f.reason.startswith("stalled"))

    def summary(self) -> str:
        if not self.below:
            return f"every file at or above the {self.floor:.0f}% floor on reachable lines"
        return (f"{len(self.below)} file(s) below the {self.floor:.0f}% floor on reachable "
                f"lines ({len(self.held)} held): "
                + "; ".join(f.describe() for f in self.below))

    def to_json(self) -> str:
        return json.dumps({"floor": self.floor, "files": [
            {"file": f.rel, "covered": f.covered, "coverable": f.coverable,
             "raw_covered": f.raw_covered, "raw_coverable": f.raw_coverable,
             "verdict": f.verdict, "reason": f.reason, "open": list(f.open_lines)}
            for f in self.files]}, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> FileFloor | None:
        try:
            raw = json.loads(text)
        except ValueError:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
            return None
        files: list[FileFloorReading] = []
        for f in raw["files"]:
            if not isinstance(f, dict) or f.get("verdict") not in _VERDICTS:
                return None
            files.append(FileFloorReading(
                str(f.get("file", "")), int(f.get("covered", 0)), int(f.get("coverable", 0)),
                int(f.get("raw_covered", 0)), int(f.get("raw_coverable", 0)),
                str(f["verdict"]), str(f.get("reason", "")),
                tuple(int(x) for x in f.get("open", ()))))
        return cls(float(raw.get("floor", 0.0)), tuple(files))

    def event_data(self) -> dict[str, Any]:
        return {"floor": self.floor, "held": [f.rel for f in self.held],
                "files": {f.rel: {"pct": round(f.pct, 1), "raw_pct": round(f.raw_pct, 1),
                                  "verdict": f.verdict, "reason": f.reason}
                          for f in self.files}}


def judge_files(floor: float, files: Iterable[tuple[str, int, int, Iterable[int]]], *,
                not_actionable: Mapping[str, Iterable[int]],
                spent: Sequence[FnTarget], stalled: Iterable[str]) -> FileFloor:
    """The floor over `files` as `(rel, raw_covered, raw_coverable, uncovered)`.
    `spent` are the unreached functions with their spans; a file in `stalled`
    was given up by the generation pass."""
    stalled_set = frozenset(stalled)
    return FileFloor(floor, tuple(
        read_file(rel, raw_covered=cov, raw_coverable=cvbl, uncovered=unc,
                  not_actionable=not_actionable.get(rel, ()),
                  spent=[t for t in spent if t.site.file == rel],
                  stalled=rel in stalled_set, floor=floor)
        for rel, cov, cvbl, unc in sorted(files, key=lambda f: f[0])))
