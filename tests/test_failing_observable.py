"""The failure's observable decides whether a failing test is a FORMAT test.

Run `3076-051d385a` (2026-10-13, cluster `stable`, 148/151): a test failed on
`called Option::unwrap() on a None value` after a downcast, and its source
also called `.to_string()` on an earlier, passing line and after the
unwrap. Read on the whole source it was a Display test: the format hop gave
it to the Display formatter, recorded a Display pair, and the escalation
withhold dropped the producer escalation the downcast needed.

The plan's sentence, asserted here on an invented crate shape: a test whose
failure is an unwrap on a value produced before any render has no format
trait in its observable, so the hop does not attribute it to a formatter and
records no pair; a test that fails on an assertion over a formatted string
keeps the trait through the `let` that bound it. Negative control: the
whole-source reading (`observed=None`, today's input) attributes the first
test to the formatter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    _attribute_format_only_test,
    behavior_frontier_from_texts,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.failing_observable import observable_source
from pipeline_core.domain.format_traits import format_traits_used
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import locate_fn

_TEST_REL = "tests/bag.rs"
_TEST_SRC = (
    "use crate_under::Bag;\n"
    "\n"
    "#[test]\n"
    "fn bag_roundtrip() {\n"
    "    let bag = Bag::new(\"x\");\n"
    "    assert_eq!(bag.to_string(), \"x\");\n"
    "    let got = bag\n"
    "        .lookup()\n"
    "        .unwrap()\n"
    "        .to_string();\n"
    "    assert_eq!(got, \"1\");\n"
    "}\n"
    "\n"
    "#[test]\n"
    "fn bag_debug_header() {\n"
    "    let bag = Bag::new(\"x\");\n"
    "    let shown = format!(\"{:?}\", bag);\n"
    "    let head = shown.lines().next().unwrap();\n"
    "    assert!(head.starts_with(\"Bag\"));\n"
    "}\n"
)

_RECS: list[dict[str, Any]] = [
    {"file": "src/bag.rs", "fn_name": "fmt", "container": "impl Display for Bag",
     "body_text": "write!(f, \"{}\", self.0)"},
    {"file": "src/bag.rs", "fn_name": "fmt", "container": "impl Debug for Bag",
     "body_text": "write!(f, \"Bag({})\", self.0)"},
    {"file": "src/bag.rs", "fn_name": "lookup", "container": "impl Bag",
     "body_text": "None"},
    {"file": "src/bag.rs", "fn_name": "new", "container": "impl Bag",
     "body_text": "Bag(s.to_owned())"},
]


def _pos(needle: str, nth: int = 0) -> tuple[int, int]:
    """1-based line:col of the `nth` occurrence of `needle` in the test."""
    at = -1
    for _ in range(nth + 1):
        at = _TEST_SRC.index(needle, at + 1)
    line = _TEST_SRC.count("\n", 0, at) + 1
    col = at - (_TEST_SRC.rfind("\n", 0, at) + 1) + 1
    return line, col


def _unwrap_failure() -> str:
    line, col = _pos("unwrap()")
    return (f"thread 'bag_roundtrip' (4242) panicked at {_TEST_REL}:"
            f"{line}:{col}:\ncalled `Option::unwrap()` on a `None` value")


def _assert_failure() -> str:
    line, col = _pos("assert!(head")
    return (f"thread 'bag_debug_header' (4243) panicked at {_TEST_REL}:"
            f"{line}:{col}:\nassertion failed: head.starts_with(\"Bag\")")


def _whole(test: str) -> str:
    loc = locate_fn(_TEST_SRC, test)
    assert loc is not None
    sig, _s, b, e = loc
    return f"{sig} {{{_TEST_SRC[b:e]}}}"


class _Structure:
    def enumerate_fns(self, _src: Path) -> list[dict[str, Any]]:
        return _RECS


def _deps() -> tuple[AttributionDeps, list[str]]:
    lines: list[str] = []
    return AttributionDeps(
        structure=_Structure(), classify=lambda **_k: {}, locate_fn=locate_fn,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False, log=lines.append), lines


def test_an_unwrap_failure_after_a_passing_render_has_no_format_trait() -> None:
    obs = observable_source(_TEST_SRC, "bag_roundtrip", _unwrap_failure(),
                            _TEST_REL)
    assert obs is not None
    assert format_traits_used(obs) == frozenset()
    # The cut stops at the panicking call, so the render after it is out,
    # and the binding it reads comes in.
    assert "lookup()" in obs and "unwrap" in obs
    assert ".to_string()" not in obs
    assert 'let bag = Bag::new("x");' in obs
    # Negative control: today's reading, the whole source.
    assert format_traits_used(_whole("bag_roundtrip")) == {"Display"}


def test_an_assertion_over_a_rendered_string_keeps_its_trait_through_lets() -> None:
    obs = observable_source(_TEST_SRC, "bag_debug_header", _assert_failure(),
                            _TEST_REL)
    assert obs is not None
    assert format_traits_used(obs) == {"Debug"}
    assert 'format!("{:?}", bag)' in obs


def test_a_failure_outside_the_test_fn_is_not_read() -> None:
    crate_panic = ("thread 'bag_roundtrip' (1) panicked at src/bag.rs:9:9:\n"
                   "not implemented")
    assert observable_source(_TEST_SRC, "bag_roundtrip", crate_panic,
                             _TEST_REL) is None
    res = AttributionResult({}, FailureKind.BEHAVIOR, {},
                            {"bag_roundtrip": _whole("bag_roundtrip")})
    # Ignorance reads the whole source, as every consumer did before.
    assert res.observable_traits_for("bag_roundtrip") == {"Display"}


def test_the_hop_does_not_take_an_unwrap_failure_to_the_formatter() -> None:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in _RECS:
        by_name.setdefault(r["fn_name"], []).append(r)
    obs = observable_source(_TEST_SRC, "bag_roundtrip", _unwrap_failure(),
                            _TEST_REL)

    def _hop(observed: str | None) -> tuple[dict, set]:
        frontier: dict = {}
        pairs: set = set()
        _attribute_format_only_test(
            "bag_roundtrip", _whole("bag_roundtrip"), _TEST_SRC, by_name,
            frontier, {}, lambda _l: None, owned=True, pairs_out=pairs,
            observed=observed)
        return frontier, pairs

    frontier, pairs = _hop(obs)
    assert FnLocator("src/bag.rs", "fmt") not in frontier and not pairs
    # Negative control: the whole-source reading attributes it to `fmt`.
    c_frontier, _ = _hop(None)
    assert c_frontier.get(FnLocator("src/bag.rs", "fmt")) == {"bag_roundtrip"}


def test_the_behavior_path_carries_the_observable_to_the_result() -> None:
    deps, _lines = _deps()
    out = ("running 1 test\ntest bag_roundtrip ... FAILED\n\nfailures:\n\n"
           "---- bag_roundtrip stdout ----\n" + _unwrap_failure() + "\n\n"
           "failures:\n    bag_roundtrip\n\n"
           "test result: FAILED. 0 passed; 1 failed\n")
    observables: dict[str, str] = {}
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, out, {_TEST_REL: _TEST_SRC}, Path("/x"),
        observables_out=observables)
    assert "bag_roundtrip" in observables
    assert FnLocator("src/bag.rs", "fmt") not in frontier
    res = AttributionResult(frontier, FailureKind.BEHAVIOR, {},
                            {"bag_roundtrip": _whole("bag_roundtrip")},
                            observables=observables)
    assert res.observable_traits_for("bag_roundtrip") == frozenset()


def test_the_slice_reads_the_shapes_libtest_and_rustfmt_produce() -> None:
    """Edge shapes of the same rule, on direct inputs: a tail expression
    with no `;`, a column on the `.` of a method call, a macro written with
    spaces around `!`, a location outside the text, and no evidence."""
    from pipeline_core.domain.failing_observable import (
        _cut_end,
        _lets,
        _masked,
        _match_forward,
        observable_slice,
    )
    src = ("fn t() {\n"
           "    let v = make();\n"
           "    check(v).expect(\"x\")\n"
           "}\n")
    b, e = src.index("{") + 1, src.rindex("}")
    tail = observable_slice(src, b, e, 3, 13)       # col on the `.`
    assert tail is not None
    assert tail.startswith("check(v).expect") and "let v = make();" in tail
    assert observable_slice(src, b, e, 99, 1) is None
    assert observable_slice(src, b, e, 0, 1) is None
    assert observable_slice(src, b, e, 1, 200) is None
    spaced = "assert_eq ! ( a , b ) ;"
    m = _masked(spaced)
    assert _cut_end(m, 0, len(m)) == spaced.index(")") + 1
    assert _cut_end(m, spaced.index("("), len(m)) == spaced.index("(") + 1
    assert _match_forward("(]", 0, 2) == 2
    assert _match_forward("((", 0, 2) == 2
    lets = _lets(_masked("{ let a = 1 }"), 1, 13)
    assert lets and lets[0][2] == {"a"}
    assert observable_source(_TEST_SRC, "bag_roundtrip", "", _TEST_REL) is None


def test_a_backtrace_frame_in_the_test_file_locates_a_crate_panic() -> None:
    line, col = _pos("unwrap()")
    words = ("thread 'bag_roundtrip' (1) panicked at src/bag.rs:9:9:\n"
             "not implemented\nstack backtrace:\n"
             f"  12: bag::bag_roundtrip\n             at ./{_TEST_REL}:{line}:{col}")
    obs = observable_source(_TEST_SRC, "bag_roundtrip", words, _TEST_REL)
    assert obs is not None and format_traits_used(obs) == frozenset()


def test_an_aliased_test_name_finds_its_observable() -> None:
    res = AttributionResult({}, FailureKind.BEHAVIOR, {},
                            {"bag_roundtrip": _whole("bag_roundtrip")},
                            observables={"bag::bag_roundtrip": "x.unwrap"})
    assert res.observable_traits_for("bag_roundtrip") == frozenset()
    res2 = AttributionResult({}, FailureKind.BEHAVIOR, {},
                             {"bag::bag_roundtrip": _whole("bag_roundtrip")})
    assert res2.observable_traits_for("bag_roundtrip") == {"Display"}


def test_the_hop_line_says_what_its_traits_were_read_from() -> None:
    """341's offline reading of eleven hop lines could not tell whether the
    hop read the failing line or the whole test source. The attribution
    line now names it."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in _RECS:
        by_name.setdefault(r["fn_name"], []).append(r)
    logs: list[str] = []
    _attribute_format_only_test(
        "bag_roundtrip", _whole("bag_roundtrip"), _TEST_SRC, by_name,
        {}, {}, logs.append, owned=True, pairs_out=set(), observed=None)
    assert any("read from the whole test source" in m for m in logs)
    logs.clear()
    _attribute_format_only_test(
        "bag_roundtrip", _whole("bag_roundtrip"), _TEST_SRC, by_name,
        {}, {}, logs.append, owned=True, pairs_out=set(),
        observed='assert_eq!(format!("{}", b), "x")')
    assert any("read from the failing observable `assert_eq!" in m
               for m in logs)
