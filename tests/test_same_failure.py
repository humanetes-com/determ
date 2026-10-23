"""A red test the original also fails is excused only by the same failure.

Run `391506-3403a6a7` (2026-10-22) ended green with one red test, trybuild's
`ui` under `nightly`, excused because the original's measurement failed a
test of that name. The fixtures are that test run alone, from its test
binary, in both trees on this machine the same day (the original from
`git archive task0c-augmented-anyhow`, the reconstruction from a `cp -rL` of
the published tree), with the scratch paths replaced by `/work/original`,
`/work/rebuilt` and `/home/someone`. Raw, they differ (123 against 1,341
lines: the reconstruction's build printed 1,218 lines of cfg warnings, and
the two resolved different trybuild versions); normalized, they are the same
98 lines. The diverging cases are the same rebuilt capture with one fact
changed, and the negative control is the name-only rule this replaces,
which excuses them.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from pipeline_core.application import reference_run
from pipeline_core.application.failure_reading import read_one
from pipeline_core.application.tasks.task3 import cluster_outputs
from pipeline_core.domain.reference_floor import (
    ClusterReference,
    ReferenceMeasurement,
    judge_against_reference,
)
from pipeline_core.domain.same_failure import (
    FailedReading,
    FailureSignature,
    FailureToRead,
    ReferenceFailure,
    signature_of,
)
from pipeline_core.domain.world_floor import excused_everywhere, failures_to_read

FIX = Path(__file__).parent / "fixtures" / "same_failure"
ORIGINAL = (FIX / "ui_original.txt").read_text(encoding="utf-8")
REBUILT = (FIX / "ui_rebuilt.txt").read_text(encoding="utf-8")
UI = ("tests/compiletest.rs", "ui")
A = ("tests/a.rs", "alpha")


def _orig_sig(text: str = ORIGINAL) -> FailureSignature:
    return signature_of(text, roots=["/work/original", "/home/someone"])


def _ours_sig(text: str = REBUILT) -> FailureSignature:
    return signature_of(text, roots=["/work/rebuilt", "/home/someone"])


def _reference(first: FailureSignature | None,
               second: FailureSignature | None = None) -> ClusterReference:
    return ClusterReference(
        cluster="nightly", passed=frozenset({A}),
        announced=2, reported=2, rc=101,
        failures={UI: ReferenceFailure(first, first if second is None else second)})


def _judge(ref: ClusterReference, ours: FailureSignature | None, *, read: bool = True):  # type: ignore[no-untyped-def]
    reading = FailedReading(pairs=frozenset({UI}),
                            signatures={UI: ours} if read else {})
    return judge_against_reference(ref, frozenset({A}), reading)


def _old_rule_excuses(ref: ClusterReference) -> bool:
    """The rule this replaced: a failure is excused when its name is in the
    reference's failed or skipped set."""
    return not (frozenset({UI}) - (ref.failed | ref.ignored))


# --- the recorded pair -------------------------------------------------------


def test_the_recorded_ui_runs_are_one_failure_after_normalization() -> None:
    assert ORIGINAL != REBUILT
    assert FailureSignature(ORIGINAL) != FailureSignature(REBUILT)
    assert _orig_sig() == _ours_sig()
    kept = _ours_sig().text
    assert "test tests/ui/no-impl.rs ... mismatch" in kept
    assert "EXPECTED:" in kept and "ACTUAL OUTPUT:" in kept
    assert "unexpected `cfg` condition name" not in kept


def test_the_same_failure_is_excused() -> None:
    v = _judge(_reference(_orig_sig()), _ours_sig())
    assert v.met
    assert v.excused == frozenset({UI})
    assert not v.excused_by_name and not v.diverged


def test_a_different_printed_path_in_the_same_case_is_not_excused() -> None:
    """The shape a dropped re-export would print: the same case mismatches,
    with another path in its ACTUAL block."""
    changed = REBUILT.replace("anyhow::kind::TraitKind", "anyhow::kind_moved::TraitKind")
    assert changed != REBUILT
    ref = _reference(_orig_sig())
    v = _judge(ref, _ours_sig(changed))
    assert not v.met
    assert v.diverged == frozenset({UI})
    assert "kind_moved" in v.divergence[UI]
    assert "not the way the original fails" in v.reason
    assert _old_rule_excuses(ref)  # negative control: the name-only rule


def test_another_case_of_the_same_harness_failing_is_not_excused() -> None:
    """One libtest test, `ui`, fails either way; the case that fails moved."""
    changed = REBUILT.replace("tests/ui/must-use.rs\x1b[0m ... \x1b[0m\x1b[32mok",
                              "tests/ui/must-use.rs\x1b[0m ... \x1b[0m\x1b[1m\x1b[31mmismatch")
    assert changed != REBUILT
    ref = _reference(_orig_sig())
    v = _judge(ref, _ours_sig(changed))
    assert not v.met and v.diverged == frozenset({UI})
    assert _old_rule_excuses(ref)


def test_a_failure_that_was_not_read_here_is_not_excused() -> None:
    v = _judge(_reference(_orig_sig()), None)
    assert not v.met and "could not be read" in v.divergence[UI]
    v = _judge(_reference(_orig_sig()), None, read=False)
    assert not v.met and v.diverged == frozenset({UI})


def test_the_divergence_never_prints_the_originals_side() -> None:
    """The original's output can quote the original's source; only this
    tree's line is logged."""
    theirs = FailureSignature("a\noriginal-only text\nc")
    mine = FailureSignature("a\nrebuilt text\nc")
    diff = mine.first_difference(theirs)
    assert "rebuilt text" in diff and "original-only" not in diff


def test_an_original_failure_that_does_not_reproduce_is_excused_by_name_and_said() -> None:
    v = _judge(_reference(_orig_sig(), FailureSignature("another reading")),
               FailureSignature("anything"))
    assert v.met
    assert v.excused_by_name == frozenset({UI})
    assert "excused by name only" in v.reason


def test_a_reference_with_no_reading_excuses_by_name() -> None:
    ref = ClusterReference(cluster="nightly", passed=frozenset({A}),
                           failures=dict.fromkeys(frozenset({UI}), ReferenceFailure()), announced=2, reported=2)
    v = _judge(ref, None, read=False)
    assert v.met and v.excused_by_name == frozenset({UI})


def test_a_test_the_original_skipped_is_excused() -> None:
    ref = ClusterReference(cluster="stable", passed=frozenset({A}),
                           ignored=frozenset({UI}), announced=2, reported=2)
    v = _judge(ref, None, read=False)
    assert v.met and v.excused == frozenset({UI}) and not v.excused_by_name


# --- the normalization rules, one each ---------------------------------------


def test_a_warning_diagnostic_is_dropped_through_its_blank_line() -> None:
    text = ("test x ... FAILED\nwarning: unused import\n --> src/a.rs:1:5\n"
            "  |\n1 | use b;\n\nwarning: `c` (lib) generated 1 warning\n\n"
            "error: boom")
    assert signature_of(text).text == "test x ... FAILED\nerror: boom"


def test_a_dependency_path_keeps_the_crate_and_file_only() -> None:
    a = signature_of("panicked at /h/.cargo/registry/src/index-1/dep-1.0.122/src/run.rs:103:13:")
    b = signature_of("panicked at /h/.cargo/registry/src/index-1/dep-1.0.120/src/run.rs:99:1:")
    assert a == b and a.text == "panicked at $REGISTRY/dep/src/run.rs:"


def test_a_location_in_the_crates_own_source_drops_its_line() -> None:
    assert (signature_of("panicked at src/error.rs:148:5:")
            == signature_of("panicked at src/error.rs:301:9:"))
    assert (signature_of("panicked at tests/t.rs:10:5:")
            != signature_of("panicked at tests/t.rs:11:5:"))


def test_thread_ids_durations_cargo_status_and_roots_are_dropped() -> None:
    a = signature_of("   Compiling dep v1.0.1\nthread 'ui' (123) panicked\n"
                     "finished in 2.34s\n/r1/x", roots=["/r1"])
    b = signature_of("   Compiling dep v1.0.2\nthread 'ui' (456) panicked\n"
                     "finished in 9.00s\n/r2/x", roots=["/r2"])
    assert a == b


# --- reading this tree's failure ---------------------------------------------


def _script(tmp_path: Path, body: str) -> Path:
    exe = tmp_path / "fake_test_binary"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_a_failure_is_read_from_the_test_run_alone(tmp_path: Path) -> None:
    """stdout and stderr both: the harness's report lands on stderr."""
    exe = _script(tmp_path, 'echo "test t ... FAILED"; echo "case 3 mismatch" >&2; '
                            'echo "$CARGO_MANIFEST_DIR" >&2; echo "$@" >&2; exit 101\n')
    f = FailureToRead(pair=("tests/t.rs", "t"), label="tests/t.rs", name="t",
                      exe_path=str(exe))
    sig = read_one(f, cwd=tmp_path, env=dict(os.environ), timeout=30, cached=False)
    assert sig is not None
    assert "case 3 mismatch" in sig.text
    assert "--exact t --test-threads=1" in sig.text
    assert "$ROOT" in sig.text  # CARGO_MANIFEST_DIR is the tree, normalized


def test_a_test_that_passes_alone_gives_no_reading(tmp_path: Path) -> None:
    exe = _script(tmp_path, "exit 0\n")
    f = FailureToRead(pair=("tests/t.rs", "t"), label="tests/t.rs", name="t",
                      exe_path=str(exe))
    assert read_one(f, cwd=tmp_path, env=dict(os.environ), timeout=30) is None


def test_a_test_with_no_executable_is_read_from_its_failure_block(tmp_path: Path) -> None:
    f = FailureToRead(pair=("Doc-tests c", "x"), label="Doc-tests c", name="x",
                      exe_path=None, body="error[E0425]: cannot find value")
    sig = read_one(f, cwd=tmp_path, env={}, timeout=30)
    assert sig is not None and "E0425" in sig.text
    assert read_one(FailureToRead(pair=("b", "y"), label="b", name="y",
                                  exe_path=None), cwd=tmp_path, env={}, timeout=30) is None


_CAPTURE = """
running 2 tests
test ui ... FAILED
test other ... FAILED

failures:

---- ui stdout ----
1 of 7 tests failed

---- other stdout ----
boom

failures:
    other
    ui

test result: FAILED. 0 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.10s

--- stderr ---
     Running tests/compiletest.rs (target/debug/deps/compiletest-0123abcd)
"""


def test_failures_to_read_names_the_executable_and_the_libtest_name() -> None:
    todo = failures_to_read(None, _CAPTURE)
    by_name = {f.name: f for f in todo}
    assert set(by_name) == {"ui", "other"}
    assert by_name["ui"].exe_path == "target/debug/deps/compiletest-0123abcd"
    assert by_name["ui"].pair == UI and by_name["ui"].body == "1 of 7 tests failed"
    only = failures_to_read(_reference(None), _CAPTURE)
    assert [f.name for f in only] == ["ui"]


# --- the consumers -------------------------------------------------------------


def test_a_failure_excused_in_one_cluster_is_not_excused_where_it_also_fails() -> None:
    """The out-of-reach stop used to union the original's failed names over
    every cluster."""
    nightly = judge_against_reference(
        _reference(_orig_sig()), frozenset({A}),
        FailedReading(pairs=frozenset({UI}), signatures={UI: _ours_sig()}))
    assert nightly.excused == frozenset({UI})
    stable_capture = _CAPTURE.replace("test other ... FAILED", "test other ... ok")
    assert excused_everywhere({"nightly": (_CAPTURE, nightly),
                               "stable": (stable_capture, None)}) == frozenset()
    assert excused_everywhere({"nightly": (_CAPTURE, nightly)}) == frozenset({"ui"})


def test_task3s_aggregated_output_splits_back_per_cluster() -> None:
    agg = ("=== cluster: nightly (rc=101, fail, 1/2 passed) ===\nN\n\n"
           "=== cluster: stable (rc=0, pass, 2/2 passed) ===\nS\n")
    parts = cluster_outputs(agg)
    assert parts["nightly"].strip() == "N" and parts["stable"].strip() == "S"


def test_the_reference_store_keeps_both_readings(tmp_path: Path) -> None:
    ref = ReferenceMeasurement(key="k", clusters={
        "nightly": _reference(_orig_sig(), FailureSignature("second"))})
    raw = json.loads(json.dumps(reference_run._to_json(ref)))
    back = reference_run._from_json(raw)
    assert back is not None
    rf = back.clusters["nightly"].failures[UI]
    assert rf.first == _orig_sig() and rf.second == FailureSignature("second")
    assert not rf.stable
    raw["schema"] = 2
    assert reference_run._from_json(raw) is None  # a name-only store re-measures


# --- the two name-only paths the 2026-10-22 review found ----------------------


def test_a_named_executable_that_is_gone_is_unread_not_signed_from_its_block(
        tmp_path: Path) -> None:
    """The block is `1 of 7 tests failed` for any failing case of a harness;
    signing it would excuse another failure under the same name. Against a
    stable reference an unread failure diverges."""
    f = FailureToRead(pair=UI, label="tests/compiletest.rs", name="ui",
                      exe_path=str(tmp_path / "gone"), body="1 of 7 tests failed")
    assert read_one(f, cwd=tmp_path, env={}, timeout=30) is None
    v = _judge(_reference(_orig_sig()), read_one(f, cwd=tmp_path, env={}, timeout=30))
    assert not v.met and v.diverged == frozenset({UI})


def test_a_failed_test_cannot_be_recorded_without_its_reading() -> None:
    """`failed` is the key set of `failures`: there is no field to name a
    failure in and leave its reading out."""
    import pytest
    with pytest.raises(TypeError):
        ClusterReference(cluster="c", failed=frozenset({UI}))  # type: ignore[call-arg]
    ref = ClusterReference(cluster="c", failures={UI: ReferenceFailure()})
    assert ref.failed == frozenset({UI})


def test_a_stored_failure_with_no_reading_entry_re_measures() -> None:
    raw = json.loads(json.dumps(reference_run._to_json(ReferenceMeasurement(
        key="k", clusters={"nightly": _reference(_orig_sig())}))))
    assert reference_run._from_json(raw) is not None
    raw["clusters"]["nightly"]["failures"] = []
    assert reference_run._from_json(raw) is None
