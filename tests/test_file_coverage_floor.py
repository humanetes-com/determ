"""The per-file line floor of the task0c stop (`domain.file_coverage_floor`).

The fixture is the last coverage reading of anyhow run `44032-0bf107ac`
(2026-10-04 11:42 UTC, total 805/826 = 97.5%), with the uncovered lines from
its lcov and the function sites from its `stable__anyhow.fns.json`:

- `src/ptr.rs` 79/88: uncovered 23-25, 78-80, 139-141, the three `Clone::clone`
  bodies the compiler proved unreachable;
- `src/backtrace.rs` 0/4: uncovered 45-48, `_assert_send_sync`, proved;
- `src/wrapper.rs` 21/24: uncovered 40-42, `Debug for DisplayError<M>`,
  unreached after a spent budget of three attempts;
- `src/fmt.rs` 90/93, `src/lib.rs` 19/20, `src/ensure.rs` 55/56: gaps inside
  executed functions.

Asserted is the entry's claim: over reachable lines, at a floor of 90, no file
is held, and wrapper.rs is named below the floor as accounted. The negative
control drops the proved exclusion and gets ptr.rs and backtrace.rs held, the
permanent stall the reachable denominator exists to prevent.
"""
from __future__ import annotations

from pipeline_core.domain.file_coverage_floor import (
    FILE_ACCOUNTED,
    FILE_HELD,
    FILE_MET,
    FILE_VACUOUS,
    FileFloor,
    judge_files,
    lines_in,
    read_file,
)
from pipeline_core.domain.fn_execution import FnTarget
from pipeline_core.domain.models import FnSite


def _t(file: str, fn: str, container: str, first: int, last: int) -> FnTarget:
    return FnTarget(FnSite(file, fn, container), first, last, False)


_PROVED = (
    _t("src/ptr.rs", "clone", "impl Clone for Own<T>", 23, 25),
    _t("src/ptr.rs", "clone", "impl Clone for Ref<'a,T>", 78, 80),
    _t("src/ptr.rs", "clone", "impl Clone for Mut<'a,T>", 139, 141),
    _t("src/backtrace.rs", "_assert_send_sync", "", 45, 48),
)
_SPENT = (_t("src/wrapper.rs", "fmt", "impl Debug for DisplayError<M>", 40, 42),)

_FILES = (
    ("src/backtrace.rs", 0, 4, [45, 46, 47, 48]),
    ("src/ensure.rs", 55, 56, [99]),
    ("src/fmt.rs", 90, 93, [53, 54, 55, 94]),
    ("src/lib.rs", 19, 20, [690]),
    ("src/ptr.rs", 79, 88, [23, 24, 25, 78, 79, 80, 139, 140, 141]),
    ("src/wrapper.rs", 21, 24, [40, 41, 42]),
)


def _proved_by_file() -> dict[str, frozenset[int]]:
    return {rel: lines_in(t for t in _PROVED if t.site.file == rel)
            for rel in {t.site.file for t in _PROVED}}


def _judge(not_actionable: dict[str, frozenset[int]], floor: float = 90.0) -> FileFloor:
    return judge_files(floor, _FILES, not_actionable=not_actionable,
                       spent=_SPENT, stalled=())


def test_the_live_reading_holds_no_file_and_names_wrapper_below_the_floor() -> None:
    floor = _judge(_proved_by_file())
    by = {f.rel: f for f in floor.files}
    assert (by["src/ptr.rs"].verdict, by["src/ptr.rs"].covered,
            by["src/ptr.rs"].coverable) == (FILE_MET, 79, 79)
    assert by["src/backtrace.rs"].verdict == FILE_VACUOUS
    assert by["src/wrapper.rs"].verdict == FILE_ACCOUNTED
    assert round(by["src/wrapper.rs"].pct, 1) == 87.5
    assert "DisplayError" in by["src/wrapper.rs"].reason
    assert {by[r].verdict for r in ("src/fmt.rs", "src/lib.rs", "src/ensure.rs")} == {FILE_MET}
    assert floor.held == ()
    assert [f.rel for f in floor.below] == ["src/wrapper.rs"]
    assert "src/wrapper.rs 87.5% (21/24 reachable) accounted" in floor.summary()


def test_negative_control_raw_denominators_hold_the_proved_files_forever() -> None:
    floor = _judge({})
    assert {f.rel for f in floor.held} == {"src/ptr.rs", "src/backtrace.rs"}


def test_a_gap_inside_an_executed_function_is_held_until_the_file_stalls() -> None:
    held = read_file("src/x.rs", raw_covered=17, raw_coverable=20, uncovered=[4, 5, 6],
                     not_actionable=(), spent=(), stalled=False, floor=90.0)
    assert held.verdict == FILE_HELD and held.open_lines == (4, 5, 6)
    stalled = read_file("src/x.rs", raw_covered=17, raw_coverable=20, uncovered=[4, 5, 6],
                        not_actionable=(), spent=(), stalled=True, floor=90.0)
    assert stalled.verdict == FILE_ACCOUNTED and stalled.reason.startswith("stalled")
    assert FileFloor(90.0, (stalled,)).stalled == frozenset({"src/x.rs"})


def test_a_spent_function_accounts_for_its_own_lines_only() -> None:
    spent = (_t("src/x.rs", "f", "", 10, 12),)
    only_spent = read_file("src/x.rs", raw_covered=17, raw_coverable=20,
                           uncovered=[10, 11, 12], not_actionable=(), spent=spent,
                           stalled=False, floor=90.0)
    assert only_spent.verdict == FILE_ACCOUNTED
    # One open line outside the spent span: the floor still has something to ask.
    mixed = read_file("src/x.rs", raw_covered=16, raw_coverable=20,
                      uncovered=[3, 10, 11, 12], not_actionable=(), spent=spent,
                      stalled=False, floor=90.0)
    assert mixed.verdict == FILE_HELD


def test_a_covered_line_in_a_proved_span_stays_counted() -> None:
    # Line 24 executed although proved: a counterexample, never hidden.
    r = read_file("src/p.rs", raw_covered=10, raw_coverable=12, uncovered=[23, 25],
                  not_actionable=frozenset({23, 24, 25}), spent=(), stalled=False,
                  floor=90.0)
    assert (r.covered, r.coverable, r.verdict) == (10, 10, FILE_MET)


def test_the_reading_round_trips_and_a_foreign_shape_reads_as_none() -> None:
    floor = _judge(_proved_by_file())
    assert FileFloor.from_json(floor.to_json()) == floor
    assert FileFloor.from_json('{"files": [{"verdict": "maybe"}]}') is None
    assert FileFloor.from_json("not json") is None


# --- the ceiling's split ---------------------------------------------------------

def test_the_ceiling_split_counts_each_kind_and_names_the_reachable_functions() -> None:
    from pipeline_core.domain.file_coverage_floor import UncoveredSplit, bucket_counts
    splits = {
        "src/backtrace.rs": UncoveredSplit(actionable=frozenset(), proved=frozenset({45, 46, 47, 48})),
        "src/ptr.rs": UncoveredSplit(actionable=frozenset(),
                                     proved=frozenset({23, 24, 25, 78, 79, 80, 139, 140, 141})),
        "src/ensure.rs": UncoveredSplit(actionable=frozenset(), artifact=frozenset({99})),
        "src/wrapper.rs": UncoveredSplit(actionable=frozenset(), spent=frozenset({40, 41, 42})),
        # lib.rs 690 lies inside an executed function: actionable, in no bucket.
        "src/lib.rs": UncoveredSplit(actionable=frozenset({690})),
    }
    floor = judge_files(90.0, _FILES, not_actionable=_proved_by_file(), spent=_SPENT, stalled=(),
                        buckets={k: bucket_counts(v) for k, v in splits.items()})
    assert floor.buckets() == {"proved": 13, "artifact": 1, "cfg": 0, "spent": 3}
    assert floor.spent_owners() == ("src/wrapper.rs::fmt [impl Debug for DisplayError<M>]",)
    assert floor.ceiling_split() == ("proved unreachable 13, LLVM artifact 1, cfg-filtered 0; "
                                     "attempt budget spent 3 line(s) in 1 reachable function(s)")
    assert FileFloor.from_json(floor.to_json()) == floor


def test_negative_control_a_reading_without_splits_reports_zero_buckets() -> None:
    floor = _judge(_proved_by_file())
    assert floor.buckets() == {"proved": 0, "artifact": 0, "cfg": 0, "spent": 0}
    # The spent function is still named: it owns an uncovered line of wrapper.rs.
    assert floor.spent_owners() == ("src/wrapper.rs::fmt [impl Debug for DisplayError<M>]",)
