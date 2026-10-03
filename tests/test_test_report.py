"""Blame-partition pins — structured test-run report + blame partition (pure domain)."""
from __future__ import annotations

from pipeline_core.domain.models import FnSite
from pipeline_core.domain.test_report import (
    BlameVerdict,
    FailureExcerpt,
    FailureKind,
    TestRunReport,
    U2Decision,
    U2Verdict,
    cap_at_line_boundary,
    parse_test_output,
    partition_blame,
    self_aborted_tests,
)

# --- excerpt discipline -------------------------------------------------- #

def test_cap_at_line_boundary_keeps_whole_lines() -> None:
    text = "line-one\nline-two\nline-three\n"
    out = cap_at_line_boundary(text, 15)
    assert "\n" not in out.split("\n", 1)[0][:0] or True   # sanity
    assert not out.startswith("ne-")     # never starts mid-line
    assert out.endswith("line-three\n")


def test_failure_excerpt_keeps_all_names_caps_bodies() -> None:
    names = tuple(f"test_{i}" for i in range(30))
    bodies = (("test_0", "x" * 400 + "\nLAST LINE"),)
    rendered = FailureExcerpt(names, bodies).render(cap=50)
    for n in names:                      # every name intact, none truncated
        assert n in rendered
    assert "test_29" in rendered
    assert "LAST LINE" in rendered       # body tail kept, line-boundary cut

# --- fixtures captured from real cargo output ------------------------------- #

# 2021+ toolchain, multi-failure: a stub panic + an assertion failure + a pass.
STABLE_MULTI = """\
running 3 tests
test tests::test_pass ... ok
test tests::test_stub ... FAILED
test tests::test_assert ... FAILED

failures:

---- tests::test_stub stdout ----
thread 'tests::test_stub' panicked at src/error.rs:42:9:
not yet implemented
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

---- tests::test_assert stdout ----
thread 'tests::test_assert' panicked at src/chain.rs:8:5:
assertion `left == right` failed
  left: 1
  right: 2

failures:
    tests::test_assert
    tests::test_stub

test result: FAILED. 1 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out
"""

# pre-2021 single-line panic form.
NIGHTLY_OLD = """\
running 1 test
test t_old ... FAILED

failures:

---- t_old stdout ----
thread 't_old' panicked at 'not yet implemented', src/error.rs:5:9
note: run with `RUST_BACKTRACE=1` ...

failures:
    t_old

test result: FAILED. 0 passed; 1 failed; 0 ignored
"""

# single-test run with NO `---- stdout ----` header (old cargo).
NO_HEADER = """\
running 1 test
test only ... FAILED
thread 'only' panicked at src/error.rs:12:1:
not yet implemented
"""


def test_parse_stable_multi() -> None:
    r = parse_test_output(STABLE_MULTI)
    assert r.passed == frozenset({"tests::test_pass"})
    assert r.failed_names == frozenset({"tests::test_stub", "tests::test_assert"})
    by = {f.test_name: f for f in r.failures}
    assert by["tests::test_stub"].kind is FailureKind.STUB_PANIC
    assert by["tests::test_stub"].panic_file == "src/error.rs"
    assert by["tests::test_stub"].panic_line == 42
    assert by["tests::test_assert"].kind is FailureKind.ASSERTION
    assert by["tests::test_assert"].panic_file == "src/chain.rs"
    assert by["tests::test_assert"].panic_line == 8


def test_parse_pre2021_form() -> None:
    r = parse_test_output(NIGHTLY_OLD)
    (f,) = r.failures
    assert f.kind is FailureKind.STUB_PANIC
    assert (f.panic_file, f.panic_line, f.panic_col) == ("src/error.rs", 5, 9)


def test_parse_no_header_single_test() -> None:
    r = parse_test_output(NO_HEADER)
    (f,) = r.failures
    assert f.kind is FailureKind.STUB_PANIC
    assert (f.panic_file, f.panic_line) == ("src/error.rs", 12)


def test_parse_empty() -> None:
    assert parse_test_output("") == TestRunReport(frozenset(), ())


# --- blame partition -------------------------------------------------------- #

def _locate(table: dict[tuple[str, int], FnSite]):
    return lambda f, ln: table.get((f, ln))


def test_partition_external_only() -> None:
    # the only failure is a stub-panic in a fn NOT owned by this unit.
    r = parse_test_output(NIGHTLY_OLD)
    unit = frozenset({FnSite("src/lib.rs", "mine", "")})
    loc = _locate({("src/error.rs", 5): FnSite("src/error.rs", "context", "impl Error")})
    v = partition_blame(r, unit, loc)
    assert v.self_failures == ()
    assert v.blocking_fns == frozenset({FnSite("src/error.rs", "context", "impl Error")})


def test_partition_mixed_keeps_only_self() -> None:
    r = parse_test_output(STABLE_MULTI)
    # this unit OWNS chain.rs::eq (the assertion), NOT error.rs (the stub).
    unit = frozenset({FnSite("src/chain.rs", "eq", "impl PartialEq for Chain")})
    loc = _locate({
        ("src/error.rs", 42): FnSite("src/error.rs", "context", "impl Error"),
        ("src/chain.rs", 8): FnSite("src/chain.rs", "eq", "impl PartialEq for Chain"),
    })
    v = partition_blame(r, unit, loc)
    assert [f.test_name for f in v.self_failures] == ["tests::test_assert"]
    assert v.blocking_fns == frozenset({FnSite("src/error.rs", "context", "impl Error")})


def test_partition_stub_in_own_unit_stays_self() -> None:
    # a stub-panic that resolves to the unit's OWN fn is not external.
    r = parse_test_output(NIGHTLY_OLD)
    own = FnSite("src/error.rs", "context", "impl Error")
    v = partition_blame(r, frozenset({own}), _locate({("src/error.rs", 5): own}))
    assert len(v.self_failures) == 1 and v.external == ()


def test_partition_same_name_sibling_is_external() -> None:
    # THE collision the U2 gate mis-classified: the unit is a `Bool` impl's
    # `not`; the panic is in the FREE `not(cond)` stub — same file, same name,
    # DIFFERENT container. A name-only key would call this the unit's own
    # failure (→ wrongly REJECT); the container disambiguates it as external.
    r = parse_test_output(NIGHTLY_OLD)
    unit = frozenset({FnSite("src/error.rs", "context", "impl Bool for &bool")})
    # panic resolves to the free fn (container "") of the SAME name+file.
    free = FnSite("src/error.rs", "context", "")
    v = partition_blame(r, unit, _locate({("src/error.rs", 5): free}))
    assert v.self_failures == () and v.blocking_fns == frozenset({free})


def test_partition_container_match_is_canonical() -> None:
    # The extractor spaces `& bool`; the unit was built with the compact form.
    # Canonical container equality must still recognise the unit's OWN fn.
    r = parse_test_output(NIGHTLY_OLD)
    unit = frozenset({FnSite("src/error.rs", "context", "impl Bool for &bool")})
    same = FnSite("src/error.rs", "context", "impl Bool for & bool")  # syn spacing
    v = partition_blame(r, unit, _locate({("src/error.rs", 5): same}))
    assert len(v.self_failures) == 1 and v.external == ()


def test_partition_unresolvable_stays_self() -> None:
    # locate returns None (unknown fn) → not subtracted (fail-safe: charge self).
    r = parse_test_output(NIGHTLY_OLD)
    v = partition_blame(r, frozenset(), _locate({}))
    assert len(v.self_failures) == 1 and v.external == ()


# --- U2Verdict factories ---------------------------------------------------- #

def test_u2verdict_factories() -> None:
    assert U2Verdict.accept().decision is U2Decision.ACCEPT
    ext = ((parse_test_output(NIGHTLY_OLD).failures[0],
            FnSite("src/error.rs", "context", "impl Error")),)
    ev = U2Verdict.accept_external_only(ext)
    assert ev.decision is U2Decision.ACCEPT_EXTERNAL_ONLY
    assert ev.blocking_fns == frozenset({FnSite("src/error.rs", "context", "impl Error")})
    rv = U2Verdict.reject(frozenset({"t"}), "detail")
    assert rv.decision is U2Decision.REJECT and rv.self_failures == frozenset({"t"})


def test_blameverdict_default_empty() -> None:
    assert BlameVerdict().self_failures == () and BlameVerdict().external == ()


# --- evidence-free failures never convict ----------------------------------- #

# A libtest run whose process aborted mid-suite: two tests printed their FAILED
# line before the abort, and the summary that would have carried their
# `---- name stdout ----` sections never ran. A third test in another binary
# completed normally and DID print its stub-panic section.
ABORTED_RUN = """\
running 4 tests
test test_altdebug ... FAILED
test test_altdisplay ... FAILED

running 2 tests
test test_iter ... FAILED

failures:

---- test_iter stdout ----
thread 'test_iter' panicked at src/error.rs:5:9:
not implemented

failures:
    test_iter

test result: FAILED. 0 passed; 1 failed; 0 ignored
"""


def test_failure_without_captured_output_is_no_verdict() -> None:
    r = parse_test_output(ABORTED_RUN)
    assert {f.test_name for f in r.failures} == {
        "test_altdebug", "test_altdisplay", "test_iter"}
    unit = frozenset({FnSite("src/lib.rs", "mine", "")})
    loc = _locate({("src/error.rs", 5): FnSite("src/error.rs", "ctx", "impl Error")})
    v = partition_blame(r, unit, loc)
    # the two body-less failures neither convict nor exonerate
    assert {f.test_name for f in v.no_verdict} == {"test_altdebug", "test_altdisplay"}
    assert v.self_failures == ()
    assert v.blocking_fns == frozenset({FnSite("src/error.rs", "ctx", "impl Error")})


def test_no_verdict_failure_is_not_charged_even_without_externals() -> None:
    # every failure is evidence-free → nothing to reject on at all.
    r = parse_test_output("running 2 tests\ntest test_a ... FAILED\n"
                          "test test_b ... FAILED\n")
    v = partition_blame(r, frozenset(), _locate({}))
    assert v.self_failures == () and v.external == ()
    assert {f.test_name for f in v.no_verdict} == {"test_a", "test_b"}


def test_render_u2_reject_flags_unjudged_tests() -> None:
    from pipeline_core.application.reconstruct.cargo_runner import _render_u2_reject
    r = parse_test_output(ABORTED_RUN)
    unit = frozenset({FnSite("src/error.rs", "ctx", "impl Error")})
    v = partition_blame(r, unit, _locate(
        {("src/error.rs", 5): FnSite("src/error.rs", "ctx", "impl Error")}))
    assert len(v.self_failures) == 1        # test_iter resolves INTO the unit
    out = _render_u2_reject(v, err_cap=4000)
    assert "not implemented" in out         # the one real body is shown
    assert "test process aborted" in out    # the unjudged pair is disclosed
    assert "test_altdebug" not in out.split("Fix the BEHAVIOR")[0]


def test_u2verdict_carries_no_verdict_names() -> None:
    assert U2Verdict.accept_external_only((), frozenset({"t"})).no_verdict == frozenset({"t"})
    assert U2Verdict.reject(frozenset({"a"}), "d", frozenset({"t"})).no_verdict == frozenset({"t"})
    assert U2Verdict.accept().no_verdict == frozenset()


def test_blameverdict_no_verdict_defaults_empty() -> None:
    assert BlameVerdict().no_verdict == ()


# --- comparative banking for joint routes (JointImprovement) -----------------
from pipeline_core.domain.test_report import (  # noqa: E402
    assess_joint_improvement,
)


def _reject(self_failures: set[str],
            no_verdict: frozenset[str] = frozenset(),
            self_passed: frozenset[str] = frozenset()) -> U2Verdict:
    return U2Verdict.reject(frozenset(self_failures), "detail",
                            no_verdict=no_verdict, self_passed=self_passed)


def test_joint_banks_on_positive_pass_evidence() -> None:
    ji = assess_joint_improvement(
        attributed=frozenset({"a", "b", "c"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset(),
        verdict=_reject({"a"}, self_passed=frozenset({"b", "c"})))
    assert ji.baseline_failing == {"a", "b", "c"}
    assert ji.resolved == {"b", "c"}
    assert ji.banks
    assert "3→1" in ji.summary


def test_joint_absence_is_not_resolution() -> None:
    # `b` appears in NEITHER reading (stale attribution, cfg drift, crashed
    # scoped binary): unmeasured, never "fixed" — no bank without positive
    # pass evidence.
    ji = assess_joint_improvement(
        attributed=frozenset({"a", "b"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset(),
        verdict=_reject({"a"}))
    assert ji.resolved == frozenset()
    assert not ji.banks


def test_joint_no_bank_when_nothing_resolved() -> None:
    ji = assess_joint_improvement(
        attributed=frozenset({"a", "b"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset(),
        verdict=_reject({"a", "b"}))
    assert not ji.banks


def test_joint_no_bank_on_new_self_failure() -> None:
    # `c` was passing at baseline and fails now: improvement elsewhere cannot
    # buy a regression (the global gate reverts it anyway).
    ji = assess_joint_improvement(
        attributed=frozenset({"a", "b", "c"}),
        baseline_passed=frozenset({"c"}),
        baseline_ignored=frozenset(),
        verdict=_reject({"c"}, self_passed=frozenset({"a", "b"})))
    assert ji.new_failures == {"c"}
    assert not ji.banks


def test_joint_aborted_previously_failing_is_not_resolved() -> None:
    # `b` printed no verdict (aborted) and was failing before — unmeasured,
    # not progress; `a` passing is what banks.
    ji = assess_joint_improvement(
        attributed=frozenset({"a", "b", "c"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset(),
        verdict=_reject({"c"}, no_verdict=frozenset({"b"}),
                        self_passed=frozenset({"a"})))
    assert ji.resolved == {"a"}
    assert ji.unresolved == {"b", "c"}
    assert ji.banks


def test_joint_baseline_vanished_counts_as_failing_and_can_resolve() -> None:
    # An attributed test absent from baseline passes (its binary crashed at
    # baseline) was certainly not passing; observing it pass now banks.
    ji = assess_joint_improvement(
        attributed=frozenset({"vanished", "red"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset(),
        verdict=_reject({"red"}, self_passed=frozenset({"vanished"})))
    assert ji.baseline_failing == {"vanished", "red"}
    assert ji.resolved == {"vanished"}
    assert ji.banks


def test_joint_ignored_tests_never_pad_the_baseline() -> None:
    # A cfg-ignored test must not sit in baseline_failing looking resolvable.
    ji = assess_joint_improvement(
        attributed=frozenset({"a", "ign"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset({"ign"}),
        verdict=_reject({"a"}))
    assert ji.baseline_failing == {"a"}
    assert not ji.banks


# --- verdicts a body destroyed ---------------------------------------------- #

def test_self_abort_is_the_intersection_of_missing_and_judged() -> None:
    # Silence read in isolation is evidence-free (partition_blame files it as
    # no_verdict); held against a baseline that judged the test, the same
    # silence means the body under test killed the process before it ran.
    assert self_aborted_tests(
        frozenset({"a", "b", "never_ran"}),
        frozenset({"a", "b", "unrelated"})) == frozenset({"a", "b"})


def test_self_abort_ignores_tests_the_baseline_never_judged() -> None:
    # No prior verdict ⇒ nothing was destroyed ⇒ nothing to charge.
    assert self_aborted_tests(frozenset({"a"}), frozenset()) == frozenset()
    assert self_aborted_tests(frozenset(), frozenset({"a"})) == frozenset()


# --- stub_only_crash: attribute an abort to the hollow tree ------------------
#
# Every capture below opens a `running N tests` section that never prints
# its trailer: the judgment is over the ABORTED binaries' own sections, and
# a bare panic line with no section belongs to no binary.

_ABORTED = "running 2 tests\ntest a ... \n"


def test_stub_only_crash_collects_stub_panic_sites_new_style() -> None:
    from pipeline_core.domain.test_report import stub_only_crash
    out = (_ABORTED
           + "thread 'a' panicked at src/error.rs:64:17:\n"
           "not implemented\n"
           "thread 'a' panicked at src/error.rs:80:9:\n"
           "not yet implemented\n")
    assert stub_only_crash(out) == frozenset({("src/error.rs", 64),
                                              ("src/error.rs", 80)})


def test_stub_only_crash_old_style_location() -> None:
    from pipeline_core.domain.test_report import stub_only_crash
    out = _ABORTED + "thread 'a' panicked at 'not implemented', src/error.rs:64:17\n"
    assert stub_only_crash(out) == frozenset({("src/error.rs", 64)})


def test_stub_only_crash_vetoed_by_any_non_stub_panic() -> None:
    # An assertion panic in the same aborted binary is real self-evidence;
    # the mix must not exonerate (the assertion may be what started the
    # unwind that reached the hollow Drop).
    from pipeline_core.domain.test_report import stub_only_crash
    out = (_ABORTED
           + "thread 'a' panicked at tests/t.rs:9:5:\n"
           "assertion failed: boom\n"
           "thread 'a' panicked at src/error.rs:64:17:\n"
           "not implemented\n")
    assert stub_only_crash(out) == frozenset()


def test_stub_only_crash_empty_without_panics() -> None:
    # A segfault prints no panic line: no evidence, no forgiveness.
    from pipeline_core.domain.test_report import stub_only_crash
    assert stub_only_crash(_ABORTED + "error: process didn't exit successfully") == frozenset()
    assert stub_only_crash("error: process didn't exit successfully") == frozenset()
    assert stub_only_crash("") == frozenset()


def test_stub_only_crash_unparseable_panic_line_vetoes() -> None:
    # A `panicked at` line whose location cannot be parsed is unknown
    # evidence; unknown must not exonerate.
    from pipeline_core.domain.test_report import stub_only_crash
    out = (_ABORTED
           + "thread 'a' panicked at somewhere strange\n"
           "thread 'a' panicked at src/error.rs:64:17:\n"
           "not implemented\n")
    assert stub_only_crash(out) == frozenset()


def test_stub_only_crash_ignores_a_panic_line_outside_any_section() -> None:
    # A panic line no binary's section holds is nobody's evidence. Pre-fix
    # the whole text was one capture and this exonerated.
    from pipeline_core.domain.test_report import stub_only_crash
    assert stub_only_crash("thread 'a' panicked at src/error.rs:64:17:\n"
                           "not implemented\n") == frozenset()


# --- the runtime's own double-panic line, and per-binary evidence -----------
#
# The abort of run `2585062-494e630e` (2026-09-07/08), rustc 1.95.0: a test
# panics in a still-stubbed fn, the `Error` it holds is dropped while
# unwinding, `Error::drop` reaches the still-stubbed `vtable()`, the stub
# panics, and the runtime prints ITS OWN `panicked at` line — located in
# `library/core/src/panicking.rs` with the message `panic in a destructor
# during cleanup` — before `thread caused non-unwinding panic. aborting.`
# and SIGABRT. The locator read the runtime line as one more located panic
# with a non-stub message, so the all-or-nothing rule was empty on every
# fill that landed a correct `Own::new`.

_RUSTC_PANICKING = ("/rustc/59807616e1fa2540724bfbac14d7976d7e4a3860/library/"
                    "core/src/panicking.rs:233:5")
_DESTRUCTOR = "panic in a destructor during cleanup"


def test_runtime_double_panic_line_is_recognised_with_and_without_rustc_prefix() -> None:
    from pipeline_core.domain.test_report import _iter_located_panics
    for loc in (_RUSTC_PANICKING, "library/core/src/panicking.rs:233:5",
                "/home/u/.rustup/toolchains/stable/lib/rustlib/src/rust/"
                "library/core/src/panicking.rs:233:5"):
        (p,) = _iter_located_panics(
            f"thread 'x' panicked at {loc}:\n{_DESTRUCTOR}\n")
        assert p is not None and p.is_runtime_abort and not p.is_stub, loc
    # NEGATIVE CONTROLS: a runtime-located panic with another message is a
    # crate panic (an `alloc` overflow); the announcement on a crate line is
    # a test that printed it. Neither is the runtime's abort.
    (q,) = _iter_located_panics(
        "thread 'x' panicked at /rustc/abc/library/alloc/src/raw_vec.rs:24:5:\n"
        "capacity overflow\n")
    assert q is not None and not q.is_runtime_abort
    (r,) = _iter_located_panics(
        f"thread 'x' panicked at src/lib.rs:3:5:\n{_DESTRUCTOR}\n")
    assert r is not None and not r.is_runtime_abort


def test_stub_only_crash_treats_the_runtime_line_as_evidence_not_veto() -> None:
    # The overnight shape: stub panic, then the runtime's announcement.
    from pipeline_core.domain.test_report import crash_panics, stub_only_crash
    out = (_ABORTED
           + "thread 'a' panicked at src/error.rs:669:67:\n"
           "not implemented\n"
           f"thread 'a' panicked at {_RUSTC_PANICKING}:\n"
           f"{_DESTRUCTOR}\n"
           "thread caused non-unwinding panic. aborting.\n")
    assert stub_only_crash(out) == frozenset({("src/error.rs", 669)})
    p = crash_panics(out)
    assert p.runtime_aborts == 1 and p.stub_only and not p.crate_panics
    # The announcement ALONE (no stub panic located) exonerates nothing: it
    # says the abort was a panic, not whose.
    only_rt = (_ABORTED + f"thread 'a' panicked at {_RUSTC_PANICKING}:\n"
               f"{_DESTRUCTOR}\n")
    assert stub_only_crash(only_rt) == frozenset()
    assert not crash_panics(only_rt).stub_only


# The three-binary capture of the overnight `phase_c.fill.reading` shape,
# in the combined layout the pipeline captures (stdout, `--- stderr ---`,
# stderr): `newly_red=[]`, one `newly_crashed` section (context_gen_1 —
# had a pass; aborts on `not implemented` at error.rs:669 plus the
# destructor line), one `crashed_all_red` section (error_gen_3 — 0 passes
# at baseline; an assertion, then the destructor line), one completed
# section with a pre-existing stub failure.
_BASE_245 = (
    "running 3 tests\n"
    "test ctx_a ... ok\n"
    "test ctx_b ... ok\n"
    "test ctx_c ... FAILED\n"
    "test result: FAILED. 2 passed; 1 failed; 0 ignored\n"
    "\n"
    "running 2 tests\n"
    "test eg_a ... FAILED\n"
    "test eg_b ... FAILED\n"
    "test result: FAILED. 0 passed; 2 failed; 0 ignored\n"
    "\n"
    "running 2 tests\n"
    "test lib_a ... ok\n"
    "test lib_b ... FAILED\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
    "\n--- stderr ---\n"
    "     Running tests/context_gen_1.rs (target/debug/deps/context_gen_1-aaaa)\n"
    "     Running tests/error_gen_3.rs (target/debug/deps/error_gen_3-bbbb)\n"
    "     Running tests/lib_gen_1.rs (target/debug/deps/lib_gen_1-cccc)\n"
)
_AFTER_245 = (
    "running 3 tests\n"
    "test ctx_a ... \n"
    "thread 'ctx_a' panicked at src/error.rs:669:67:\n"
    "not implemented\n"
    "\n"
    "running 2 tests\n"
    "test eg_a ... \n"
    "thread 'eg_a' panicked at tests/error_gen_3.rs:50:5:\n"
    "assertion failed: inner.is_some()\n"
    "\n"
    "running 2 tests\n"
    "test lib_a ... ok\n"
    "test lib_b ... FAILED\n"
    "\n"
    "failures:\n"
    "\n"
    "---- lib_b stdout ----\n"
    "thread 'lib_b' panicked at src/fmt.rs:8:5:\n"
    "not implemented\n"
    "\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
    "\n--- stderr ---\n"
    "     Running tests/context_gen_1.rs (target/debug/deps/context_gen_1-aaaa)\n"
    "\n"
    f"thread 'ctx_a' panicked at {_RUSTC_PANICKING}:\n"
    f"{_DESTRUCTOR}\n"
    "thread caused non-unwinding panic. aborting.\n"
    "error: test failed, to rerun pass `--test context_gen_1`\n"
    "     Running tests/error_gen_3.rs (target/debug/deps/error_gen_3-bbbb)\n"
    "\n"
    f"thread 'eg_a' panicked at {_RUSTC_PANICKING}:\n"
    f"{_DESTRUCTOR}\n"
    "thread caused non-unwinding panic. aborting.\n"
    "error: test failed, to rerun pass `--test error_gen_3`\n"
    "     Running tests/lib_gen_1.rs (target/debug/deps/lib_gen_1-cccc)\n"
    "error: test failed, to rerun pass `--test lib_gen_1`\n"
)


def _readings_245():
    from pipeline_core.domain.test_results import (
        TestStateReading,
        parse_binary_results,
    )
    return (TestStateReading(parse_binary_results(_BASE_245)),
            TestStateReading(parse_binary_results(_AFTER_245)))


def test_binary_output_sections_pair_the_stderr_slice_with_its_binary() -> None:
    # Combined layout: the runtime's destructor line is printed on STDERR
    # under the binary's `Running` line, the stub panic on STDOUT under its
    # header; the section holds both, and the completed binary holds
    # neither.
    from pipeline_core.domain.test_results import binary_output_sections
    secs = {s.label: s for s in binary_output_sections(_AFTER_245)}
    assert list(secs) == ["tests/context_gen_1.rs", "tests/error_gen_3.rs",
                          "tests/lib_gen_1.rs"]
    ctx = secs["tests/context_gen_1.rs"]
    assert not ctx.completed
    assert "src/error.rs:669:67" in ctx.text and _DESTRUCTOR in ctx.text
    assert "assertion failed" not in ctx.text
    eg = secs["tests/error_gen_3.rs"]
    assert not eg.completed and "assertion failed" in eg.text
    assert eg.text.count(_DESTRUCTOR) == 1
    lib = secs["tests/lib_gen_1.rs"]
    assert lib.completed and _DESTRUCTOR not in lib.text
    assert "src/fmt.rs:8:5" in lib.text
    # Interleaved layout (no separator): same pairing.
    inter = ("     Running tests/a.rs (t/a-1)\n"
             "running 1 test\ntest x ... ok\n"
             "test result: ok. 1 passed; 0 failed; 0 ignored\n"
             "     Running tests/b.rs (t/b-2)\n"
             "running 1 test\ntest y ... \n"
             "thread 'y' panicked at src/error.rs:669:67:\nnot implemented\n"
             f"thread 'y' panicked at {_RUSTC_PANICKING}:\n{_DESTRUCTOR}\n")
    a, b = binary_output_sections(inter)
    assert (a.label, a.completed) == ("tests/a.rs", True)
    assert (b.label, b.completed) == ("tests/b.rs", False)
    assert _DESTRUCTOR in b.text and _DESTRUCTOR not in a.text


def test_the_overnight_reading_keeps_only_when_judged_over_newly_crashed() -> None:
    # The plan's sentence, computed: `compare_readings` splits the crash;
    # the whole text is empty (the pre-fix path — the runtime line and
    # error_gen_3's assertion veto); every crashed section is still empty
    # (error_gen_3's assertion); the `newly_crashed` section alone yields
    # the vtable stub site, and that is the keep.
    from pipeline_core.domain.test_report import crash_panics, stub_only_crash
    from pipeline_core.domain.test_results import (
        binary_output_sections,
        compare_readings,
    )
    base, after = _readings_245()
    cmp = compare_readings(base, after)
    assert cmp.newly_crashed == ("tests/context_gen_1.rs",)
    assert cmp.crashed_all_red == ("tests/error_gen_3.rs",)
    assert not cmp.newly_red
    assert (cmp.cur_pass >= cmp.base_pass
            and cmp.cur_executed >= cmp.base_executed)  # the crash-only branch
    every = [s.label for s in binary_output_sections(_AFTER_245)]
    assert stub_only_crash(_AFTER_245, crashed=every) == frozenset()
    assert stub_only_crash(_AFTER_245) == frozenset()          # all aborted
    assert stub_only_crash(_AFTER_245, crashed=cmp.newly_crashed) == frozenset(
        {("src/error.rs", 669)})
    p = crash_panics(_AFTER_245, crashed=cmp.newly_crashed)
    assert p.stub_only and p.runtime_aborts == 1 and p.sections == cmp.newly_crashed


def test_a_segfault_or_a_missing_section_in_newly_crashed_still_reverts() -> None:
    from pipeline_core.domain.test_report import crash_panics, stub_only_crash
    from pipeline_core.domain.test_results import compare_readings
    base, after = _readings_245()
    crashed = compare_readings(base, after).newly_crashed
    # No panic at all in the newly-crashed section: undefined behaviour.
    seg = (_AFTER_245
           .replace("thread 'ctx_a' panicked at src/error.rs:669:67:\n"
                    "not implemented\n", "")
           .replace(f"thread 'ctx_a' panicked at {_RUSTC_PANICKING}:\n"
                    f"{_DESTRUCTOR}\n"
                    "thread caused non-unwinding panic. aborting.\n",
                    "(signal: 11, SIGSEGV: invalid memory reference)\n"))
    assert stub_only_crash(seg, crashed=crashed) == frozenset()
    assert crash_panics(seg, crashed=crashed).runtime_aborts == 0
    # A section the capture does not hold is no evidence.
    assert stub_only_crash(_AFTER_245, crashed=("tests/nope.rs",)) == frozenset()
    assert crash_panics(_AFTER_245, crashed=("tests/nope.rs",)).missing == (
        "tests/nope.rs",)
    # The assertion INSIDE the newly-crashed section is the fill's own.
    own = _AFTER_245.replace(
        "thread 'ctx_a' panicked at src/error.rs:669:67:\nnot implemented\n",
        "thread 'ctx_a' panicked at tests/context_gen_1.rs:9:5:\n"
        "assertion failed: boom\n"
        "thread 'ctx_a' panicked at src/error.rs:669:67:\nnot implemented\n")
    assert stub_only_crash(own, crashed=crashed) == frozenset()
    q = crash_panics(own, crashed=crashed)
    assert not q.stub_only and len(q.crate_panics) == 1
    assert q.stub_sites == frozenset({("src/error.rs", 669)})


# --- the passing verdict of a `#[should_panic]` test, and the failure words --- #

SHOULD_PANIC_PASS = """\
running 2 tests
test error::gen_tests_4::test_no_backtrace_fallback - should panic ... ok
test error::gen_tests_4::test_plain ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_should_panic_pass_line_is_a_passed_name() -> None:
    """libtest qualifies a `#[should_panic]` result BEFORE the ` ... `
    separator. The oracle reconciles the expected names against THIS parser's
    `passed`; a whitespace-token walk dropped the qualified line, so a passing
    test was reported crash-vanished on every sweep of runs `626784-4b7c38ba`
    and `902902-daee577d` (2026-08-20) after the qualifier strip had reached
    the three parsers in `test_results` but not this one."""
    from pipeline_core.domain.oracle_ledger import reconcile_sweep
    r = parse_test_output(SHOULD_PANIC_PASS)
    assert r.passed == frozenset({
        "error::gen_tests_4::test_no_backtrace_fallback",
        "error::gen_tests_4::test_plain"})
    oracle = reconcile_sweep(
        2, frozenset({"test_no_backtrace_fallback", "test_plain"}),
        r.passed, r.failed_names, executed_count=2)
    assert oracle.vanished == frozenset() and oracle.intact


def test_passed_names_negative_control_the_token_walk_drops_the_line() -> None:
    """The pre-fix predicate (`toks[2] == "..."`) does not hold on the
    qualified line — that is the defect, reproduced — while the separator
    split reads it."""
    line = "test a::test_x - should panic ... ok"
    assert line.split()[2] != "..."
    assert "a::test_x" in parse_test_output(line + "\n").passed
    # An ignored or failed line is still not a pass.
    assert parse_test_output("test a::t ... ignored\n").passed == frozenset()
    assert parse_test_output("test a::t - should panic ... FAILED\n").passed == frozenset()


def test_failure_evidence_by_test_carries_panic_site_and_assertion() -> None:
    from pipeline_core.domain.test_report import failure_evidence_by_test
    ev = failure_evidence_by_test(STABLE_MULTI)
    assert set(ev) == {"tests::test_stub", "tests::test_assert"}
    a = ev["tests::test_assert"]
    assert "panicked at src/chain.rs:8:5" in a
    assert "assertion `left == right` failed" in a
    assert "left: 1" in a and "right: 2" in a
    # the backtrace hint is noise, not evidence
    assert "RUST_BACKTRACE" not in ev["tests::test_stub"]
    # capped, never empty for a test that left a body
    small = failure_evidence_by_test(STABLE_MULTI, per_test_cap=40)
    assert all(len(v) <= 42 for v in small.values())
    assert failure_evidence_by_test("") == {}


# --- the assertion-position gradient -----------------------------------------
from pipeline_core.domain.assertion_position import (  # noqa: E402
    PositionMove,
    TestKey,
    compare_positions,
)

_BIN = "tests/test_a.rs"


def _reject_at(positions: dict[str, int], binary: str = _BIN) -> U2Verdict:
    """A REJECT carrying the assertion positions of its scoped capture."""
    return U2Verdict.reject(
        frozenset(positions), "detail",
        positions={TestKey(binary, t): ln for t, ln in positions.items()})


def test_a_later_position_in_the_same_test_is_progress_not_a_regression() -> None:
    """A body that passes one more assertion moves the position to a line the
    pre-fill tree did NOT have; order within the test's own body is the
    gradient."""
    k = TestKey(_BIN, "t")
    earlier, later = compare_positions({k: 103}, {k: 105})
    assert earlier == ()
    assert later == (PositionMove(k, 103, 105),)


def test_an_earlier_position_is_a_regression_the_name_set_cannot_see() -> None:
    """Same verdict vector, strictly fewer assertions reached. `new_failures`
    is empty because the test was already red at baseline."""
    ji = assess_joint_improvement(
        attributed=frozenset({"t"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset(),
        verdict=_reject_at({"t": 103}),
        baseline_positions={TestKey(_BIN, "t"): 124})
    assert ji.new_failures == frozenset()
    assert ji.regressed_sites == (PositionMove(TestKey(_BIN, "t"), 124, 103),)
    assert "panic EARLIER" in ji.site_movement
    assert not ji.non_regression


def test_positions_in_two_binaries_do_not_compare() -> None:
    """One name in two binaries is two tests; a key compares only with
    itself."""
    earlier, later = compare_positions(
        {TestKey("tests/test_a.rs", "t"): 90},
        {TestKey("tests/test_b.rs", "t"): 10})
    assert (earlier, later) == ((), ())


def test_a_failure_with_no_position_is_absent_not_defaulted() -> None:
    """A sentinel line number would read as movement in whichever direction the
    sentinel sits."""
    v = U2Verdict.reject(frozenset({"t"}), "d")
    assert v.positions == {}
    assert compare_positions({TestKey(_BIN, "t"): 5}, v.positions) == ((), ())


def test_position_comparison_is_scoped_to_the_units_attributed_tests() -> None:
    """A position that moved in a test this unit does not answer for is
    somebody else's measurement."""
    ji = assess_joint_improvement(
        attributed=frozenset({"mine"}),
        baseline_passed=frozenset(),
        baseline_ignored=frozenset(),
        verdict=_reject_at({"mine": 50, "theirs": 10}),
        baseline_positions={TestKey(_BIN, "mine"): 50,
                            TestKey(_BIN, "theirs"): 99})
    assert ji.regressed_sites == ()      # `theirs` moved earlier; not ours
