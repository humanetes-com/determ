"""Pins for the done-criterion becoming a FLOOR measured from the crate itself.

The run of 2026-08-06 reached the best tree this project has produced —
`stable` 147 passed / 2 ignored / rc=0, `nightly` 128 passed / 1 failed, 275 of
278 across both clusters — and could not report success in two independent ways,
neither of which was a defect in the reconstructed code:

  A. the one failing test is `ui`, a trybuild harness whose assertion is about
     rustc's own diagnostic rendering. Pristine upstream `1dbe1862 Release
     1.0.104` fails the SAME test on the same machine (nightly 98 passed /
     1 failed), because upstream regenerates its `.stderr` fixtures per
     nightly — "Update ui test suite to nightly-2025-09-07", "…2025-06-18",
     "…2024-03-31" — and this machine runs `rustc 1.97.0-nightly (9eb3be26b
     2026-05-18)`. `run_clusters_strict` held `all_pass` false forever on it;
  B. the single-module completion gate ran a cfg-BLIND stub scan with no
     unreachable verdict, so a fully green suite would still have exited 1 on
     the three `#[cfg(anyhow_build_probe)]` bodies in `src/nightly.rs` — two of
     which are byte-identical to the `unimplemented!()` upstream writes there.

And the reason TASK 4 was called at all: `classify_failure` matched `error[e`
inside the trybuild fixture the `ui` test PRINTS, so a run with 275 passing
tests was classified `build`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The recorded reference vector lives in the WORKSPACE, and `.gitignore`
# excludes `tmp` — present on an operator's box, absent from a bare checkout.
# The test that reads it computes its fixture on the live record by design: a
# hand-made pass/fail vector would assert the floor's own rule back at it. So
# the guard is a skip, not a synthetic fixture. Without it the test raised
# FileNotFoundError on tmp/.reference_anyhow/reference.json in CI on
# 2026-09-03; the suite already carries the same guard for the live
# tests/test_fmt.rs pin and for the untracked `.env`.
_LIVE_REFERENCE = REPO / "tmp" / ".reference_anyhow" / "reference.json"

from pipeline_core.domain.cfg import CfgSet  # noqa: E402
from pipeline_core.domain.models import FnLocator  # noqa: E402
from pipeline_core.domain.reference_floor import (  # noqa: E402
    ClusterReference,
    FloorVerdict,
    ReferenceMeasurement,
    is_doc_pair,
    judge_against_reference,
    normalize_pair,
    orphaned_stubs,
    pairs_of,
    reference_floor_close_reason,
)

_UI = ("tests/compiletest.rs", "ui")
_A = ("tests/test_a.rs", "alpha")
_B = ("tests/test_a.rs", "beta")
# A generated test: the pipeline's own oracle, absent from upstream entirely.
_GEN = ("anyhow", "error::gen_tests_1::roundtrip")


# --- normalize_pair: doc-test names carry a line number -------------------


def test_doc_test_line_numbers_are_stripped() -> None:
    """The measured drift, 2026-08-06: the reconstruction's line numbering is
    not upstream's, so a doc test's cargo name differs in every tree. Comparing
    raw names reports all 26 of `anyhow`'s doc tests as missing forever, which
    makes this floor strictly HARDER to satisfy than the absolute it replaces.
    """
    ref = normalize_pair("Doc-tests anyhow", "src/context.rs - context::Option<T> (line 70)")
    ours = normalize_pair("Doc-tests anyhow", "src/context.rs - context::Option<T> (line 55)")
    assert ref == ours
    assert ref == ("Doc-tests anyhow", "src/context.rs - context::Option<T>")
    assert normalize_pair("Doc-tests anyhow", "src/error.rs - error::Error::chain (line 426)") \
        == normalize_pair("Doc-tests anyhow", "src/error.rs - error::Error::chain (line 404)")


def test_a_relocated_unit_test_module_compares_equal() -> None:
    """The pipeline's OWN rename. Inline `#[cfg(test)] mod`s are relocated
    pre-strip into `<stem>.gen_tests_N.rs` siblings, so upstream's
    `fmt::tests::no_digits` runs here as `fmt::gen_tests_1::no_digits`.
    Measured 2026-08-06 against pristine `1dbe1862`, those three `fmt` tests
    were the ENTIRE difference the floor reported on a tree that meets it —
    read as missing in both clusters while `fmt.gen_tests_1.rs` was passing
    3/3 the whole time."""
    upstream = normalize_pair("unittests src/lib.rs", "fmt::tests::no_digits")
    ours = normalize_pair("unittests src/lib.rs", "fmt::gen_tests_1::no_digits")
    assert upstream == ours == ("unittests src/lib.rs", "fmt::no_digits")


def test_a_module_that_is_not_a_test_module_survives() -> None:
    """Only `tests`/`test` and `gen_tests_<N>` are dropped — a real module
    named `testing` or `latest` is part of the test's identity."""
    assert normalize_pair("b", "testing::x")[1] == "testing::x"
    assert normalize_pair("b", "gen_tests_abc::x")[1] == "gen_tests_abc::x"
    assert normalize_pair("b", "ptr::gen_tests_1::test_own_clone")[1] \
        == "ptr::test_own_clone"


def test_repeated_doc_examples_keep_their_cardinality() -> None:
    """Stripping the line number alone is LOSSY: `src/lib.rs - Context` names
    three distinct doc tests in `anyhow` (upstream lines 479, 543, 583) and
    `src/lib.rs - ` names six. Collapsing them took the reference's 26 doc
    tests to 16 and its 98 passing tests to 88 — measured — leaving a floor
    that treats one surviving `Context` example as satisfying all three."""
    from pipeline_core.domain.reference_floor import pairs_of
    binary = SimpleNamespace(
        label="Doc-tests anyhow", completed=True, failed=(), ignored=(),
        passed=("src/lib.rs - Context (line 479)",
                "src/lib.rs - Context (line 543)",
                "src/lib.rs - Context (line 583)"))
    passed, _f, _i, _inc = pairs_of([binary])
    assert len(passed) == 3
    # Ordinals follow SOURCE order, so the k-th example upstream is the k-th
    # here whatever line either sits on — cargo's emission order is a
    # parallel-execution artefact that corresponds to nothing.
    ours = SimpleNamespace(
        label="Doc-tests anyhow", completed=True, failed=(), ignored=(),
        passed=("src/lib.rs - Context (line 402)",
                "src/lib.rs - Context (line 466)",
                "src/lib.rs - Context (line 506)"))
    assert pairs_of([ours])[0] == passed


def test_a_tree_holding_one_of_three_examples_reports_two_missing() -> None:
    from pipeline_core.domain.reference_floor import pairs_of
    ref = SimpleNamespace(
        label="Doc-tests anyhow", completed=True, failed=(), ignored=(),
        passed=("src/lib.rs - Context (line 479)",
                "src/lib.rs - Context (line 543)",
                "src/lib.rs - Context (line 583)"))
    ours = SimpleNamespace(
        label="Doc-tests anyhow", completed=True, failed=(), ignored=(),
        passed=("src/lib.rs - Context (line 402)",))
    verdict = judge_against_reference(
        ClusterReference(cluster="stable", passed=pairs_of([ref])[0],
                         announced=3, reported=3),
        pairs_of([ours])[0], frozenset())
    assert len(verdict.advisory_unmet) == 2


def test_a_name_that_merely_ends_in_a_paren_is_untouched() -> None:
    """Only a trailing ` (line <digits>)` is a doc-test suffix. A `#[test]` fn
    whose path ends in parens or digits keeps its name intact."""
    assert normalize_pair("tests/x.rs", "mod::t (line abc)")[1] == "mod::t (line abc)"
    assert normalize_pair("tests/x.rs", "generic::<u8>")[1] == "generic::<u8>"
    assert is_doc_pair(("Doc-tests anyhow", "x")) is True
    assert is_doc_pair(("tests/compiletest.rs", "ui")) is False


# --- the three states, and why two is not enough --------------------------


def test_a_failure_the_original_also_fails_is_neutral() -> None:
    """The `ui` case, with the measured shape of both runs."""
    ref = ClusterReference(cluster="nightly", passed=frozenset({_A, _B}),
                           failed=frozenset({_UI}), announced=3, reported=3)
    verdict = judge_against_reference(ref, frozenset({_A, _B}), frozenset({_UI}))
    assert verdict.met is True
    assert verdict.unmet == frozenset()
    assert verdict.unexcused_failures == frozenset()


def test_a_failing_test_absent_from_the_reference_is_still_required() -> None:
    """THE three-state pin. `src/*.gen_tests_*.rs` are generated by task0c from
    the source under reconstruction and do not exist upstream — 50 of them on
    `stable` (147 ours vs 97 upstream) and 30 on `nightly` (128 vs 98). They are
    what convict most bodies. "Absent from the reference" is not "fails or skips
    in the reference", and a two-state rule that neutralised everything the
    reference lacks would drop the whole augmentation out of the criterion."""
    ref = ClusterReference(cluster="nightly", passed=frozenset({_A, _B}),
                           failed=frozenset({_UI}), announced=3, reported=3)
    verdict = judge_against_reference(
        ref, frozenset({_A, _B}), frozenset({_UI, _GEN}))
    assert verdict.met is False
    assert verdict.unexcused_failures == frozenset({_GEN})
    assert _UI not in verdict.unexcused_failures


@pytest.mark.skipif(not _LIVE_REFERENCE.exists(),
                    reason="live tmp/.reference_anyhow/reference.json "
                           "not present (untracked workspace)")
def test_tdd_closes_when_failures_are_the_recorded_nightly_neutral() -> None:
    """t2a done is the reference floor once nothing reachable remains.

    Fixture: the recorded nightly `failed` row in
    tmp/.reference_anyhow/reference.json is compiletest ui. A FAIL sweep
    whose failed pairs are that set, with an intact oracle and a compiling
    tree, is the original crate's leftover. A generated name the reference
    never had is not. Negative control: today's t2a close is rc==0 only,
    so this helper returns empty for a green rc.
    """
    from pipeline_core.domain.test_results import split_reachable_failures

    path = REPO / "tmp" / ".reference_anyhow" / "reference.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    nightly = raw["clusters"]["nightly"]
    failed = frozenset(tuple(p) for p in nightly["failed"])
    assert failed == frozenset({_UI})
    ref = ClusterReference(
        cluster="nightly",
        passed=frozenset(tuple(p) for p in nightly["passed"]),
        failed=failed,
        ignored=frozenset(tuple(p) for p in nightly.get("ignored") or []),
        announced=int(nightly["announced"]),
        reported=int(nightly["reported"]),
    )
    ours_pass = ref.passed
    ours_fail = failed
    verdict = judge_against_reference(ref, ours_pass, ours_fail)
    assert verdict.met is True
    reachable, _out = split_reachable_failures(
        [n for _, n in ours_fail],
        excused=[n for _, n in ref.neutral])
    assert reachable == frozenset()
    assert reference_floor_close_reason(
        rc=101, fail_count=1, compile_broke=False, oracle_intact=True,
        verdict=verdict) == verdict.reason
    extra = judge_against_reference(ref, ours_pass, ours_fail | {_GEN})
    assert extra.met is False
    assert extra.unexcused_failures == frozenset({_GEN})
    reachable_extra, _out = split_reachable_failures(
        [n for _, n in (ours_fail | {_GEN})],
        excused=[n for _, n in ref.neutral])
    assert reachable_extra
    assert reference_floor_close_reason(
        rc=101, fail_count=2, compile_broke=False, oracle_intact=True,
        verdict=extra) == ""
    # Negative control: today's t2a close is rc==0 and an intact oracle.
    assert reference_floor_close_reason(
        rc=0, fail_count=0, compile_broke=False, oracle_intact=True,
        verdict=verdict) == ""
    assert reference_floor_close_reason(
        rc=101, fail_count=1, compile_broke=True, oracle_intact=True,
        verdict=verdict) == ""
    assert reference_floor_close_reason(
        rc=101, fail_count=1, compile_broke=False, oracle_intact=False,
        verdict=verdict) == ""


def test_losing_a_test_the_original_passes_fails_the_floor() -> None:
    ref = ClusterReference(cluster="nightly", passed=frozenset({_A, _B}),
                           failed=frozenset({_UI}), announced=3, reported=3)
    verdict = judge_against_reference(ref, frozenset({_A}), frozenset({_UI, _B}))
    assert verdict.met is False
    assert _B in verdict.unmet


def test_exceeding_the_reference_is_never_a_failure() -> None:
    """Floor, not equality: 30 extra passing tests on nightly and 50 on stable
    are the normal state, not drift to be reported as a mismatch."""
    ref = ClusterReference(cluster="stable", passed=frozenset({_A}),
                           announced=1, reported=1)
    verdict = judge_against_reference(ref, frozenset({_A, _B, _GEN}), frozenset())
    assert verdict.met is True
    assert verdict.exceeded == 2


def test_a_test_the_reference_skips_is_neutral() -> None:
    """`ui` is `#[rustversion::attr(not(nightly), ignore)]`, so on `stable` the
    reference IGNORES it. An ignored test bounds nothing either way."""
    ref = ClusterReference(cluster="stable", passed=frozenset({_A}),
                           ignored=frozenset({_UI}), announced=2, reported=2)
    assert judge_against_reference(ref, frozenset({_A}), frozenset({_UI})).met is True


# --- doc tests are compared and reported, never gated ---------------------


_DOC = ("Doc-tests anyhow", "src/lib.rs - Error")


def test_a_missing_doc_test_is_advisory_not_gating() -> None:
    """Measured 2026-08-06: 25 doc tests in the reconstruction against the
    reference's 26, the absent one being `src/lib.rs - Error` — upstream's
    `pub struct Error` (`lib.rs:390`) carries ~90 lines of doc with one runnable
    example, the reconstruction's (`lib.rs:273`) carries no doc comment at all.
    No fill route writes doc comments and the merge's syn round-trip drops
    comments outright, so gating here would fail every run for a reason no
    iteration can address. It is reported instead."""
    ref = ClusterReference(cluster="stable", passed=frozenset({_A, _DOC}),
                           announced=2, reported=2)
    verdict = judge_against_reference(ref, frozenset({_A}), frozenset())
    assert verdict.met is True
    assert verdict.advisory_unmet == frozenset({_DOC})
    assert "src/lib.rs - Error" in verdict.reason


_DOC_RAN = ("Doc-tests anyhow", "src/error.rs - error::Error::boxed#0")


def test_a_doc_test_that_runs_here_and_fails_gates() -> None:
    """Absent and failing are different facts. A doc example that is in the
    tree, was compiled by rustdoc and asserts something false about the
    reconstruction is a failing test the reference passes: the floor is not
    met, while an absent one beside it stays advisory. Run `292191-d60a5308`
    (2026-10-02): the example asserting a boxed error still provides its
    backtrace fails on the merged tree once rustdoc sees the cluster's cfg."""
    ref = ClusterReference(cluster="nightly",
                           passed=frozenset({_A, _DOC, _DOC_RAN}),
                           announced=3, reported=3)
    verdict = judge_against_reference(
        ref, frozenset({_A}), frozenset({_DOC_RAN}))
    assert verdict.met is False
    assert verdict.unmet == frozenset({_DOC_RAN})
    assert verdict.unexcused_failures == frozenset({_DOC_RAN})
    assert verdict.advisory_unmet == frozenset({_DOC})

    # NEGATIVE CONTROL: the filter this replaced dropped every doc pair from
    # both conditions; on the same input it reads the floor as met.
    unmet = ref.passed - frozenset({_A})
    unexcused = frozenset({_DOC_RAN}) - ref.neutral
    old_unmet = frozenset(p for p in unmet if not is_doc_pair(p))
    old_unexcused = frozenset(p for p in unexcused if not is_doc_pair(p))
    assert not old_unmet and not old_unexcused


def test_a_failing_doc_test_the_reference_fails_too_is_excused() -> None:
    ref = ClusterReference(cluster="nightly", passed=frozenset({_A}),
                           failed=frozenset({_DOC_RAN}),
                           announced=2, reported=2)
    assert judge_against_reference(
        ref, frozenset({_A}), frozenset({_DOC_RAN})).met is True


# --- an unhealthy reference is refused, never quietly trusted -------------


def test_a_crashed_reference_binary_makes_it_unusable() -> None:
    """A silently-broken reference LOWERS the bar invisibly — every test it
    failed to run becomes neutral. That is the one way this can do harm."""
    ref = ClusterReference(cluster="nightly", passed=frozenset({_A}),
                           incomplete=frozenset({"tests/test_downcast.rs"}),
                           announced=8, reported=2)
    assert ref.healthy is False
    assert "crashed" in ref.unhealthy_reason
    assert ReferenceMeasurement(key="k", clusters={"nightly": ref}).usable is False


def test_announced_not_equal_reported_makes_it_unusable() -> None:
    ref = ClusterReference(cluster="nightly", passed=frozenset({_A}),
                           announced=9, reported=3)
    assert ref.healthy is False
    assert "announced 9" in ref.unhealthy_reason


def test_a_healthy_reference_is_usable() -> None:
    ref = ClusterReference(cluster="nightly", passed=frozenset({_A, _B}),
                           failed=frozenset({_UI}), announced=3, reported=3)
    assert ref.healthy is True
    assert ReferenceMeasurement(key="k", clusters={"nightly": ref}).usable is True


# --- the stub gate: reachability first, then the reference ----------------

_PROBE = "anyhow_build_probe"
_NIGHTLY = CfgSet.of(cfg_true=["error_generic_member_access"], cfg_false=[])
_STABLE = CfgSet.of(cfg_true=[], cfg_false=["error_generic_member_access"])


def test_the_probe_trio_is_unreachable_under_both_baked_clusters() -> None:
    """`src/nightly.rs` lines 12/17/22 sit under `#[cfg(anyhow_build_probe)]`, a
    build-script probe no `cargo test` sets — `coverage_strategy.md` records
    `NEVER: anyhow_build_probe # build-script only cfg`. No cluster's build can
    compile them, so no fill could ever have been attempted."""
    from pipeline_core.application.orchestrator import classify_remaining_stubs
    stubs = [(FnLocator("src/nightly.rs", n), (_PROBE,))
             for n in ("fmt", "fmt", "provide")]
    verdict = classify_remaining_stubs(stubs, [_STABLE, _NIGHTLY])
    assert verdict.orphaned == ()
    assert len(verdict.unreachable) == 3


def test_the_unguarded_scan_convicts_the_same_trio() -> None:
    """The guard is load-bearing, not decorative: with no cfg-sets on hand
    nothing is provably unreachable and all three come back as defects — which
    is exactly what the single-module completion path did, and why a fully green
    `anyhow` would have exited 1."""
    from pipeline_core.application.orchestrator import classify_remaining_stubs
    stubs = [(FnLocator("src/nightly.rs", n), (_PROBE,))
             for n in ("fmt", "fmt", "provide")]
    assert len(classify_remaining_stubs(stubs, []).orphaned) == 3


def test_a_reachable_fn_hollow_upstream_too_is_not_a_defect() -> None:
    """Second filter. Reproducing a stub the crate ships is not failing to fill
    one; `todo!()` in an unsupported trait method that some cluster compiles and
    no test exercises is the general case."""
    from pipeline_core.application.orchestrator import classify_remaining_stubs
    stubs = [(FnLocator("src/api.rs", "unsupported"), ()),
             (FnLocator("src/api.rs", "real"), ())]
    verdict = classify_remaining_stubs(
        stubs, [_STABLE], reference_hollow=[("src/api.rs", "unsupported")])
    assert [loc.fn_name for loc in verdict.orphaned] == ["real"]
    assert [loc.fn_name for loc in verdict.upstream_hollow] == ["unsupported"]


def test_orphaned_stubs_subtracts_the_reference_set() -> None:
    assert orphaned_stubs([("a.rs", "f"), ("a.rs", "g")], [("a.rs", "f")]) \
        == frozenset({("a.rs", "g")})


# --- classify_failure must read CARGO, not what a test printed ------------


_TRYBUILD_OUTPUT = """\
   Compiling anyhow v1.0.104 (/tmp/anyhow)
    Finished `test` profile [unoptimized + debuginfo] target(s)
     Running tests/compiletest.rs (target/debug/deps/compiletest-abc)

running 1 test
error[E0599]: the method `anyhow_kind` exists for reference `&Error`, but its \
trait bounds were not satisfied
 --> tests/ui/no-impl.rs:7:13
error[E0599]: the method `anyhow_kind` exists for reference `&Error`, but its \
trait bounds were not satisfied
test ui ... FAILED

failures:

---- ui stdout ----
mismatch

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_a_diagnostic_a_test_printed_is_not_a_build_failure() -> None:
    """THE misroute. `error[E0599` appears twice in the `ui` output, both past
    the `running 1 test` line — measured on the live tree at lines 3035 and
    3071 of 3122, with the only `running` line at 1513. Reading them as a
    compile failure handed TASK 4 the prompt "The implementation failed to
    COMPILE this iteration — the tests could not even run", on a run where cargo
    had just reported 128 of 129 tests passing."""
    from pipeline_core.domain.failure_classification import (
        FailureCategory,
        classify_failure,
    )
    assert classify_failure(_TRYBUILD_OUTPUT) is not FailureCategory.BUILD


def test_a_real_compile_failure_is_still_a_build_failure() -> None:
    from pipeline_core.domain.failure_classification import (
        FailureCategory,
        classify_failure,
    )
    out = ("   Compiling anyhow v1.0.104\n"
           "error[E0425]: cannot find value `x` in this scope\n"
           "error: aborting due to 1 previous error\n")
    assert classify_failure(out) is FailureCategory.BUILD


def test_a_test_target_that_did_not_build_runs_nothing_and_is_build() -> None:
    """Measured on cargo 1.95.0 / 1.97.0-nightly (2026-09-07): under
    `--no-fail-fast` a compile failure in ANY target runs NO binary — zero
    `Running`, zero `running N tests` — so the driver's own wording is in the
    build phase, where every signal is read. This replaces a constructed
    stream that put the driver line AFTER a `test result:` trailer; cargo does
    not print that shape."""
    from pipeline_core.domain.failure_classification import (
        FailureCategory,
        classify_failure,
    )
    out = ("   Compiling anyhow v1.0.104 (/w)\n"
           "error: expected one of `!` or `::`, found `fn`\n"
           " --> tests/other.rs:1:5\n"
           "error: could not compile `anyhow` (test \"other\")\n"
           "warning: build failed, waiting for other jobs to finish...\n")
    assert classify_failure(out) is FailureCategory.BUILD


def test_a_nested_cargo_build_a_test_ran_is_not_a_build_failure() -> None:
    """The driver's own words after a test announced itself were printed BY a
    test: trybuild's nested build of the crate (without the cluster's `--cfg`)
    relayed `error: could not compile `anyhow` (lib)` into run
    `2131920-86818224`'s sweep with 32 of 32 binaries complete. Trusting that
    wording anywhere in the capture classified it BUILD, the TASK 4 half of the
    same misroute the diagnostic-only rule above already closed."""
    from pipeline_core.domain.failure_classification import (
        FailureCategory,
        classify_failure,
    )
    out = ("   Compiling anyhow v1.0.104 (/w)\n"
           "    Finished `test` profile [unoptimized + debuginfo] target(s)\n"
           "     Running tests/compiletest.rs (target/debug/deps/compiletest-1)\n"
           "\n"
           "running 1 test\n"
           "test ui ...    Compiling anyhow v1.0.104 (/w)\n"
           "error[E0063]: missing field `object_backtrace` in initializer of `ErrorVTable`\n"
           " --> /w/src/error.rs:202:41\n"
           "error: could not compile `anyhow` (lib) due to 1 previous error\n"
           "FAILED\n"
           "\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored\n")
    # Negative control: the anywhere-trusted driver wording IS in the capture.
    assert "error: could not compile" in out
    assert classify_failure(out) is not FailureCategory.BUILD


def test_build_phase_output_stops_at_the_first_announcement() -> None:
    from pipeline_core.domain.test_results import build_phase_output
    kept = build_phase_output(_TRYBUILD_OUTPUT)
    assert "Compiling anyhow" in kept
    assert "error[E0599]" not in kept


# --- nothing left in reach → stop, instead of burning to the plateau cap --


def test_split_reachable_failures_calls_ui_out_of_reach() -> None:
    from pipeline_core.domain.test_results import split_reachable_failures
    reachable, blocked = split_reachable_failures({"ui"}, unrepairable={"ui"})
    assert reachable == frozenset()
    assert blocked == frozenset({"ui"})


def test_a_failure_the_reference_excuses_is_out_of_reach() -> None:
    from pipeline_core.domain.test_results import split_reachable_failures
    reachable, blocked = split_reachable_failures(
        {"ui", "test_boxed_str"}, unrepairable={"ui"}, excused={"test_boxed_str"})
    assert reachable == frozenset()
    assert blocked == frozenset({"ui", "test_boxed_str"})


def test_any_reachable_failure_keeps_the_loop_running() -> None:
    """Strictly a stopping rule: it may end a loop early, never continue one."""
    from pipeline_core.domain.test_results import split_reachable_failures
    reachable, _ = split_reachable_failures({"ui", "test_root_cause"},
                                            unrepairable={"ui"})
    assert reachable == frozenset({"test_root_cause"})


def test_module_loop_stops_without_calling_task4() -> None:
    """TASK 4 is `FailureAnalysisStep`. On 2026-08-06 every call it made was
    spent on `ui`: iteration 1's genesis update was +64 lines of which the new
    rules 10 and 16 and the `[UI-SNAPSHOT]` note were all three about that one
    test, and rule 16 told the model to treat diagnostic formatting differences
    as non-failures — which cannot move a verdict decided by cargo's exit code.
    Seven model calls and 13 min 06 s per module iteration, with
    `MODULE_PLATEAU_ABORT=10` between it and a guaranteed exit 1."""
    from pipeline_core.application.reconstruct.module_reconstructor import (
        ModuleReconstructor,
    )
    calls: list[str] = []

    class _Analysis:
        def run(self, *a: object, **k: object) -> object:
            calls.append("task4")
            raise AssertionError("TASK 4 must not be called when nothing is in reach")

    class _Step:
        def run(self, *a: object, **k: object) -> object:
            return SimpleNamespace(locked=False, failure_type="test",
                                   test_output="failures:\n    ui\n",
                                   end_verdict=None)

    class _Tel:
        def log(self, *a: object, **k: object) -> None: ...
        def emit(self, *a: object, **k: object) -> None: ...

    rec = ModuleReconstructor(
        step=_Step(), analysis=_Analysis(), telemetry=_Tel(), plateau_abort=10,
        out_of_reach=lambda _ctx, _out: "ui is a compile-fail harness")
    result = rec.run(SimpleNamespace(module_name="anyhow", project_dir=Path(".")),
                     total_iters=0, coverage_strategy="")
    assert calls == []
    assert result.converged is False
    assert "compile-fail harness" in result.stopped_because


# --- the floor, end to end through run_clusters_strict --------------------


_CARGO_OUT = """\
     Running tests/test_a.rs (target/debug/deps/test_a-1)

running 2 tests
test alpha ... ok
test beta ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out

     Running tests/compiletest.rs (target/debug/deps/compiletest-2)

running 1 test
test ui ... FAILED

failures:
    ui

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


def _deps(reference: object = None, out: str = _CARGO_OUT) -> object:
    from pipeline_core.application.tasks.task3 import Task3Deps
    return Task3Deps(
        phase=None, load_baked_clusters=lambda p: [],
        task3_legacy_single=lambda *a, **k: (False, ""),
        cargo_env=lambda: {"PATH": os.environ.get("PATH", ""), "FAKE_OUT": out},
        parse_cmd=lambda cmd: ["sh", "-c", 'printf "%s" "$FAKE_OUT"'],
        count_active_tests_for_cluster=lambda p, c: 3,
        store_prompt_content=lambda s: "", task3_inline_retry=lambda **k: (False, ""),
        read=lambda p: "", write=lambda p, s: None, test_timeout=30,
        reference=lambda _p: reference)


def _cluster(name: str = "nightly") -> SimpleNamespace:
    return SimpleNamespace(name=name, toolchain="", env_extra={})


# --- the guardrail: an oracle, never content ------------------------------


def test_the_reference_never_lives_inside_the_project_directory() -> None:
    """Everything that builds a prompt sweeps inside `project_dir`
    (`collect_source_context`, `load_api_surface`, the artifact walks). The
    worktree is pristine upstream source IN FULL, so a sibling rather than a
    child is what makes "a prompt reads the original" structurally impossible
    instead of merely discouraged.

    Also pinned ABSOLUTE: `git worktree add` runs with `cwd=project_dir` and
    resolves a relative destination against THAT, so a relative path checks the
    pristine tree out INSIDE the project directory — observed on the first live
    run of this code, which put it at `tmp/anyhow/tmp/.reference_anyhow/tree`.
    """
    from pipeline_core.application.reference_run import reference_dir
    project = Path("tmp/anyhow")
    d = reference_dir(project)
    assert d.is_absolute()
    assert project.resolve() not in d.parents
    assert d.name == ".reference_anyhow"


def test_no_prompt_builder_imports_the_reference() -> None:
    """The moment a reference measurement reaches a prompt, the pipeline stops
    reconstructing and starts copying, and every score it has ever recorded
    becomes meaningless. `reference.json` holds a pass/fail vector and a set of
    fn names — no bodies, no source — but the cheapest way to keep it that way
    is for the prompt layer never to import the module at all."""
    prompts = REPO / "pipeline_core" / "prompts"
    offenders = [
        p.relative_to(REPO) for p in prompts.rglob("*.py")
        if "reference_run" in p.read_text(encoding="utf-8")
        or "reference_floor" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_without_a_reference_the_criterion_stays_absolute(tmp_path: Path) -> None:
    """The pre-existing behaviour, unchanged: one unrepairable failure holds
    `all_pass` false, which is what ran ten module iterations at a guaranteed
    exit 1."""
    from pipeline_core.application.tasks.task3 import run_clusters_strict
    all_pass, _out, results = run_clusters_strict(
        tmp_path, [_cluster()], _deps(reference=None))
    assert all_pass is False
    assert results[0]["status"] == "fail"


def test_the_floor_passes_a_cluster_whose_only_failure_the_original_shares(
        tmp_path: Path) -> None:
    from pipeline_core.application.tasks.task3 import run_clusters_strict
    ref = ReferenceMeasurement(key="k", clusters={"nightly": ClusterReference(
        cluster="nightly", passed=frozenset({_A, _B}), failed=frozenset({_UI}),
        announced=3, reported=3)})
    all_pass, _out, results = run_clusters_strict(
        tmp_path, [_cluster()], _deps(reference=ref))
    assert all_pass is True
    assert results[0]["status"] == "pass"
    assert results[0]["floor_met"] is True


def test_the_floor_does_not_excuse_a_failure_the_original_passes(
        tmp_path: Path) -> None:
    """Same tree, same output — a reference that PASSES `ui` keeps the verdict
    at fail. This is what makes the mechanism empirical rather than a
    hand-maintained taxonomy of unrepairable tests: on a toolchain whose
    diagnostics match the checked-in fixtures, `ui` stops being excused with no
    code change at all."""
    from pipeline_core.application.tasks.task3 import run_clusters_strict
    ref = ReferenceMeasurement(key="k", clusters={"nightly": ClusterReference(
        cluster="nightly", passed=frozenset({_A, _B, _UI}), announced=3,
        reported=3)})
    all_pass, _out, results = run_clusters_strict(
        tmp_path, [_cluster()], _deps(reference=ref))
    assert all_pass is False
    assert results[0]["status"] == "fail"


def test_the_floor_never_upgrades_an_incomplete_cluster(tmp_path: Path) -> None:
    """A crashed binary is not something the reference can excuse: it says
    nothing about whether THIS tree's binaries survived. The announcement guard
    keeps its own authority."""
    from pipeline_core.application.tasks.task3 import run_clusters_strict
    silent = _CARGO_OUT.replace(
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; "
        "0 filtered out\n", "")
    ref = ReferenceMeasurement(key="k", clusters={"nightly": ClusterReference(
        cluster="nightly", passed=frozenset({_A, _B}), failed=frozenset({_UI}),
        announced=3, reported=3)})
    all_pass, _out, results = run_clusters_strict(
        tmp_path, [_cluster()], _deps(reference=ref, out=silent))
    assert all_pass is False
    assert results[0]["status"] == "incomplete"


def test_floor_verdict_reason_names_unmet_and_unexcused() -> None:
    """The reason string is what the done-criterion logs; both halves of a
    miss must appear, and a long list is capped."""
    many = frozenset(("bin", f"t{i}") for i in range(8))
    v = FloorVerdict(cluster="stable", unmet=frozenset({("a", "t")}),
                     unexcused_failures=frozenset({("b", "u")}))
    assert "do not pass here" in v.reason and "does not excuse" in v.reason
    long = FloorVerdict(cluster="stable", unmet=many)
    assert "…" in long.reason
    healthy = ClusterReference(
        cluster="stable", passed=frozenset({("a", "t")}),
        announced=1, reported=1)
    assert healthy.unhealthy_reason == ""
    from types import SimpleNamespace
    _p, _f, _i, incomplete = pairs_of([
        SimpleNamespace(label="bin", passed=(), failed=(), ignored=(),
                        completed=False)])
    assert "bin" in incomplete
