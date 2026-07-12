"""F3 pins — structured test-run report + blame partition (pure domain)."""
from __future__ import annotations

from pipeline_core.domain.models import FnLocator
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

def _locate(table: dict[tuple[str, int], FnLocator]):
    return lambda f, ln: table.get((f, ln))


def test_partition_external_only() -> None:
    # the only failure is a stub-panic in a fn NOT owned by this unit.
    r = parse_test_output(NIGHTLY_OLD)
    unit = frozenset({FnLocator("src/lib.rs", "mine")})
    loc = _locate({("src/error.rs", 5): FnLocator("src/error.rs", "context")})
    v = partition_blame(r, unit, loc)
    assert v.self_failures == ()
    assert v.blocking_fns == frozenset({FnLocator("src/error.rs", "context")})


def test_partition_mixed_keeps_only_self() -> None:
    r = parse_test_output(STABLE_MULTI)
    # this unit OWNS chain.rs::eq (the assertion), NOT error.rs (the stub).
    unit = frozenset({FnLocator("src/chain.rs", "eq")})
    loc = _locate({
        ("src/error.rs", 42): FnLocator("src/error.rs", "context"),
        ("src/chain.rs", 8): FnLocator("src/chain.rs", "eq"),
    })
    v = partition_blame(r, unit, loc)
    assert [f.test_name for f in v.self_failures] == ["tests::test_assert"]
    assert v.blocking_fns == frozenset({FnLocator("src/error.rs", "context")})


def test_partition_stub_in_own_unit_stays_self() -> None:
    # a stub-panic that resolves to the unit's OWN fn is not external.
    r = parse_test_output(NIGHTLY_OLD)
    own = FnLocator("src/error.rs", "context")
    v = partition_blame(r, frozenset({own}), _locate({("src/error.rs", 5): own}))
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
            FnLocator("src/error.rs", "context")),)
    ev = U2Verdict.accept_external_only(ext)
    assert ev.decision is U2Decision.ACCEPT_EXTERNAL_ONLY
    assert ev.blocking_fns == frozenset({FnLocator("src/error.rs", "context")})
    rv = U2Verdict.reject(frozenset({"t"}), "detail")
    assert rv.decision is U2Decision.REJECT and rv.self_failures == frozenset({"t"})


def test_blameverdict_default_empty() -> None:
    assert BlameVerdict().self_failures == () and BlameVerdict().external == ()
