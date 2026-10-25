"""A quoted failure names the cfg world it was captured in.

Run `1700492-f6c4d66c` (2026-10-24): the one-tree loop judges every world in
one joined capture (`test_results.join_world_captures`). Its only red test
failed only in the `nightly` world, in a test file the home world does not
build. The section splitter read the joined stdout as one stream, so the
quoted failure named no world, and the fill prompt's invariants section said
the tests run in the home world. Invented captures, invented names.
"""
from __future__ import annotations

from pipeline_core.domain.test_report import (
    OTHER_COPY_MARK,
    failure_evidence_by_test,
    parse_test_output,
)
from pipeline_core.domain.test_results import (
    failure_bodies_for,
    join_capture,
    join_world_captures,
    split_test_stdout_sections_in_worlds,
    world_failure_line,
)


def _libtest(name: str, file: str, line: int, msg: str) -> str:
    return (
        "running 1 test\n"
        f"test {name} ... FAILED\n\n"
        "failures:\n\n"
        f"---- {name} stdout ----\n\n"
        f"thread '{name}' panicked at {file}:{line}:5:\n"
        f"{msg}\n"
        "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n\n\n"
        "failures:\n"
        f"    {name}\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n")


def _ok() -> str:
    return ("running 1 test\ntest other_thing ... ok\n\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n")


def _stderr(label: str) -> str:
    return f"     Running {label} (target/debug/deps/probe-0000)\n"


def _joined(base_out: str, alt_out: str) -> str:
    return join_world_captures([
        ("base", join_capture(base_out, _stderr("tests/probe.rs"))),
        ("alt", join_capture(alt_out, _stderr("tests/probe.rs"))),
    ])


def test_a_failure_only_another_world_builds_names_that_world() -> None:
    cap = _joined(_ok(), _libtest("reads_value", "tests/probe.rs", 12,
                                  "assertion failed: v.is_some()"))
    [f] = parse_test_output(cap).failures
    assert f.world == "alt"
    text = failure_evidence_by_test(cap)["reads_value"]
    assert text.splitlines()[0] == world_failure_line("alt")
    assert "panicked at tests/probe.rs:12:5" in text


def test_the_same_binary_failing_in_two_worlds_is_not_a_different_binary() -> None:
    cap = _joined(
        _libtest("reads_value", "tests/probe.rs", 12, "left: 1 right: 2"),
        _libtest("reads_value", "tests/probe.rs", 12, "left: 3 right: 2"))
    [f] = parse_test_output(cap).failures
    assert (f.world, [o.world for o in f.others]) == ("base", ["alt"])
    text = failure_evidence_by_test(cap)["reads_value"]
    assert text.splitlines()[0] == world_failure_line("base")
    assert f"{OTHER_COPY_MARK} reads_value, in cfg world `alt`, also failed:)" in text
    assert "different test binary" not in text
    # NEGATIVE CONTROL: the same two sections read without their world
    # headers (the pre-change splitter's reading) say "a different binary"
    # and name no world.
    blind = "\n".join(ln for ln in cap.splitlines()
                      if not ln.startswith("=== cfg world: "))
    blind_text = failure_evidence_by_test(blind)["reads_value"]
    assert "in a different test binary, also failed" in blind_text
    assert "cfg world" not in blind_text


def test_a_one_world_capture_reads_as_before() -> None:
    cap = join_capture(_libtest("reads_value", "tests/probe.rs", 12, "boom"),
                       _stderr("tests/probe.rs"))
    [f] = parse_test_output(cap).failures
    assert f.world == ""
    text = failure_evidence_by_test(cap)["reads_value"]
    assert "cfg world" not in text and text.startswith("thread 'reads_value'")


def test_a_world_header_closes_an_open_section() -> None:
    # A section the base world left open (no trailing structural line) must
    # not absorb the next world's output as its body.
    base = "failures:\n\n---- reads_value stdout ----\nthread 'reads_value' panicked at src/lib.rs:3:5:\nbase says\n"
    cap = _joined(base, _libtest("reads_value", "tests/probe.rs", 12, "alt says"))
    parts = split_test_stdout_sections_in_worlds(cap)
    assert [(w, n) for w, n, _b in parts] == [("base", "reads_value"),
                                               ("alt", "reads_value")]
    assert "alt says" not in parts[0][2]


def test_failure_bodies_keep_each_worlds_section() -> None:
    cap = _joined(
        _libtest("reads_value", "tests/probe.rs", 12, "base says"),
        _libtest("reads_value", "tests/probe.rs", 12, "alt says"))
    body = failure_bodies_for(cap, {"reads_value"})["reads_value"]
    assert world_failure_line("base") in body and "base says" in body
    assert world_failure_line("alt") in body and "alt says" in body
    # One world failing: the body is that section, byte for byte.
    one = _joined(_ok(), _libtest("reads_value", "tests/probe.rs", 12, "alt says"))
    only = failure_bodies_for(one, {"reads_value"})["reads_value"]
    assert "cfg world" not in only and only.startswith("thread 'reads_value'")
