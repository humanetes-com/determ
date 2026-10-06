"""The per-file line floor through the real task0c entry and generation pass.

The stop is driven through `task0c_augment_module` with the fakes of
`test_task0c_fn_criterion.py` (generation replaced by a recorder, coverage
scripted per measurement over one invented `src/lib.rs`). The generation half
is driven through `_run_phase_c` on the microcrate fixture with the fakes of
`test_task0c_contract.py`.

Asserted is the entry's claim:
- with the module aggregate met, a file under the floor on reachable lines
  keeps the loop open until a measurement lifts it; with the floor off the same
  reading stops at once (the control: the pre-change stop);
- a file the generation pass gave up on is accounted, and stays accounted
  when the cluster re-enters, so the floor never buys it a second budget;
- the lines of a function the compiler proved unreachable leave the
  denominator, so a file whose only gap is such a function does not hold;
- the generation pass no longer aims a prompt at those lines (control: the
  same lines without the proof are aimed at);
- the lines of a function whose targeted-attempt budget is spent stay in the
  denominator and read the file accounted, and the generation pass is handed
  that same set and aims no prompt at them, so a pass left with nothing else
  ends at the ceiling instead of re-prompting until the stall counter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.tasks.task0c import (
    Task0cDeps,
    Task0cFileHistory,
    _PhaseCCtx,
    _PhaseCOutcome,
    _run_phase_c,
)
from pipeline_core.domain.cluster_plan import nightly_rules_for
from pipeline_core.domain.coverage import CoverageMeasurement, PerFileCoverage
from pipeline_core.domain.file_coverage_floor import FILE_ACCOUNTED, FILE_MET, FileFloor
from tests.test_task0c_contract import EXACT, _Harness
from tests.test_task0c_fn_criterion import _Loop, _mir, _reader
from tests.test_task0c_fn_targets import _ADD, _STOCK, _files

# `_SRC` of the fn-criterion harness: `called` is lines 1-3, `never` 5-7.
_COVERABLE = (1, 2, 3, 5, 6, 7)


class _FloorLoop(_Loop):
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                 never_counts: list[int | None], uncovered: list[set[int]],
                 floor: float, stall_file: bool = False) -> None:
        super().__init__(tmp_path, monkeypatch, never_counts=never_counts)
        self.uncovered = list(uncovered)
        self.floor = floor
        self.stall_file = stall_file
        self.floors: dict[tuple[str, str], FileFloor] = {}

    def _measure(self, *a: Any) -> CoverageMeasurement:
        base = super()._measure(*a)
        unc = self.uncovered.pop(0) if len(self.uncovered) > 1 else self.uncovered[0]
        files = [PerFileCoverage(
            abs_path=str(self.ws / "src" / "lib.rs"), path=["src", "lib.rs"],
            traces=[{"line": ln, "stats": {"Line": 0 if ln in unc else 1}} for ln in _COVERABLE],
            covered=len(_COVERABLE) - len(unc), coverable=len(_COVERABLE))]
        return CoverageMeasurement(base[0], files, base[2], base[3], base[4])

    def _phase_c(self, **kw: Any) -> _PhaseCOutcome:
        if self.stall_file:
            kw["hist"].skipped_files.add("src/lib.rs")
        return super()._phase_c(**kw)

    def deps(self) -> Task0cDeps:
        d = super().deps()
        for name, value in {
                "COVERAGE_FILE_FLOOR": self.floor,
                "strip_gen_tests_decls": lambda text: text,
                "load_file_floor": lambda pd, c, m: self.floors.get((c, m)),
                "save_file_floor": lambda pd, c, m, f: self.floors.__setitem__((c, m), f),
        }.items():
            object.__setattr__(d, name, value)
        return d

    @property
    def floor_reading(self) -> FileFloor:
        return self.floors[("stable", "m")]


def test_a_file_under_the_floor_holds_the_met_aggregate_until_it_is_lifted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _FloorLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}, set()],
                      floor=90.0)
    loop.run()
    # 5/6 = 83.3% on reachable lines: one generation pass, then the lift.
    assert loop.measurements == 2 and len(loop.phase_c_targets) == 1
    held = [ln for ln in loop.logs if "below the 90% floor" in ln and "NOT stopping" in ln]
    assert len(held) == 1 and "src/lib.rs 83.3% (5/6 reachable) held" in held[0]
    assert [f["data"]["held"] for e, f in loop.events
            if e == "task0c.file_floor_held"] == [["src/lib.rs"]]
    assert [f.verdict for f in loop.floor_reading.files] == [FILE_MET]
    assert any("95.0% coverage reached" in ln for ln in loop.logs)


def test_negative_control_with_the_floor_off_the_same_reading_stops_at_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _FloorLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}, set()],
                      floor=0.0)
    loop.run()
    assert loop.measurements == 1 and loop.phase_c_targets == []
    assert not [e for e, _ in loop.events if e.startswith("task0c.file_floor")]
    assert loop.floors == {}


def test_a_stalled_file_is_accounted_and_stays_so_on_re_entry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _FloorLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}],
                      floor=90.0, stall_file=True)
    loop.run()
    assert loop.measurements == 2 and len(loop.phase_c_targets) == 1
    (f,) = loop.floor_reading.files
    assert f.verdict == FILE_ACCOUNTED and f.reason.startswith("stalled")
    # The cluster re-enters the module: the given-up file does not hold again.
    again = _FloorLoop(tmp_path / "again", monkeypatch, never_counts=[2], uncovered=[{2}],
                       floor=90.0)
    again.ws, again.floors = loop.ws, loop.floors
    again.run()
    assert again.measurements == 1 and again.phase_c_targets == []


def test_the_lines_of_a_proved_function_leave_the_denominator(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _FloorLoop(tmp_path, monkeypatch, never_counts=[0], uncovered=[{5, 6, 7}],
                      floor=90.0)
    loop.reach = _reader(_mir())
    loop.run()
    # Raw 3/6 = 50%; over reachable lines 3/3. No pass is aimed at anything.
    assert loop.measurements == 1 and loop.phase_c_targets == []
    (f,) = loop.floor_reading.files
    assert (f.verdict, f.covered, f.coverable, f.raw_coverable) == (FILE_MET, 3, 3, 6)


def test_a_spent_function_stays_in_the_denominator_and_reads_accounted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `never` (lines 5-7) is unexecuted at every measurement and nothing proves
    # it unreachable: three targeted passes spend its budget, the fourth
    # measurement records it unreached and the loop stops on the line target.
    loop = _FloorLoop(tmp_path, monkeypatch, never_counts=[0], uncovered=[{5, 6, 7}],
                      floor=90.0)
    loop.run()
    assert loop.phase_c_targets == [["never"], ["never"], ["never"]]
    (f,) = loop.floor_reading.files
    # Reachable by a test in principle, so still counted: 3/6, not 3/3 met.
    assert (f.verdict, f.covered, f.coverable) == (FILE_ACCOUNTED, 3, 6)
    assert f.reason == "attempt budget spent on src/lib.rs::never"


class _SpentLoop(_Loop):
    """Line target out of reach, so the loop keeps generating after the budget
    of `never` is spent; records what each generation pass is handed."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(tmp_path, monkeypatch, never_counts=[0])
        self.phase_c_spent: list[list[str]] = []

    def _phase_c(self, **kw: Any) -> _PhaseCOutcome:
        spent = [t.site.fn_name for t in kw.get("spent", ())]
        self.phase_c_spent.append(spent)
        if spent:
            return _PhaseCOutcome("break", at_ceiling=True)
        return super()._phase_c(**kw)

    def deps(self) -> Task0cDeps:
        d = super().deps()
        object.__setattr__(d, "COVERAGE_TARGET", 100.0)
        return d


def test_the_generation_pass_is_handed_the_spent_functions_of_the_stop(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _SpentLoop(tmp_path, monkeypatch)
    loop.run()
    # Three targeted passes, then the pass after the measurement that records
    # `never` unreached is handed it as spent and no longer as a target.
    assert loop.phase_c_targets == [["never"], ["never"], ["never"]]
    assert loop.phase_c_spent == [[], [], [], ["never"]]
    assert [u.site.fn_name for u in loop.record.unreached] == ["never"]


def _phase_c(h: _Harness, lines: dict[str, list[int]], *, proved: tuple[Any, ...],
             spent: tuple[Any, ...] = ()) -> _PhaseCOutcome:
    object.__setattr__(h.deps, "nightly_rules_for_cluster", lambda: nightly_rules_for(_STOCK))
    return _run_phase_c(
        project_dir=h.ws, module_name="microcrate", module_dir=h.ws,
        project_context="", coverage_strategy="", attempt=4, coverage=95.0,
        covered_lines=3, files_data=_files(h, lines), hist=Task0cFileHistory(),
        ctx=_PhaseCCtx(deps=h.deps, current_cluster=_STOCK, cluster_label=_STOCK.name,
                       split_n=1, allow_line="", prelude_block="", prelude_names="",
                       file_stall_limit=5, retro_deep_at_stall=1, stall_context=False),
        proved=proved, spent=spent)


def test_the_generation_pass_is_not_aimed_at_proved_lines(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # microcrate/src/lib.rs `add` spans 27-29; line 28 is its body.
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    _phase_c(h, {"src/lib.rs": [28]}, proved=(_ADD,))
    assert h.prompts == []
    assert any("filtered 1 line(s) in functions proved unreachable from tests/: [28]" in ln
               for ln in h.logs)

    control = tmp_path / "control"
    control.mkdir()
    c = _Harness(control, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    _phase_c(c, {"src/lib.rs": [28]}, proved=())
    assert c.prompts and "Target file: src/lib.rs" in c.prompts[0]


def test_the_generation_pass_is_not_aimed_at_lines_of_a_spent_function(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The same line 28 of `add`, now with its targeted-attempt budget spent:
    # the stop reads it accounted, so no prompt is aimed at it and the pass,
    # with nothing left to aim at, ends at the ceiling instead of re-prompting
    # until the stall counter. The control is the unproved, unspent run above.
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0, specs=())
    outcome = _phase_c(h, {"src/lib.rs": [28]}, proved=(), spent=(_ADD,))
    assert h.prompts == [] and h.parallel_prompts == []
    assert any("filtered 1 line(s) in functions whose targeted-attempt budget is spent: [28]"
               in ln for ln in h.logs)
    assert (outcome.kind, outcome.at_ceiling) == ("break", True)
