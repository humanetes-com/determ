"""The fill gates' stub-only crash keep and the sweep's closure exemption read
one fact: the tests a write lost died with their process, inside a
still-stubbed function. Two capture shapes hid that fact from both readers
after the 2026-10-19 close of the destructor-line veto (review 2026-10-22):

- the fill runner joins every build world into one capture, so a
  comparison's `newly_crashed` names `world::label` while the section slicer
  labelled the bare cargo target; `crash_panics` then reported the name
  missing and the keep never fired;
- test isolation re-runs an aborted binary one test per process, so the
  abort is a red verdict of that test (`newly_red`) and `newly_crashed` is
  empty; the crash-only branch asked only `newly_crashed`.

Invented shapes only: a stub at `src/holder.rs:12` reached while a value is
dropped during unwind, and the runtime's own destructor announcement."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.test_results import (
    ABORT_MARK,
    STDERR_MARK,
    TestStateReading,
    _one_world_sections,
    binary_output_sections,
    compare_readings,
    join_world_captures,
    parse_binary_results,
)

_RUNTIME = ("thread 'b' panicked at /rustc/0000/library/core/src/panicking.rs:233:5:\n"
            "panic in a destructor during cleanup\n"
            "thread caused non-unwinding panic. aborting.")
_STUB = "thread 'b' panicked at src/holder.rs:12:9:\nnot implemented"


def _capture(stdout: str, label: str = "tests/holder.rs") -> str:
    return (stdout + f"\n{STDERR_MARK}\n"
            f"     Running {label} (target/debug/deps/holder-aaaa)\n")


_DONE = ("running 2 tests\n"
         "test a ... ok\n"
         "test b ... ok\n"
         "test result: ok. 2 passed; 0 failed; 0 ignored\n")
_SHARED_ABORT = ("running 2 tests\n"
                 "test a ... ok\n"
                 "test b ... \n" + _STUB + "\n" + _RUNTIME + "\n")


def _reading(capture: str) -> TestStateReading:
    return TestStateReading(parse_binary_results(capture))


def _joined(per_world: dict[str, str]) -> str:
    return join_world_captures([(w, _capture(o)) for w, o in per_world.items()])


def test_a_joined_capture_names_each_section_as_the_comparison_names_its_binary() -> None:
    from pipeline_core.domain.test_report import crash_panics, stub_only_crash
    base = _joined({"base": _DONE, "nightly": _DONE})
    after = _joined({"base": _SHARED_ABORT, "nightly": _DONE})
    cmp = compare_readings(_reading(base), _reading(after))
    assert cmp.newly_crashed == ("base::tests/holder.rs",)
    labels = {s.label for s in binary_output_sections(after)}
    assert labels == {"base::tests/holder.rs", "nightly::tests/holder.rs"}
    panics = crash_panics(after, cmp.newly_crashed)
    assert not panics.missing and panics.runtime_aborts == 1
    assert stub_only_crash(after, cmp.newly_crashed) == frozenset(
        {("src/holder.rs", 12)})
    # NEGATIVE CONTROL: the pre-fix slicer over the same joined capture
    # labels the bare target, so the comparison's name is not a section.
    assert "base::tests/holder.rs" not in {
        s.label for s in _one_world_sections(after)}


def test_a_one_world_capture_keeps_its_bare_labels() -> None:
    after = _capture(_SHARED_ABORT)
    assert [s.label for s in binary_output_sections(after)] == ["tests/holder.rs"]


def _isolated_after(block_b: str) -> str:
    return _capture(
        "running 2 tests\n"
        "test a ... ok\n"
        "test b ... FAILED\n\n"
        "failures:\n\n"
        "---- b stdout ----\n"
        f"{ABORT_MARK}\n{block_b}\n\n"
        "failures:\n    b\n\n"
        "test result: FAILED. 1 passed; 1 failed; 0 ignored\n")


def test_an_isolated_abort_is_the_lost_by_abort_shape_with_its_cost() -> None:
    cmp = compare_readings(_reading(_capture(_DONE)),
                           _reading(_isolated_after(_STUB + "\n" + _RUNTIME)))
    assert cmp.newly_red == frozenset({"b"}) and not cmp.newly_crashed
    assert cmp.newly_red_aborted == frozenset({"b"})
    assert cmp.abort_lost_pass == 1 and cmp.lost_by_abort
    # An ordinary red verdict is not an abort: the shape does not hold.
    plain = compare_readings(
        _reading(_capture(_DONE)),
        _reading(_capture("running 2 tests\ntest a ... ok\ntest b ... FAILED\n\n"
                          "failures:\n\n---- b stdout ----\n"
                          "thread 'b' panicked at tests/holder.rs:4:5:\n"
                          "assertion failed\n\nfailures:\n    b\n\n"
                          "test result: FAILED. 1 passed; 1 failed; 0 ignored\n")))
    assert plain.newly_red == frozenset({"b"}) and not plain.lost_by_abort


def _gate(after: str, tmp_path: Path, hollow_before: int | None,
          fired: list[tuple[str, str]]):
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )

    def run_cargo(*_a, **_k):
        return 101, after

    return test_regression_gate(
        run_cargo, "cargo test", tmp_path, {}, _reading(_capture(_DONE)),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired.append((loc, m)),
        hollow_before=hollow_before)


def test_the_gate_reads_an_isolated_stub_abort_by_the_crash_only_rule(
        tmp_path: Path) -> None:
    """`tmp_path` holds no `.rs`, so the hollow count after is 0: a caller
    whose baseline counted one hollow body closed it (the closure keep)."""
    after = _isolated_after(_STUB + "\n" + _RUNTIME)
    fired: list[tuple[str, str]] = []
    assert _gate(after, tmp_path, 1, fired) is None
    assert any(loc == "phase-c/crash_closure_kept" for loc, _m in fired)
    # The hollow set not shrinking is the sweep's charge: reverted, and the
    # comparison carries the stub site for the retro writer.
    fired2: list[tuple[str, str]] = []
    res = _gate(after, tmp_path, 0, fired2)
    assert res is not None
    assert res[0].crash_stub_sites == frozenset({("src/holder.rs", 12)})
    assert any(loc == "phase-c/crash_stub_panic_reverted" for loc, _m in fired2)
    # NEGATIVE CONTROL: the pre-fix branch condition (`newly_crashed and not
    # newly_red`) is false on this comparison, so the panics were never read.
    cmp = res[0]
    assert not (cmp.newly_crashed and not cmp.newly_red)


def test_an_isolated_abort_with_a_crate_panic_is_not_exonerated(tmp_path: Path) -> None:
    after = _isolated_after("thread 'b' panicked at src/holder.rs:30:9:\n"
                            "index out of bounds\n" + _RUNTIME)
    fired: list[tuple[str, str]] = []
    res = _gate(after, tmp_path, 1, fired)
    assert res is not None and not res[0].crash_stub_sites
    assert any(loc == "phase-c/crash_only_reverted" for loc, _m in fired)


def test_the_sweep_closure_reads_the_same_isolated_abort() -> None:
    from pipeline_core.application.reconstruct.tdd_ratchet import (
        aborted_names,
        closure_stub_sites,
        closure_withheld,
    )
    after = _isolated_after(_STUB + "\n" + _RUNTIME)
    sites = closure_stub_sites(_reading(_capture(_DONE)), after)
    assert sites == frozenset({("src/holder.rs", 12)})
    judged = _reading(after).judged_names
    assert "b" in judged and aborted_names(after) == frozenset({"b"})
    assert closure_withheld("breach", frozenset({"b"}), judged - aborted_names(after),
                            sites, hollow_now=0, hollow_at_snapshot=1)
    # NEGATIVE CONTROL: the judged set as passed before keeps the aborted
    # test, and the exemption never fires.
    assert closure_withheld("breach", frozenset({"b"}), judged, sites,
                            hollow_now=0, hollow_at_snapshot=1) == ""
