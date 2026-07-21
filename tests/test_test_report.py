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
