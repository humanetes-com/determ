"""The task0c stop on an accounted floor, the given-up files across entries,
and shared given-up lines across worlds.

Fixtures are the per-file floor readings of anyhow run `61686-7295ec75`
(2026-10-10, `--restart-from-pre-augment`), copied from
`tmp/anyhow/.cluster_augmented/{stable,nightly}__anyhow.files.json`:

- stable, final (784/807): 13 proved lines (backtrace.rs 4, ptr.rs 9), 5
  artifact lines, 3 spent in `src/error.rs::no_backtrace` (808-810), and two
  open lines in files the generation pass gave up on, `src/fmt.rs:54` and
  `src/lib.rs:690`; `0 held`. The run re-entered stable twice from this state
  (2h05m, 48 model calls, no line).
- nightly, attempt 1 (805/833): open lines context.rs 147-148, error.rs
  668-670, wrapper.rs 81-82, plus fmt.rs 54 and lib.rs 690 (exactly stable's
  given-up lines).

Asserted is the plan's claim (entry 312, "Reading 2026-10-10" and its
addendum):
- an entry whose floor (read from the measurement it returned) holds no file
  and whose function record has nothing pending ends the module `accounted`
  after that one entry, naming the given-up open lines; controls: a held file,
  the floor off, a floor not read from the returned measurement;
- a re-entry does not re-offer a file an earlier entry gave up on, unless it
  holds a pending function or a measurement opens a new line in it;
- a shared file whose every open line another world gave up on is aimed once
  in this world and then given up; a file with any other open line keeps its
  whole budget.
"""
from __future__ import annotations

import dataclasses
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
from pipeline_core.domain.file_coverage_floor import (
    FILE_ACCOUNTED,
    FILE_HELD,
    FileFloor,
    FileFloorReading,
    carried_stall_attempts,
    reopened,
)
from pipeline_core.domain.fn_execution import FnCoverageRecord
from tests.test_cluster_augment import FakeAdapter, _augmentor, _cluster, _ctx
from tests.test_task0c_contract import EXACT, _Harness
from tests.test_task0c_file_floor import _FloorLoop
from tests.test_task0c_fn_targets import _ADD, _STOCK, _files

_FIXTURES = Path(__file__).parent / "fixtures" / "task0c_floor"


def _live(cluster: str) -> FileFloor:
    floor = FileFloor.from_json(
        (_FIXTURES / f"{cluster}_anyhow_61686-7295ec75.files.json").read_text(encoding="utf-8"))
    assert floor is not None
    return floor


# --- the domain rules on the live readings -------------------------------

def test_the_live_stable_reading_names_its_given_up_open_lines() -> None:
    stable = _live("stable")
    assert stable.held == ()
    assert stable.stalled == {"src/fmt.rs", "src/lib.rs"}
    assert stable.stalled_open() == ("src/fmt.rs:54", "src/lib.rs:690")
    # The field is new: a reading written before it is not known to be fresh.
    assert stable.measured_covered == -1
    again = FileFloor.from_json(dataclasses.replace(stable, measured_covered=784).to_json())
    assert again is not None and again.measured_covered == 784


def test_nightly_carries_only_the_files_whose_open_lines_stable_gave_up_on() -> None:
    carried = carried_stall_attempts([_live("stable")], _live("nightly"), budget=5)
    # context.rs 147-148, error.rs 668-670 and wrapper.rs 81-82 are the lines
    # of nightly's cluster-gated functions: stable compiles them out, so they
    # are never its open lines, and those files keep their whole budget.
    assert carried == {"src/fmt.rs": 4, "src/lib.rs": 4}
    # Control: no other world, nothing carried.
    assert carried_stall_attempts([], _live("nightly"), budget=5) == {}


def test_a_file_with_one_open_line_the_other_world_did_not_give_up_on_is_not_carried() -> None:
    nightly = _live("nightly")
    widened = dataclasses.replace(nightly, files=tuple(
        dataclasses.replace(f, open_lines=(54, 61)) if f.rel == "src/fmt.rs" else f
        for f in nightly.files))
    assert carried_stall_attempts([_live("stable")], widened, budget=5) == {"src/lib.rs": 4}


def test_a_new_open_line_reopens_a_given_up_file_and_the_same_lines_do_not() -> None:
    prior = {"src/fmt.rs": [54], "src/lib.rs": [690]}
    stable = _live("stable")
    assert reopened(prior, stable) == frozenset()
    widened = dataclasses.replace(stable, files=tuple(
        dataclasses.replace(f, open_lines=(54, 61)) if f.rel == "src/fmt.rs" else f
        for f in stable.files))
    assert reopened(prior, widened) == {"src/fmt.rs"}


# --- the module stop --------------------------------------------------------

def _measured() -> FnCoverageRecord:
    return FnCoverageRecord(measured=True, candidates=117)


def _stable_fake(floor: FileFloor) -> FakeAdapter:
    fake = FakeAdapter(augment_script={("stable", "anyhow"): [(97.1, 784, False, "")]})
    fake.floor_scripts[("stable", "anyhow")] = [floor]
    fake.fn_scripts[("stable", "anyhow")] = [_measured()]
    return fake


def test_an_accounted_floor_ends_the_module_after_one_entry() -> None:
    fake = _stable_fake(dataclasses.replace(_live("stable"), measured_covered=784))
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], file_floor_pct=100.0,
                              pass_stall_limit=2))
    assert fake.events.count("task.done") == 1
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "accounted"
    (line,) = [m for m in fake.logs if "ACCOUNTED [stable]" in m]
    assert "stalled open: src/fmt.rs:54, src/lib.rs:690" in line
    assert "proved unreachable 13, LLVM artifact 5" in line
    assert any("WARNING [stable]" in m and "src/error.rs::no_backtrace" in m for m in fake.logs)
    assert not any("HARD RULE" in m for m in fake.logs)


def test_control_a_held_file_still_re_enters_until_the_stall_limit() -> None:
    live = _live("stable")
    held = dataclasses.replace(live, measured_covered=784, files=tuple(
        dataclasses.replace(f, verdict=FILE_HELD, reason="") if f.rel == "src/lib.rs" else f
        for f in live.files))
    fake = _stable_fake(held)
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], file_floor_pct=100.0,
                              pass_stall_limit=2))
    assert fake.events.count("task.done") == 3
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "hard_block"


def test_control_the_floor_off_keeps_the_raw_rule() -> None:
    fake = _stable_fake(dataclasses.replace(_live("stable"), measured_covered=784))
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], pass_stall_limit=2))
    assert fake.events.count("task.done") == 3
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "hard_block"


def test_control_a_floor_not_read_from_the_returned_measurement_does_not_account() -> None:
    # The reading on disk is from an earlier measurement (783 covered, or a
    # reading written before the field): an entry that never measured must
    # not end on it.
    for stale in (783, -1):
        fake = _stable_fake(dataclasses.replace(_live("stable"), measured_covered=stale))
        _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"],
                                  file_floor_pct=100.0, pass_stall_limit=2))
        assert fake.cluster_sentinels[("stable", "anyhow")].reason == "hard_block"


def test_control_an_unmeasured_function_record_does_not_account() -> None:
    fake = _stable_fake(dataclasses.replace(_live("stable"), measured_covered=784))
    fake.fn_scripts[("stable", "anyhow")] = [FnCoverageRecord(measured=False)]
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], file_floor_pct=100.0,
                              pass_stall_limit=2))
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "hard_block"


def test_a_blocked_pass_leaves_an_accounted_status_in_place() -> None:
    fake = _stable_fake(dataclasses.replace(_live("stable"), measured_covered=784))
    fake._script[("stable", "anyhow")] = [(97.1, 784, False, "harness broken")]
    _augmentor(fake).run(_ctx(fake, [_cluster("stable")], ["anyhow"], file_floor_pct=100.0))
    assert fake.cluster_sentinels[("stable", "anyhow")].reason == "accounted"


# --- re-entry: the given-up files stay given up --------------------------

def _given_up(open_lines: tuple[int, ...]) -> FileFloor:
    return FileFloor(90.0, (FileFloorReading(
        "src/lib.rs", 5, 6, 5, 6, FILE_ACCOUNTED,
        "stalled: the generation pass gave the file up", open_lines),))


class _ReentryLoop(_FloorLoop):
    """Line target out of reach, so the entry reaches the generation pass;
    records the skipped set and the carry each pass is handed."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                 others: list[FileFloor] | None = None, **kw: Any) -> None:
        super().__init__(tmp_path, monkeypatch, floor=90.0, **kw)
        self.others = list(others or [])
        self.skipped_seen: list[set[str]] = []
        self.carried_seen: list[dict[str, int]] = []

    def _phase_c(self, **kw: Any) -> _PhaseCOutcome:
        self.skipped_seen.append(set(kw["hist"].skipped_files))
        self.carried_seen.append(dict(kw["hist"].file_carried))
        return super()._phase_c(**kw)

    def deps(self) -> Task0cDeps:
        d = super().deps()
        object.__setattr__(d, "COVERAGE_TARGET", 100.0)
        object.__setattr__(d, "other_file_floors", lambda pd, c, m: self.others)
        return d


def test_a_re_entry_does_not_re_offer_a_given_up_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_MAX_STALLS", "1")
    loop = _ReentryLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}])
    loop.floors[("stable", "m")] = _given_up((2,))
    loop.run()
    assert loop.skipped_seen and loop.skipped_seen[0] == {"src/lib.rs"}
    (f,) = loop.floor_reading.files
    assert f.verdict == FILE_ACCOUNTED and loop.floor_reading.measured_covered == 19


def test_control_a_given_up_file_holding_a_pending_function_is_re_offered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `never` (src/lib.rs 5-7) is still inside its attempt budget on the
    # record the entry loads: the file gave up first (an empty reply stalls the
    # file without charging the function), and seeding it would hold the
    # function out until the outer stall hard-blocks it.
    from pipeline_core.domain.fn_execution import FnTarget, PendingFn
    from pipeline_core.domain.models import FnSite
    monkeypatch.setenv("TASK0C_MAX_STALLS", "1")
    loop = _ReentryLoop(tmp_path, monkeypatch, never_counts=[0], uncovered=[{2}])
    loop.floors[("stable", "m")] = _given_up((2,))
    loop.store[("stable", "m")] = FnCoverageRecord(measured=True, candidates=2, pending=(
        PendingFn(FnTarget(FnSite("src/lib.rs", "never", ""), 5, 7, False)),))
    loop.run()
    assert loop.skipped_seen and loop.skipped_seen[0] == set()


def test_control_a_new_open_line_re_offers_the_file_and_holds_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_MAX_STALLS", "1")
    loop = _ReentryLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2, 6}])
    loop.floors[("stable", "m")] = _given_up((2,))
    loop.run()
    assert loop.skipped_seen and loop.skipped_seen[0] == set()
    assert any("src/lib.rs: a line opened" in ln for ln in loop.logs)
    (f,) = loop.floor_reading.files
    assert (f.verdict, f.open_lines) == (FILE_HELD, (2, 6))


def test_control_no_prior_floor_offers_the_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_MAX_STALLS", "1")
    loop = _ReentryLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}])
    loop.run()
    assert loop.skipped_seen and loop.skipped_seen[0] == set()


def test_the_entry_hands_the_pass_the_carry_from_another_world(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_MAX_STALLS", "1")
    loop = _ReentryLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}],
                        others=[_given_up((2,))])
    loop.run()
    assert loop.carried_seen[0] == {"src/lib.rs": 4}
    # Control: the other world gave up on a different line.
    other = _ReentryLoop(tmp_path / "o", monkeypatch, never_counts=[2], uncovered=[{2}],
                         others=[_given_up((3,))])
    other.run()
    assert other.carried_seen[0] == {}


# --- the generation pass: one own aim, then given up ---------------------

def _pass(h: _Harness, hist: Task0cFileHistory, fn_targets: tuple[Any, ...] = ()) -> _PhaseCOutcome:
    object.__setattr__(h.deps, "nightly_rules_for_cluster", lambda: nightly_rules_for(_STOCK))
    return _run_phase_c(
        project_dir=h.ws, module_name="microcrate", module_dir=h.ws,
        project_context="", coverage_strategy="", attempt=1, coverage=95.0,
        covered_lines=3, files_data=_files(h, {"src/lib.rs": [28]}), hist=hist,
        ctx=_PhaseCCtx(deps=h.deps, current_cluster=_STOCK, cluster_label=_STOCK.name,
                       split_n=1, allow_line="", prelude_block="", prelude_names="",
                       file_stall_limit=5, retro_deep_at_stall=1, stall_context=False),
        proved=(), spent=(), fn_targets=fn_targets)


def test_a_carried_file_is_aimed_once_then_given_up(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT, EXACT],
                 target=0.0, specs=())
    hist = Task0cFileHistory()
    hist.file_carried = {"src/lib.rs": 4}
    _pass(h, hist)
    aimed = len(h.prompts) + len(h.parallel_prompts)
    assert aimed >= 1 and hist.file_aims == {"src/lib.rs": 1}
    assert "src/lib.rs" in hist.skipped_files
    _pass(h, hist)
    assert len(h.prompts) + len(h.parallel_prompts) == aimed


def test_control_an_uncarried_file_keeps_its_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT, EXACT],
                 target=0.0, specs=())
    hist = Task0cFileHistory()
    _pass(h, hist)
    assert hist.file_aims == {"src/lib.rs": 1}
    assert "src/lib.rs" not in hist.skipped_files


class _GiveUpAndReturnLoop(_ReentryLoop):
    """The pass gives src/lib.rs up and returns (every file stalled), so the
    entry ends on the measurement whose floor was read before the give-up."""

    def _phase_c(self, **kw: Any) -> _PhaseCOutcome:
        self.phase_c_targets.append([])
        kw["hist"].skipped_files.add("src/lib.rs")
        return _PhaseCOutcome("return", result=(95.0, 19, False, ""))


def test_a_file_given_up_after_the_floor_was_read_reads_accounted_at_the_return(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _GiveUpAndReturnLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}])
    loop.run()
    (f,) = loop.floor_reading.files
    assert f.verdict == FILE_ACCOUNTED and f.reason.startswith("stalled")
    assert loop.floor_reading.measured_covered == 19


def test_control_a_carried_file_holding_a_pending_function_is_not_given_up(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `add` (lines 27-29) is unexecuted and inside its budget: an empty reply
    # would count as the aim without charging it, so the carry must not
    # latch the file while it holds the function.
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT, EXACT],
                 target=0.0, specs=())
    hist = Task0cFileHistory()
    hist.file_carried = {"src/lib.rs": 4}
    _pass(h, hist, fn_targets=(_ADD,))
    assert hist.file_aims == {"src/lib.rs": 1}
    assert "src/lib.rs" not in hist.skipped_files and not hist.carry_given_up


class _CarryLoop(_ReentryLoop):
    """The pass gives the file up on its carry as the real pass does at the
    aim, and returns `continue`; every floor write is recorded."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.saved: list[FileFloor] = []
        self.saved_after: list[int] = []   # measurements taken at each write

    def _phase_c(self, **kw: Any) -> _PhaseCOutcome:
        hist = kw["hist"]
        self.skipped_seen.append(set(hist.skipped_files))
        self.carried_seen.append(dict(hist.file_carried))
        self.phase_c_targets.append([])
        if hist.file_carried.get("src/lib.rs") and "src/lib.rs" not in hist.skipped_files:
            hist.skipped_files.add("src/lib.rs")
            hist.carry_given_up.add("src/lib.rs")
        return _PhaseCOutcome("continue")

    def deps(self) -> Task0cDeps:
        d = super().deps()

        def _save(pd: Path, c: str, m: str, f: FileFloor) -> None:
            self.saved.append(f)
            self.saved_after.append(self.measurements)
            self.floors[(c, m)] = f
        object.__setattr__(d, "save_file_floor", _save)
        return d


def test_a_carry_give_up_is_written_before_the_next_measurement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_MAX_STALLS", "1")
    loop = _CarryLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}],
                      others=[_given_up((2, 3))])
    loop.run()
    # Saves: measurement 1 (held), right after the pass (given up), then
    # measurement 2 (given up). The middle one is what a stop between the two
    # measurements would leave on disk.
    assert [(n, s.files[0].verdict) for n, s in zip(loop.saved_after, loop.saved, strict=True)][:3] \
        == [(1, FILE_HELD), (1, FILE_ACCOUNTED), (2, FILE_ACCOUNTED)]
    assert loop.saved[1].measured_covered == loop.saved[0].measured_covered
    # The next entry of this cluster does not re-offer it.
    again = _CarryLoop(tmp_path / "again", monkeypatch, never_counts=[2], uncovered=[{2}],
                       others=[_given_up((2, 3))])
    again.ws, again.floors = loop.ws, loop.floors
    again.run()
    assert again.skipped_seen[0] == {"src/lib.rs"}


def test_a_carry_give_up_holds_when_an_aim_covers_another_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_MAX_STALLS", "2")
    loop = _CarryLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2, 3}, {2}],
                      others=[_given_up((2, 3))])
    loop.run()
    (f,) = loop.floor_reading.files
    assert (f.verdict, f.open_lines) == (FILE_ACCOUNTED, (2,))


def test_control_a_line_no_other_world_gave_up_on_re_offers_a_carried_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_MAX_STALLS", "2")
    loop = _CarryLoop(tmp_path, monkeypatch, never_counts=[2], uncovered=[{2}, {2, 6}],
                      others=[_given_up((2,))])
    loop.run()
    assert any("src/lib.rs: a line opened that no other cluster gave up on" in ln
               for ln in loop.logs)
    (f,) = loop.floor_reading.files
    assert (f.verdict, f.open_lines) == (FILE_HELD, (2, 6))
