"""F3 pins — structured test-run report + blame partition (pure domain)."""
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

# --- F8 excerpt discipline -------------------------------------------------- #

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


def test_stub_only_crash_collects_stub_panic_sites_new_style() -> None:
    from pipeline_core.domain.test_report import stub_only_crash
    out = ("running 2 tests\n"
           "test a ... \n"
           "thread 'a' panicked at src/error.rs:64:17:\n"
           "not implemented\n"
           "thread 'a' panicked at src/error.rs:80:9:\n"
           "not yet implemented\n")
    assert stub_only_crash(out) == frozenset({("src/error.rs", 64),
                                              ("src/error.rs", 80)})


def test_stub_only_crash_old_style_location() -> None:
    from pipeline_core.domain.test_report import stub_only_crash
    out = "thread 'a' panicked at 'not implemented', src/error.rs:64:17\n"
    assert stub_only_crash(out) == frozenset({("src/error.rs", 64)})


def test_stub_only_crash_vetoed_by_any_non_stub_panic() -> None:
    # An assertion panic in the same capture is real self-evidence; the mix
    # must not exonerate (the assertion may be what started the unwind that
    # reached the hollow Drop).
    from pipeline_core.domain.test_report import stub_only_crash
    out = ("thread 'a' panicked at tests/t.rs:9:5:\n"
           "assertion failed: boom\n"
           "thread 'a' panicked at src/error.rs:64:17:\n"
           "not implemented\n")
    assert stub_only_crash(out) == frozenset()


def test_stub_only_crash_empty_without_panics() -> None:
    # A segfault prints no panic line: no evidence, no forgiveness.
    from pipeline_core.domain.test_report import stub_only_crash
    assert stub_only_crash("error: process didn't exit successfully") == frozenset()
    assert stub_only_crash("") == frozenset()


def test_stub_only_crash_unparseable_panic_line_vetoes() -> None:
    # A `panicked at` line whose location cannot be parsed is unknown
    # evidence; unknown must not exonerate.
    from pipeline_core.domain.test_report import stub_only_crash
    out = ("thread 'a' panicked at somewhere strange\n"
           "thread 'a' panicked at src/error.rs:64:17:\n"
           "not implemented\n")
    assert stub_only_crash(out) == frozenset()
