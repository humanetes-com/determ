"""One capture layout: stdout, the stderr mark, stderr — one writer, one
split, and every section reader reads sections from stdout only.

Two instances of one class (two readers splitting one capture two ways):

- the sanitizer sweep joined its streams with no mark, so trybuild's stderr
  progress line became `tests/wrapper_gen_6.rs`'s in-flight test (run
  `12616-8345ae1d`, 2026-09-27; the recorded excerpt below splits at its
  first cargo stderr line);
- an isolated test's block embedded its own process's joined output, mark
  included, inside the capture's stdout, and `binary_output_sections`
  switched to stderr at that first mark: the isolated binary read as never
  completed and every later section was dropped (found 2026-09-27 while
  building this batch).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain import test_results
from pipeline_core.domain.test_isolation import (
    STDERR_IN_BLOCK,
    read_single_test_run,
    render_isolated_section,
)
from pipeline_core.domain.test_results import (
    STDERR_MARK,
    binary_output_sections,
    join_capture,
    parse_binary_results,
    split_capture,
)

_EXCERPT = (Path(__file__).parent / "fixtures" / "crash"
            / "asan_trybuild_stderr_after_open_section_12616.txt")


def _marked_excerpt() -> str:
    lines = _EXCERPT.read_text(encoding="utf-8").split("\n")
    cut = next(i for i, ln in enumerate(lines) if ln.startswith("   Compiling"))
    return join_capture("\n".join(lines[:cut]), "\n".join(lines[cut:]))


def test_a_stderr_line_never_names_an_in_flight_test(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The name rule is off: the layout alone must keep the stderr line out.
    monkeypatch.setattr(test_results, "_has_control", lambda text: False)
    got = {b.label: b.dangling for b in parse_binary_results(_marked_excerpt())
           if not b.completed}
    assert got == {"tests/wrapper_gen_6.rs": "test_boxed_error_debug_via_as_ref"}


def test_negative_control_the_unmarked_capture_names_the_trybuild_line(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_results, "_has_control", lambda text: False)
    raw = _EXCERPT.read_text(encoding="utf-8")
    got = {b.dangling for b in parse_binary_results(raw) if not b.completed}
    assert any("temporary-value" in (d or "") for d in got)


def _isolated_then_completed(embedded_mark: bool) -> str:
    """A binary rebuilt by the isolation renderer, then a second binary that
    completed. ``embedded_mark`` writes the aborted test's block the way the
    old runner did (its process's joined output, mark included)."""
    process = ("running 1 test\ntest t_abort ... \n"
               f"{STDERR_MARK}\nthread 't_abort' has overflowed its stack")
    one = (read_single_test_run("t_abort", -6, process, timed_out=False,
                                timeout=60) if embedded_mark else
           read_single_test_run("t_abort", -6, process.split(f"\n{STDERR_MARK}\n")[0],
                                stderr="thread 't_abort' has overflowed its stack",
                                timed_out=False, timeout=60))
    if embedded_mark:
        one = type(one)(one.name, one.status,
                        one.body.replace(STDERR_IN_BLOCK, STDERR_MARK))
    stdout = (render_isolated_section([one]) + "\nrunning 1 test\n"
              "test t_ok ... ok\n\ntest result: ok. 1 passed; 0 failed; "
              "0 ignored; 0 measured; 0 filtered out\n")
    stderr = ("     Running tests/a.rs (target/debug/deps/a-1)\n"
              "     Running tests/b.rs (target/debug/deps/b-2)\n")
    return join_capture(stdout, stderr)


def test_an_isolated_binary_completes_and_later_sections_survive() -> None:
    for embedded in (False, True):
        secs = binary_output_sections(_isolated_then_completed(embedded))
        assert [(s.label, s.completed) for s in secs] == [
            ("tests/a.rs", True), ("tests/b.rs", True)], embedded


def test_negative_control_splitting_at_the_first_mark_loses_the_sections(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def first_mark(output: str) -> tuple[str, str]:
        head, sep, tail = output.partition(f"\n{STDERR_MARK}\n")
        return (head, tail) if sep else (output, "")
    monkeypatch.setattr(test_results, "split_capture", first_mark)
    secs = binary_output_sections(_isolated_then_completed(True))
    assert [s.completed for s in secs] != [True, True]


def test_an_isolated_block_carries_no_capture_mark() -> None:
    one = read_single_test_run(
        "t", -6, "running 1 test\ntest t ... ", stderr="runtime line",
        timed_out=False, timeout=60)
    assert STDERR_MARK not in one.body and "runtime line" in one.body


def test_split_is_at_the_last_mark_and_a_markless_capture_is_all_stdout() -> None:
    assert split_capture("a\n--- stderr ---\nb\n--- stderr ---\nc") == (
        "a\n--- stderr ---\nb", "c")
    assert split_capture("plain") == ("plain", "")
