"""The sweep's RED-build verdict is cargo's own `could not compile` in the
BUILD PHASE of the capture, never a substring over the whole stream.

Run `2131920-86818224` (cluster `nightly`, 2026-09-07): `tests/compiletest.rs::ui`
is trybuild, which builds the crate again as a path dependency WITHOUT the
cluster's `--cfg`, so the cluster-correct six-field `ErrorVTable` literal fails
`E0063` in that nested world and the nested cargo's `error: could not compile
`anyhow` (lib)` lands in the sweep stream between `running 1 test` and `test ui
... FAILED`. All 32 binaries completed; the whole-stream substring read the sweep
as red, narrowed the frontier from 33 fns to the one the NESTED build
implicated, dispatched 0 of 0 twice and closed the cluster EXHAUSTED at 42/152.

Six streams, one predicate, the whole-capture substring as the negative control
on the row that motivated the change. The nested row is the sweep reproduced
with the loop's own command (`--no-fail-fast --lib --test <31 stems> --
--test-threads=1`) on the run's tree in a scratch target dir; its 32 labels are
byte-equal to the run's iter-2 `phase_c.tdd.iter` event. The lib-fail and
test-bin-fail rows are the shape cargo 1.95.0 / 1.97.0-nightly print, measured
2026-09-07: a compile failure in ANY target runs NO binary (zero `Running`,
zero `running N tests`).
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.failure_classification import (
    FailureCategory,
    classify_failure,
)
from pipeline_core.domain.test_cmd import expected_target_labels
from pipeline_core.domain.test_results import (
    build_broke,
    compile_error_digest,
    compile_failed_targets,
    lib_compile_clean,
    parse_binary_results,
    sweep_build,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "crash"
_NESTED = (_FIXTURES
           / "sweep_trybuild_nested_could_not_compile_2131920_2026-09-07_iter2.txt")

# The loop's `stems=31` for `nightly` (the cluster-entry line of `2131920`),
# plus `--lib`: the command the reproduced sweep ran.
_NIGHTLY_STEMS = [
    "behaviour_nightly_gen_1", "compiletest", "context_gen_3", "context_gen_4",
    "context_gen_5", "ensure_gen_2", "error_gen_4", "error_gen_5", "error_gen_6",
    "error_gen_7", "fmt_gen_2", "lib_gen_2", "ptr_gen_2", "ptr_gen_3",
    "test_autotrait", "test_backtrace", "test_boxed", "test_chain", "test_context",
    "test_convert", "test_downcast", "test_ensure", "test_ffi", "test_fmt",
    "test_macros", "test_repr", "test_source", "wrapper_gen_4", "wrapper_gen_5",
    "wrapper_gen_6", "wrapper_gen_7",
]

# A lib that did not build: nothing runs (measured cargo shape).
_LIB_FAIL = (
    "   Compiling anyhow v1.0.104 (/w)\n"
    "error[E0599]: no method named `context` found for type parameter `E`\n"
    " --> src/context.rs:24:14\n"
    "error: could not compile `anyhow` (lib) due to 1 previous error\n"
)
# The lib built and one test target did not: nothing runs either — cargo
# 1.95.0 / 1.97.0-nightly print exactly this, nine lines, no `Running`, and the
# `ok` binary that DID compile is not executed.
_TEST_BIN_FAIL = (
    "   Compiling anyhow v1.0.104 (/w)\n"
    "error: expected one of `!` or `::`, found `fn`\n"
    " --> tests/bad.rs:1:5\n"
    "  |\n"
    "1 | bad fn x() {}\n"
    "  |     ^^ expected one of `!` or `::`\n"
    "\n"
    'error: could not compile `anyhow` (test "bad") due to 1 previous error\n'
    "warning: build failed, waiting for other jobs to finish...\n"
)
# The scoped u2 run's compile fixture (`tests/test_cargo_runner.py`): no
# expected labels at all, the marker is the whole capture.
_U2_COMPILE = "error: could not compile `crate`"
# A binary that opened its section and died before the trailer.
_CRASH = (
    "     Running tests/x.rs (target/debug/deps/x-1)\n"
    "running 2 tests\n"
    "test a ... ok\n"
    "test b ... "
)


def _nested() -> str:
    return _NESTED.read_text(encoding="utf-8")


def test_nested_could_not_compile_inside_a_running_test_is_not_red() -> None:
    out = _nested()
    expected = expected_target_labels(_NIGHTLY_STEMS, include_lib=True)
    build = sweep_build(out, expected)
    assert len(expected) == 32
    assert build.built == frozenset(expected), "every target the command asked for ran"
    assert build.missing == frozenset()
    assert build.red is False
    assert build_broke(out) is False
    assert compile_error_digest(build.build_output) == (0, [])
    assert build.failed_targets == ()
    assert all(b.completed for b in parse_binary_results(out))
    assert classify_failure(out) is not FailureCategory.BUILD


def test_negative_control_the_whole_capture_substring_reads_the_nested_build_as_this_crate() -> None:
    """The pre-fix reading, asserted to be the wrong one: the marker IS in the
    stream, the digest DOES name the nested world's `E0063`, and cargo's target
    naming says `lib` — indistinguishable by text from the sweep's own build."""
    out = _nested()
    assert "could not compile" in out
    n, lines = compile_error_digest(out)
    assert n == 1
    assert "error.rs:202" in lines[0] and "E0063" in lines[0]
    assert "object_backtrace" in lines[0]
    assert compile_failed_targets(out) == ("lib",)
    assert lib_compile_clean(out) is False


def test_a_lib_that_did_not_build_is_red_and_nothing_ran() -> None:
    expected = expected_target_labels(["t"], include_lib=True)
    build = sweep_build(_LIB_FAIL, expected)
    assert build.red is True and build_broke(_LIB_FAIL) is True
    assert build.missing == frozenset(expected)
    assert build.build_output == _LIB_FAIL, "a genuine red is its build phase in full"
    assert compile_error_digest(build.build_output)[0] == 1
    assert build.failed_targets == ("lib",)
    assert lib_compile_clean(build.build_output) is False
    assert classify_failure(_LIB_FAIL) is FailureCategory.BUILD


def test_a_test_target_that_did_not_build_is_red_and_keeps_the_lib_clean_discriminator() -> None:
    """The cause-A layout retrigger's own case: the lib built, a binary did
    not. No `Running` line for the binaries that DID compile — cargo does not
    run them — so the discriminator is cargo's target naming, not the label
    accounting."""
    expected = expected_target_labels(["ok", "bad"], include_lib=True)
    build = sweep_build(_TEST_BIN_FAIL, expected)
    assert build.red is True
    assert build.missing == frozenset(expected)
    assert build.failed_targets == ('test "bad"',)
    assert lib_compile_clean(build.build_output) is True
    assert classify_failure(_TEST_BIN_FAIL) is FailureCategory.BUILD


def test_a_walk_stub_with_no_running_line_is_not_red() -> None:
    """The walk harness renders sweeps without `Running` lines. Missing
    labels therefore cannot define red: every replayed walk would be
    compile-broke, `improved` would never fire and the high-water would never
    re-base."""
    from tests.walks.harness import render_sweep
    out = render_sweep(1, 2)
    expected = expected_target_labels(["t"], include_lib=True)
    build = sweep_build(out, expected)
    assert build.missing == frozenset(expected), "the stub prints no Running line"
    assert build.red is False
    assert build_broke(out) is False


def test_the_scoped_u2_compile_fixture_stays_a_compile_failure() -> None:
    build = sweep_build(_U2_COMPILE, ())
    assert build.missing == frozenset()
    assert build.red is True
    assert build_broke(_U2_COMPILE) is True


def test_a_crashed_binary_is_neither_missing_nor_red() -> None:
    expected = ("tests/x.rs",)
    build = sweep_build(_CRASH, expected)
    bins = parse_binary_results(_CRASH)
    assert bins and bins[0].completed is False
    assert build.missing == frozenset()
    assert build.red is False


def test_expected_labels_match_the_live_iter_event_vocabulary() -> None:
    """The plan's fixture, computed: the 31 stems plus the lib binary equal
    the 32 labels the reproduced stream (and the run's iter-2 event) carry."""
    labels = {b.label for b in parse_binary_results(_nested())}
    assert labels == set(expected_target_labels(_NIGHTLY_STEMS, include_lib=True))
    assert expected_target_labels(["a"]) == ("tests/a.rs",)
    assert expected_target_labels(["a"], include_lib=True) == (
        "unittests src/lib.rs", "tests/a.rs")
