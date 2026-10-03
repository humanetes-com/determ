"""The augmentation stop under the function-execution criterion.

Driven through `task0c_augment_module` with a fakes-built `Task0cDeps` (the
pattern of `test_task0c_fix_loops.py`); the generation pass `_run_phase_c` is
replaced by a recorder so the loop's own stop is what is under test. The
coverage reading is scripted: line coverage already above the target, one
function the crate compiles with its function record at 0 — the shape run
`330242-0be2c3e1` stopped on at 95.2 % (2026-09-03) and run `301146-2d8a5f06`
paid for as exit 1 (2026-09-17). Invented crate, no live names.

Asserted is the entry's claim: the line target does not stop the loop while a
function is unexecuted inside its attempt budget; it stops once a measurement
counts the function executed, or once the budget of targeted attempts is spent
with the count still 0 — recorded unreached with the attempts; an unmeasured
reading stops on lines alone and says so. The negative control injects the
pre-fix stop (every record accounted) and gets the break at the first
measurement with the function still unexecuted.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pipeline_core.application.tasks.task0c as task0c_mod
from pipeline_core.application.tasks.task0c import Task0cDeps, _PhaseCOutcome, task0c_augment_module
from pipeline_core.domain.coverage import CoverageMeasurement
from pipeline_core.domain.fn_execution import FnCoverageRecord
from pipeline_core.domain.fn_reachability import WorldReading

_SRC = """\
pub fn called() -> u32 {
    1
}

pub fn never() -> u32 {
    2
}
"""


def _record(name: str) -> dict[str, Any]:
    start = _SRC.index(f"pub fn {name}(")
    end = _SRC.index("}", start)
    return {"file": "src/lib.rs", "fn_name": name, "sig_byte_start": start,
            "body_byte_start": _SRC.index("{", start) + 1, "body_byte_end": end,
            "body_text": _SRC[_SRC.index("{", start) + 1:end]}


class _Loop:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                 never_counts: list[int | None], budget: int = 3) -> None:
        self.ws = tmp_path / "ws"
        (self.ws / "src").mkdir(parents=True)
        (self.ws / "src" / "lib.rs").write_text(_SRC, encoding="utf-8")
        (self.ws / "Cargo.toml").write_text('[package]\nname = "m"\n', encoding="utf-8")
        self.never_counts = list(never_counts)
        self.measurements = 0
        self.phase_c_targets: list[list[str]] = []
        self.logs: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.store: dict[tuple[str, str], FnCoverageRecord] = {}
        self.reach: Any = None
        monkeypatch.setenv("TASK0C_FN_ATTEMPTS", str(budget))
        monkeypatch.setattr(task0c_mod, "log", self.logs.append)
        monkeypatch.setattr(task0c_mod, "emit",
                            lambda event, **f: self.events.append((event, f)))
        monkeypatch.setattr(task0c_mod, "_run_phase_c", self._phase_c)

    def _measure(self, *_a: Any) -> CoverageMeasurement:
        self.measurements += 1
        count = self.never_counts.pop(0) if len(self.never_counts) > 1 else self.never_counts[0]
        functions = None if count is None else {("src/lib.rs", 1): 5, ("src/lib.rs", 5): count}
        return CoverageMeasurement(95.0, [], 19, 20, functions)

    def _phase_c(self, **kw: Any) -> _PhaseCOutcome:
        targets = kw.get("fn_targets", ())
        self.phase_c_targets.append([t.site.fn_name for t in targets])
        # A block for every target compiled and ran.
        return _PhaseCOutcome("continue", fn_attempted=frozenset(t.site for t in targets))

    def deps(self) -> Task0cDeps:
        stub: dict[str, Any] = {f: (lambda *a, **k: None) for f in Task0cDeps.__dataclass_fields__}
        stub.update(
            BUILD_TIMEOUT=30, COVERAGE_TARGET=90.0, TASK0C_MODEL="m",
            get_current_cluster=lambda: None,
            read_state=lambda pd: {"phases": {}},
            derive_gentest_prelude=lambda pd: ("", ""),
            prelude_import_names=lambda b: "",
            generate_fix_loop=lambda **k: (True, ""),
            is_test_file=lambda p, root: False,
            is_gen_tests_file=lambda name: False,
            check_stop=lambda ctx: None,
            test_augmentor_factory=lambda: SimpleNamespace(
                is_applicable=lambda ct: True,
                lib_compiles=lambda p, m: (True, ""),
                test_harness_compiles=lambda p, m: (True, ""),
                measure_coverage=self._measure),
            enumerate_fns=lambda d: [_record("called"), _record("never")],
            enumerate_mods=lambda d: [],
            measure_reachability=(self.reach if self.reach is not None
                                  else (lambda *a, **k: None)),
            load_fn_record=lambda pd, c, m: self.store.get((c, m)),
            save_fn_record=lambda pd, c, m, rec: self.store.__setitem__((c, m), rec),
        )
        return Task0cDeps(**stub)

    def run(self) -> tuple[float, int, bool, str]:
        return task0c_augment_module(self.ws, "m", self.ws, self.deps(), crate_type="lib")

    @property
    def record(self) -> FnCoverageRecord:
        return self.store[("stable", "m")]


def test_the_line_target_does_not_stop_while_a_function_is_pending(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop(tmp_path, monkeypatch, never_counts=[0])
    loop.run()
    # Three targeted passes, each aimed at the unexecuted function only; the
    # fourth measurement reads the budget spent with the count still 0.
    assert loop.phase_c_targets == [["never"], ["never"], ["never"]]
    assert loop.measurements == 4
    rec = loop.record
    assert rec.accounted and rec.pending == ()
    assert [(u.site.fn_name, u.attempts) for u in rec.unreached] == [("never", (1, 2, 3))]
    held = [ln for ln in loop.logs if "NOT stopping" in ln]
    assert len(held) == 3 and "src/lib.rs::never" in held[0]
    assert any("functions: executed 1/2, unexecuted 0 (cluster-gated 0), unreached 1" in ln
               for ln in loop.logs)
    assert any(e == "task0c.fn_stop_held" for e, _ in loop.events)
    assert any("95.0% coverage reached" in ln for ln in loop.logs)


def test_a_measurement_that_counts_the_function_releases_the_stop(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop(tmp_path, monkeypatch, never_counts=[0, 2])
    loop.run()
    assert loop.phase_c_targets == [["never"]]
    assert loop.measurements == 2
    assert {s.fn_name for s in loop.record.executed} == {"called", "never"}
    assert loop.record.unreached == ()


def test_an_unmeasured_reading_stops_on_lines_and_says_why(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop(tmp_path, monkeypatch, never_counts=[None])
    loop.run()
    assert loop.phase_c_targets == [] and loop.measurements == 1
    assert any("function execution NOT measured (the coverage backend reported no "
               "function records)" in ln for ln in loop.logs)
    assert loop.record.measured is False


def test_the_record_resumes_with_the_attempts_already_spent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _Loop(tmp_path, monkeypatch, never_counts=[0], budget=3)
    monkeypatch.setattr(task0c_mod, "_run_phase_c", lambda **kw: _PhaseCOutcome(
        "return", result=(95.0, 19, False, ""),
        fn_attempted=frozenset(t.site for t in kw["fn_targets"])))
    first.run()                                    # one pass charged, then the module returns
    assert first.record.pending[0].attempts == (1,)
    second = _Loop(tmp_path / "again", monkeypatch, never_counts=[0], budget=3)
    second.store = first.store
    second.ws = first.ws
    second.run()
    # Two more passes (numbering restarts at 1) spend the budget of three.
    assert second.phase_c_targets == [["never"], ["never"]]
    assert [u.attempts for u in second.record.unreached] == [(1, 1, 2)]


def test_negative_control_the_pre_fix_stop_breaks_with_the_function_unexecuted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FnCoverageRecord, "accounted", property(lambda self: True))
    loop = _Loop(tmp_path, monkeypatch, never_counts=[0])
    loop.run()
    assert loop.phase_c_targets == [] and loop.measurements == 1
    assert [p.target.site.fn_name for p in loop.record.pending] == ["never"]


# --- the functions no test can execute are proved, not attempted -----

_RUSTDOC = json.dumps({"index": {"1": {
    "name": "called", "span": {"filename": "src/lib.rs", "begin": [1, 1], "end": [3, 2]},
    "inner": {"function": {"sig": {"inputs": [], "output": {"primitive": "u32"}}}}}}})


def _mir(*, calls_never: bool = False) -> str:
    use = "        _1 = never() -> [return: bb1, unwind continue];\n" if calls_never else ""
    return ("fn called() -> u32 {\n    bb0: {\n" + use + "        return;\n    }\n}\n\n"
            "fn never() -> u32 {\n    bb0: {\n        return;\n    }\n}\n")


def _reader(mir: str, reason: str = "") -> Any:
    calls: list[str] = []

    def read(project_dir: Path, cluster: Any) -> WorldReading:
        calls.append("read")
        return (WorldReading("stable", reason=reason) if reason
                else WorldReading("stable", mir=mir, rustdoc=_RUSTDOC))
    read.calls = calls  # type: ignore[attr-defined]
    return read


def test_a_function_the_compiler_proves_unreachable_is_never_aimed_at(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop(tmp_path, monkeypatch, never_counts=[0])
    loop.reach = _reader(_mir())
    loop.run()
    # No targeted pass at all, one measurement, the stop reads the line target.
    assert loop.phase_c_targets == [] and loop.measurements == 1
    rec = loop.record
    assert rec.accounted and rec.pending == () and rec.unreached == ()
    assert [(p.site.fn_name, "never" in p.proof) for p in rec.by_construction] == [("never", True)]
    assert any("1 function(s) no test can execute, proved" in ln and "src/lib.rs::never" in ln
               for ln in loop.logs)
    ev = [f for e, f in loop.events if e == "task0c.fn_reachability"]
    assert ev and ev[0]["data"]["status"] == "ok" and len(ev[0]["data"]["proved"]) == 1
    assert any("95.0% coverage reached" in ln for ln in loop.logs)


def test_negative_control_a_call_in_the_dump_leaves_the_function_to_the_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop(tmp_path, monkeypatch, never_counts=[0])
    loop.reach = _reader(_mir(calls_never=True))
    loop.run()
    assert loop.phase_c_targets == [["never"], ["never"], ["never"]]
    assert loop.record.by_construction == ()
    assert [u.site.fn_name for u in loop.record.unreached] == ["never"]


def test_no_reading_means_the_budget_decides_alone_and_the_log_says_why(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop(tmp_path, monkeypatch, never_counts=[0])
    loop.reach = _reader("", reason="cargo rustc rc=101: error")
    loop.run()
    assert loop.phase_c_targets == [["never"], ["never"], ["never"]]
    assert any("no reachability proof taken (cargo rustc rc=101: error)" in ln for ln in loop.logs)


def test_the_measurement_beats_a_proof_and_the_counterexample_is_loud(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop(tmp_path, monkeypatch, never_counts=[2])
    loop.reach = _reader(_mir())
    loop.run()
    assert {s.fn_name for s in loop.record.executed} == {"called", "never"}
    assert loop.record.by_construction == ()
    assert any("COUNTEREXAMPLE to the reachability proof: src/lib.rs::never" in ln
               for ln in loop.logs)
    assert any(e == "task0c.fn_reachability_contradicted" for e, _ in loop.events)


def test_the_kill_switch_never_calls_the_reader(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK0C_FN_REACHABILITY", "0")
    loop = _Loop(tmp_path, monkeypatch, never_counts=[0])
    loop.reach = _reader(_mir())
    loop.run()
    assert loop.reach.calls == [] and loop.record.by_construction == ()
