"""One test name failing in two test binaries is two failing tests.

Run `3893442-cb547388` (2026-09-19): `test_downcast_ref` failed in
`tests/test_context.rs` and in `tests/test_downcast.rs`. The parser kept the
first `---- name stdout ----` section and dropped the second, so the evidence
a prompt quotes, the premise a fill re-plans on and the truncation reading
each saw one copy, and the owner map measured one binary. The capture below
is invented: `t` fails in `one` at a test-source assertion and in `two`
inside the crate.

Negative control for each reader: the same capture with the second binary's
section removed reads exactly as the collided capture used to.
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.measurement import (
    cargo_target_of,
    failing_binaries_by_name,
)
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.test_outcome import (
    RunKind,
    outcomes_of,
    premise_moved,
    premise_of,
    run_kind,
)
from pipeline_core.domain.test_report import (
    failure_evidence_by_test,
    parse_test_output,
)

_ONE = """     Running tests/one.rs (target/debug/deps/one-aaa)

running 1 test
test t ... FAILED

failures:

---- t stdout ----

thread 't' panicked at tests/one.rs:3:5:
assertion `left == right` failed
  left: 1
 right: 2
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    t

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

"""

_TWO = """     Running tests/two.rs (target/debug/deps/two-bbb)

running 1 test
test t ... FAILED

failures:

---- t stdout ----

thread 't' panicked at src/lib.rs:LINE:5:
not implemented


failures:
    t

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


def _capture(line: int = 6) -> str:
    return _ONE + _TWO.replace("LINE", str(line))


def test_the_parser_keeps_the_other_binarys_failure_of_the_same_name() -> None:
    (f,) = parse_test_output(_capture()).failures
    assert (f.panic_file, f.panic_line) == ("tests/one.rs", 3)
    assert [(o.panic_file, o.panic_line) for o in f.others] == [("src/lib.rs", 6)]
    (control,) = parse_test_output(_ONE).failures
    assert control.others == ()


def test_the_evidence_quotes_both_panics() -> None:
    text = failure_evidence_by_test(_capture())["t"]
    assert "tests/one.rs:3:5" in text and "src/lib.rs:6:5" in text
    assert "another test named t, in a different test binary" in text
    assert "src/lib.rs" not in failure_evidence_by_test(_ONE)["t"]


def test_a_copy_that_stopped_in_the_crate_makes_the_name_truncated() -> None:
    assert run_kind(outcomes_of(_capture())["t"]) is RunKind.TRUNCATED
    assert run_kind(outcomes_of(_ONE)["t"]) is RunKind.ASSERTED


def test_a_move_of_the_second_copy_moves_the_premise() -> None:
    # The second copy's first failure moves from `alpha` to `beta`.
    def locate(file: str, line: int) -> FnSite | None:
        return FnSite(file, "alpha" if line < 8 else "beta", "")

    before = premise_of(outcomes_of(_capture(6)), locate)
    after = premise_of(outcomes_of(_capture(9)), locate)
    assert premise_moved(before, after) == ("t",)
    # Control: read as the first copy only (the pre-fix parse), the name's
    # entry is identical on both captures and nothing moved.
    first_only = [premise_of({"t": replace(o["t"], others=())}, locate)
                  for o in (outcomes_of(_capture(6)), outcomes_of(_capture(9)))]
    assert premise_moved(*first_only) == ()


def test_the_failing_binaries_of_a_name_are_its_cargo_targets() -> None:
    pairs = [("tests/one.rs", "t"), ("tests/two.rs", "t"),
             ("tests/nested/main.rs", "u"), ("src/lib.rs", "v")]
    assert failing_binaries_by_name(pairs) == {
        "t": frozenset({"one", "two"}), "u": frozenset({"nested"})}
    assert cargo_target_of("src/lib.rs") is None


def test_each_copys_panic_line_is_quoted() -> None:
    from pipeline_core.domain.refill_evidence import quote_panic_site
    sources = {"tests/one.rs": "#[test]\nfn t() {\n    assert_eq!(widgets::alpha(), 2);\n}\n",
               "src/lib.rs": "\n".join(f"// {i}" for i in range(1, 12))}
    text = quote_panic_site(failure_evidence_by_test(_capture())["t"], sources.get)
    assert "at tests/one.rs:3" in text and "at src/lib.rs:6" in text
    # Control: the first copy alone quotes one line.
    one = quote_panic_site(failure_evidence_by_test(_ONE)["t"], sources.get)
    assert "at src/lib.rs" not in one and "at tests/one.rs:3" in one
