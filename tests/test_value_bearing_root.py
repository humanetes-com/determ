"""The value-bearing gate judges the operator at the root of the when's value.

The gate scanned the whole when for `==`, `!=`, `<`, `>`, `&&`, `||`,
`.contains(`. Every when that compares inside a macro argument, a call
argument or a closure it then unwraps was refused before cargo:
`(|| -> Result<()> { ensure!(a >= b); Ok(()) })().unwrap_err()` is an
error, not a boolean. Over every task 0d reply on record (2026-10-04 →
2026-10-27, 634 whens; `tests/fixtures/behaviour/recorded_whens.json`,
distilled with `scripts/value_bearing_replay.py`'s reader) the substring
scan refused 110, all of them that shape or a qualified path read as `<`;
16 of 25 on run `2355041-eae39441`, which halved the oracle's behaviour
tests.

Claims (entry 373 as its review corrected it): every recorded when is
admitted; the comparisons the gate exists for are still refused, including
the three a depth-only scan would admit — a parenthesised comparison, a
block whose tail compares, a called closure whose body compares; an
identity predicate is admitted. NEGATIVE CONTROL: the substring scan,
copied here, refuses 110 of the record.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.behaviour_probe import (
    _COMPARISON_TOKENS,
    _LOGIC_TOKENS,
    identity_type_of,
    value_bearing_reason,
    without_turbofish,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "behaviour" / "recorded_whens.json"


def _substring_scan(when: str) -> str | None:
    """The gate before the change, for the control."""
    bare = without_turbofish(when.strip())
    if any(t in bare for t in _LOGIC_TOKENS):
        return "logic"
    if identity_type_of(when) is not None:
        return None
    return "compares" if any(t in bare for t in (*_COMPARISON_TOKENS, "<", ">")) else None


def test_every_recorded_when_is_admitted() -> None:
    whens = [r["when"] for r in json.loads(_FIXTURE.read_text())["whens"]]
    assert len(whens) == 634
    refused = [w for w in whens if value_bearing_reason(w) is not None]
    assert refused == []
    assert sum(1 for w in whens if _substring_scan(w) is not None) == 110


def test_the_value_s_own_comparison_is_still_refused() -> None:
    for expr in ('format!("{}", b) == "world"', "a != b", "n >= 3", 'x.contains("y")',
                 "v.len() < 3", "n > 0", "a.is::<X>() && b.is::<Y>()",
                 "a.is::<X>() || b.is::<Y>()",
                 '(format!("{}", b) == "world")', "{ let n = 2; n == 2 }",
                 "(|x| x == 1)(x)", "((|x| x == 1))(x)",
                 "(((|x| x == 1)))(x)", "unsafe { a < b }", "((a == b))"):
        assert value_bearing_reason(expr) is not None, expr


def test_an_operator_off_the_root_is_admitted() -> None:
    for expr in ("(|| -> Result<()> { ensure!(a >= b); Ok(()) })().unwrap_err()",
                 "ensure!(x > y).unwrap_err()", "f(a == b)",
                 "<(i32, &str) as Tr>::f((1, \"a\"), \"m\")",
                 "<&err as std::error::Error>::source()",
                 "{ let ok = a == b; describe(ok) }",
                 'format!("{}", "a == b")', "(a, b == c)",
                 "err.is::<io::Error>()", "e.to_string()", "((a, b))(x)"):
        assert value_bearing_reason(expr) is None, expr


def test_the_root_reader_on_every_shape_it_names() -> None:
    """Each branch of `value_root` and `_top_level_text`, on its own shape."""
    from pipeline_core.domain.behaviour_probe import _closure_body, value_root
    assert value_root("(move || { a == b })()") == "a == b"
    assert value_root("(|| a == b)()") == "a == b"
    assert value_root("(|x| -> bool { x == 1 })(2)") == "x == 1"
    assert value_root("((|x| x == 1))(x)") == "x == 1"
    assert value_root("((a, b))(x)") == "((a, b))(x)"
    assert _closure_body("|| -> bool x") is None          # a return type with no block
    assert _closure_body("|x x") is None                    # an unterminated parameter list
    assert _closure_body("make()") is None                  # not a closure
    assert value_root("(f)(x)") == "(f)(x)"                 # a call of a parenthesised path
    assert value_root("(a == b") == "(a == b"               # unbalanced: read as written
    assert value_root("(1, 2)") == "(1, 2)"                 # a tuple is its own value
    assert value_root("{ x; }") == ""                       # a block whose value is ()
    assert value_bearing_reason("x == (1") is not None      # unbalanced group still compares
    assert value_bearing_reason("|| a == b") is None        # an uncalled closure is the closure
    assert value_bearing_reason("v.iter().map(|x| x + 1).count()") is None
