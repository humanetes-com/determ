"""One test per process for the binaries a cargo test run lost to an abort.

libtest runs every test of a binary in one process. When one test aborts the
process — a panic during a `Drop` that runs while another panic unwinds, a
signal, undefined behaviour — every other test in that binary loses its
verdict with it, including tests that pass. Every reader of the capture then
had to decide whose loss that was, and the decision grew one branch per
incident across the fill gate and the sweep ratchet (a stub-only exemption,
the runtime's own abort line, an all-red binary, a closure exemption, …). On
run `3213361-9d582ae7` (2026-09-19) a correct first `construct` let every
test build an `Error`; 34 binaries aborted on the still-stubbed drop path,
two of them after an already-red test's own assertion, and that assertion
vetoed the exemption: every `construct` was reverted as a crash it did not
cause, while the tests those binaries lost were all red at baseline.

The collateral is removed where it is made, in the measurement: a binary
whose section never closed is re-run one test per process (`--exact`), and
its section is rebuilt from those runs in libtest's own format. Each test
then carries its own verdict — passed, failed, ignored, or failed by an
abort of its OWN process (`ABORT_MARK` heads its failure block, followed by
what that process printed) — and every reader of the capture compares
per-test verdicts by the one rule it already has for completed binaries.

This module is the pure half: which arguments a re-run repeats, how one
single-test run is read, how the rebuilt section is rendered and spliced into
the capture. `application.reconstruct.cargo_runner.run_cargo` runs the
processes.
"""
from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.test_results import (
    ABORT_MARK,
    failure_block_name,
    is_section_header,
    parse_binary_results,
    target_label,
)

__all__ = [
    "IsolatedStatus",
    "IsolatedTest",
    "harness_args",
    "is_isolatable_test_run",
    "listed_tests",
    "listing_args",
    "read_single_test_run",
    "render_isolated_section",
    "run_args",
    "splice_isolated_sections",
]

# libtest harness flags that take a separate value argument.
_VALUE_FLAGS = frozenset({
    "--test-threads", "--skip", "--format", "--logfile", "--color", "-Z",
    "--shuffle-seed", "--report-time-format"})
# Flags a single-test re-run sets itself or must not repeat: it lists or
# runs exactly one name, on one thread, with libtest's verbose verdict lines
# (the parsers read `test NAME ... ok`, which `-q` suppresses).
_DROPPED_FLAGS = frozenset({
    "--exact", "--list", "--test-threads", "--format", "-q", "--quiet",
    "--ignored", "--include-ignored"})


def is_isolatable_test_run(cmd: str) -> bool:
    """A cargo command that RUNS test binaries: the `test` subcommand, not
    `--no-run` (nothing ran) and not `--list` (nothing ran)."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if not argv or argv[0] != "cargo":
        return False
    head = argv[: argv.index("--")] if "--" in argv else argv
    tail = argv[argv.index("--") + 1:] if "--" in argv else []
    if "test" not in head:
        return False
    return not ({"--no-run", "--list"} & set(head)
                or "--list" in tail)


def harness_args(cmd: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(flags to repeat, name filters) from the arguments after `--`.

    Filters are what libtest matched names against in the original run; a
    re-run lists with them so it runs exactly the tests the binary would
    have run. Flags are repeated on every single-test run except the ones
    in `_DROPPED_FLAGS`, and `--ignored`/`--include-ignored` only change
    which tests are LISTED, so they are kept for listing through the
    filters' side (see `listing_args`)."""
    argv = shlex.split(cmd)
    if "--" not in argv:
        return (), ()
    tail = argv[argv.index("--") + 1:]
    flags: list[str] = []
    filters: list[str] = []
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            takes_value = name in _VALUE_FLAGS and "=" not in tok
            val = tail[i + 1] if takes_value and i + 1 < len(tail) else None
            if name not in _DROPPED_FLAGS or name in ("--ignored",
                                                      "--include-ignored"):
                flags.append(tok)
                if val is not None:
                    flags.append(val)
            i += 2 if takes_value else 1
            continue
        filters.append(tok)
        i += 1
    return tuple(flags), tuple(filters)


def listing_args(cmd: str) -> list[str]:
    """Arguments that make the binary LIST exactly the tests the original
    run executed: the original filters, `--exact` when the original used
    it, the ignored-selection flags, and `--skip`."""
    argv = shlex.split(cmd)
    tail = argv[argv.index("--") + 1:] if "--" in argv else []
    flags, filters = harness_args(cmd)
    out = ["--list", "--format", "terse"]
    i = 0
    while i < len(flags):
        if flags[i] in ("--ignored", "--include-ignored"):
            out.append(flags[i])
        elif flags[i] == "--skip" and i + 1 < len(flags):
            out += [flags[i], flags[i + 1]]
            i += 1
        elif flags[i].startswith("--skip="):
            out.append(flags[i])
        i += 1
    if "--exact" in tail:
        out.append("--exact")
    return out + list(filters)


def run_args(cmd: str, name: str) -> list[str]:
    """Arguments for the single-test run of ``name``: the original flags
    minus the ignored-selection ones (the name is already selected), one
    thread, `--exact`."""
    flags, _filters = harness_args(cmd)
    kept = [f for f in flags if f not in ("--ignored", "--include-ignored")]
    if "--include-ignored" in flags or "--ignored" in flags:
        kept.append("--include-ignored")
    return [*kept, "--test-threads=1", "--exact", name]


def listed_tests(list_output: str) -> tuple[str, ...]:
    """Test names from `--list --format terse` output (`NAME: test`);
    benchmarks are not tests."""
    out: list[str] = []
    for raw in list_output.splitlines():
        line = raw.strip()
        if line.endswith(": test"):
            out.append(line[: -len(": test")])
    return tuple(out)


class IsolatedStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    IGNORED = "ignored"
    ABORTED = "aborted"
    HUNG = "hung"


@dataclass(frozen=True, slots=True)
class IsolatedTest:
    """One test's outcome in its own process, and what that process printed
    about it (the failure block body; empty for a pass)."""

    name: str
    status: IsolatedStatus
    body: str = ""


def _failure_body(out: str, name: str) -> str:
    """The lines under ``---- name stdout ----`` up to libtest's closing
    ``failures:`` list, or "" when the block is absent."""
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if failure_block_name(ln.strip()) == name:
            body: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.strip() == "failures:":
                    break
                body.append(nxt)
            return "\n".join(body).strip("\n")
    return ""


def read_single_test_run(
    name: str, rc: int, out: str, *, timed_out: bool, timeout: float,
) -> IsolatedTest:
    """The outcome of ``name``'s own process from its combined capture.

    A section that closed carries libtest's verdict. One that did not close
    is an abort of THIS test's process (its body is everything the process
    printed after the section opened, which is where the runtime's panic
    lines land); a run the timeout cut is a hang. A process that printed no
    section at all (the binary refused to start) is a failure whose body is
    the whole capture — never a pass."""
    if timed_out:
        return IsolatedTest(name, IsolatedStatus.HUNG,
                            f"test did not return within {timeout:.0f}s")
    sections = parse_binary_results(out)
    sec = sections[0] if sections else None
    if sec is not None and sec.completed:
        if name in sec.passed:
            return IsolatedTest(name, IsolatedStatus.PASSED)
        if name in sec.ignored:
            return IsolatedTest(name, IsolatedStatus.IGNORED)
        if name in sec.failed:
            return IsolatedTest(name, IsolatedStatus.FAILED,
                                _failure_body(out, name) or out.strip())
    if sec is not None:
        after: list[str] = []
        opened = False
        for ln in out.splitlines():
            if opened:
                after.append(ln)
            elif is_section_header(ln.strip()):
                opened = True
        body = "\n".join(after).strip("\n") or out.strip()
        return IsolatedTest(name, IsolatedStatus.ABORTED,
                            f"{body}\n(exit status {rc})")
    return IsolatedTest(name, IsolatedStatus.FAILED,
                        out.strip() or f"(exit status {rc}, no output)")


def render_isolated_section(tests: Sequence[IsolatedTest]) -> str:
    """A complete libtest stdout section for one binary, built from its
    tests' own runs: verdict lines, one failure block per failing test (an
    abort's block opens with `ABORT_MARK`), the failures list, and a
    trailer whose counts are the verdicts'."""
    lines = [f"running {len(tests)} test{'s' if len(tests) != 1 else ''}"]
    for t in tests:
        verdict = {IsolatedStatus.PASSED: "ok",
                   IsolatedStatus.IGNORED: "ignored"}.get(t.status, "FAILED")
        lines.append(f"test {t.name} ... {verdict}")
    failing = [t for t in tests if t.status in (
        IsolatedStatus.FAILED, IsolatedStatus.ABORTED, IsolatedStatus.HUNG)]
    if failing:
        lines += ["", "failures:", ""]
        for t in failing:
            lines.append(f"---- {t.name} stdout ----")
            if t.status is IsolatedStatus.ABORTED:
                lines.append(ABORT_MARK)
            if t.body:
                lines.append(t.body)
            lines.append("")
        lines += ["", "failures:"]
        lines += [f"    {t.name}" for t in failing]
    passed = sum(1 for t in tests if t.status is IsolatedStatus.PASSED)
    ignored = sum(1 for t in tests if t.status is IsolatedStatus.IGNORED)
    lines += ["", f"test result: {'FAILED' if failing else 'ok'}. "
              f"{passed} passed; {len(failing)} failed; {ignored} ignored; "
              "0 measured; 0 filtered out", ""]
    return "\n".join(lines)


def splice_isolated_sections(
    stdout: str, stderr: str, rebuilt: dict[int, str],
) -> tuple[str, str] | None:
    """Replace the stdout section at each index of ``rebuilt`` (indices as
    :func:`parse_binary_results` numbers them) and drop that binary's
    stderr slice under its `Running` line — the whole-run abort's panic
    lines and cargo's `process didn't exit successfully` note, which no
    longer describe any test. Returns None when the capture's shape does
    not allow an unambiguous splice (a headerless section, a label count
    that does not match), and the caller keeps the original capture."""
    out_lines = stdout.split("\n")
    headers = [i for i, ln in enumerate(out_lines)
               if is_section_header(ln.strip())]
    if not rebuilt:
        return stdout, stderr
    if len(parse_binary_results(stdout)) != len(headers):
        return None
    if max(rebuilt) >= len(headers):
        return None
    for idx in sorted(rebuilt, reverse=True):
        start = headers[idx]
        end = headers[idx + 1] if idx + 1 < len(headers) else len(out_lines)
        out_lines[start:end] = rebuilt[idx].split("\n")
    err_lines = stderr.split("\n")
    labels = [i for i, ln in enumerate(err_lines)
              if target_label(ln.strip()) is not None]
    if len(labels) < len(headers):
        return None
    for idx in sorted(rebuilt, reverse=True):
        start = labels[idx] + 1
        end = labels[idx + 1] if idx + 1 < len(labels) else len(err_lines)
        # Keep cargo's trailing summary (`error: N targets failed:` and its
        # list) when this is the last binary: it starts at the first line
        # that is not part of this binary's output.
        if idx + 1 >= len(labels):
            for j in range(start, end):
                if (err_lines[j].startswith("error: ")
                        and err_lines[j].rstrip().endswith(
                            ("target failed:", "targets failed:"))):
                    end = j
                    break
        del err_lines[start:end]
    return "\n".join(out_lines), "\n".join(err_lines)
