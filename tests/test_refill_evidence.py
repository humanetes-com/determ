"""Pins for `domain.refill_evidence` — what a prompt may claim about a body it
asks the model to rewrite.

The instance: the frontier entry `src/context.rs::source` (a traversal step the
escalation added for `test_iter`/`test_len`/`test_rev`/`test_clone`) stamped
[REFILL] on BOTH `impl<C, E> StdError for ContextError<C, E>` (whose body is
correct) and `impl<C> StdError for ContextError<C, Error>` (the severed
`None`). The prompt told the model the correct one "FAILS — rewrite the
BEHAVIOR" and showed the sibling as settled; the tests it meant were not in
the prompt (run `902902-daee577d`, 2026-08-20).
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    body_sha,
    latest_by_unit,
)
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    RefillEvidence,
    aimed_refill_names,
    attributed_test_leaves,
    collect_refill_evidence,
    container_header,
    history_for_block,
    inferred_refill_names,
    pinned_observables_from_tests,
    render_refill_evidence,
    try_history,
    typeid_pin_consequence,
)

FILE = "src/context.rs"
GENERIC = "impl<C, E> StdError for ContextError<C, E>\nwhere\n    C: Display,\n    E: StdError + 'static, {"
ANYHOW = "impl<C> StdError for ContextError<C, Error>\nwhere C: Display, {"


def _member(name: str, body: str) -> FnRecord:
    return FnRecord(file=FILE, fn_name=name, sig_text=f"fn {name}(&self)",
                    body_text=body, has_unimplemented_body=False)


def _block(container: str, body: str) -> ImplBlock:
    m = _member("source", body)
    return ImplBlock(file=FILE, container=container, members=(m,), refill_members=(m,))


def _frontier(direct: bool, evidence: dict[str, str] | None = None) -> FailureFrontier:
    key = FnLocator(FILE, "source")
    dispatch = {key: {"test_iter", "test_len"}}
    convicting = {key: {"test_len"}} if direct else {}
    return FailureFrontier.build(dispatch, convicting, evidence=evidence)


def test_same_named_refill_in_two_blocks_is_inferred_with_the_sibling_named() -> None:
    generic, anyhow = _block(GENERIC, "Some(&self.error)"), _block(ANYHOW, "None")
    fr = _frontier(direct=False, evidence={
        "test_len": "panicked at tests/test_chain.rs:31:5\nassertion `left == right` failed\n  left: 4\n  right: 1"})
    (ev,) = collect_refill_evidence(generic, [generic, anyhow], fr)
    assert ev == RefillEvidence(
        fn_name="source", tests=("test_iter", "test_len"),
        attribution=Attribution.NAMED,
        sibling_containers=(ANYHOW,),
        excerpts=(("test_len", fr.evidence_for("test_len")),))
    assert inferred_refill_names([ev]) == frozenset({"source"})
    text = render_refill_evidence([ev])
    assert "INFERRED" in text and "return it UNCHANGED" in text
    assert "impl<C> StdError for ContextError<C, Error> where C: Display," in text
    assert "attributed failing test(s): test_iter, test_len" in text
    assert "test_len said:" in text and "left: 4" in text and "right: 1" in text
    # The sibling sees the generic block as ITS candidate.
    (ev2,) = collect_refill_evidence(anyhow, [generic, anyhow], fr)
    assert ev2.sibling_containers == (GENERIC,)


def test_direct_conviction_is_stated_as_such() -> None:
    generic = _block(GENERIC, "Some(&self.error)")
    (ev,) = collect_refill_evidence(generic, [generic], _frontier(direct=True))
    assert ev.direct and ev.sibling_containers == ()
    assert inferred_refill_names([ev]) == frozenset()
    text = render_refill_evidence([ev])
    assert "DIRECT" in text and "INFERRED" not in text
    assert "Rewrite its BEHAVIOR" in text


def test_aimed_callee_is_not_named() -> None:
    """Run `1911637-d0fab212`: `object_downcast` was in `crash_texts` and not
    in `crash_blamed`, so it graded NAMED and the prompt said return it
    unchanged. AIMED is that split. Negative control: the same locator
    without `aimed=` is still NAMED."""
    key = FnLocator("src/error.rs", "object_downcast")
    member = FnRecord(
        file="src/error.rs", fn_name="object_downcast",
        sig_text="fn object_downcast()", body_text="addr_of!(_object)",
        has_unimplemented_body=False)
    block = ImplBlock(
        file="src/error.rs", container="", members=(member,),
        refill_members=(member,))
    fr = FailureFrontier.build({key: {"test_large_alignment"}}, {})
    (named,) = collect_refill_evidence(block, [block], fr)
    assert named.attribution is Attribution.NAMED
    assert inferred_refill_names([named]) == frozenset({"object_downcast"})
    assert "return it UNCHANGED" in render_refill_evidence([named])

    (aimed,) = collect_refill_evidence(
        block, [block], fr, aimed=frozenset({key}))
    assert aimed.attribution is Attribution.AIMED
    assert aimed_refill_names([aimed]) == frozenset({"object_downcast"})
    assert inferred_refill_names([aimed]) == frozenset()
    text = render_refill_evidence([aimed])
    assert "AIMED" in text
    assert "return it UNCHANGED" not in text
    assert "Do not return it unchanged" in text


def test_no_frontier_is_inference_with_no_tests() -> None:
    generic = _block(GENERIC, "Some(&self.error)")
    (ev,) = collect_refill_evidence(generic, [generic], None)
    assert ev.tests == () and not ev.direct and ev.excerpts == ()
    assert render_refill_evidence([ev]).startswith("`source` — INFERRED")
    assert render_refill_evidence([]) == ""
    # A block with no re-fill members has nothing to say.
    plain = ImplBlock(file=FILE, container=GENERIC, members=(_member("source", "x"),))
    assert collect_refill_evidence(plain, [plain], _frontier(direct=True)) == ()


def test_attributed_test_leaves_direct_first_bare_deduped_capped() -> None:
    evs = [
        RefillEvidence("a", ("chain::test_rev", "test_iter"),
                       Attribution.NAMED, (), ()),
        RefillEvidence("b", ("tests::test_len", "test_iter"),
                       Attribution.DIRECT, (), ()),
    ]
    assert attributed_test_leaves(evs) == ("test_len", "test_iter", "test_rev")
    assert attributed_test_leaves(evs, cap=2) == ("test_len", "test_iter")
    assert attributed_test_leaves([]) == ()


def test_container_header_is_one_line_without_the_body() -> None:
    assert container_header(GENERIC) == (
        "impl<C, E> StdError for ContextError<C, E> where C: Display, E: StdError + 'static,")
    assert container_header("impl Error {") == "impl Error"
    assert container_header("") == ""


# --- the panic-site quote: the observable, not just the message ------------

_DOWNCAST_RS = (
    "use common::*;\n"            # 1
    "\n"                          # 2
    "#[test]\n"                   # 3
    "fn test_downcast() {\n"      # 4
    "    assert_eq!(\n"           # 5
    "        \"oh no!\",\n"       # 6
    "        bail_literal().unwrap_err().downcast::<&str>().unwrap(),\n"  # 7
    "    );\n"                    # 8
    "}\n"                         # 9
)
_MSG = ("thread 'test_downcast' (263947) panicked at tests/test_downcast.rs:7:56:\n"
        "called `Result::unwrap()` on an `Err` value: oh no!")


def test_panic_site_line_is_quoted_beneath_the_message() -> None:
    """Run `263474-b0e608d3` batch 1: the message alone let the model write
    'test_downcast … Should work.' — the line names the `&str` payload that
    has to survive the downcast."""
    from pipeline_core.domain.refill_evidence import quote_panic_site
    reader = lambda rel: _DOWNCAST_RS if rel == "tests/test_downcast.rs" else None  # noqa: E731
    out = quote_panic_site(_MSG, reader)
    assert out.startswith(_MSG)
    assert "at tests/test_downcast.rs:7 — the line that panicked" in out
    assert ">    7 |         bail_literal().unwrap_err().downcast::<&str>().unwrap()," in out
    # ±2 lines of context, no more.
    assert "     5 |     assert_eq!(" in out and "     9 | }" in out
    assert "use common::*;" not in out
    # NEGATIVE CONTROLS: unchanged when the path cannot be served, when the
    # line is past the file's end, or when there is no panic location at all.
    assert quote_panic_site(_MSG, lambda rel: None) == _MSG
    short = _MSG.replace(":7:56:", ":70:56:")
    assert quote_panic_site(short, reader) == short
    assert quote_panic_site("assertion failed", reader) == "assertion failed"


def test_evidence_block_carries_the_quoted_site_when_a_reader_is_given() -> None:
    generic = _block(GENERIC, "Some(&self.error)")
    fr = _frontier(direct=False, evidence={"test_len": _MSG})
    reader = lambda rel: _DOWNCAST_RS if rel == "tests/test_downcast.rs" else None  # noqa: E731
    (ev,) = collect_refill_evidence(generic, [generic], fr, read_source=reader)
    text = render_refill_evidence([ev])
    assert "test_len said:" in text
    assert "downcast::<&str>().unwrap()," in text
    # Without a reader the block is the message alone — the pre-fix shape.
    (ev0,) = collect_refill_evidence(generic, [generic], fr)
    assert "downcast::<&str>" not in render_refill_evidence([ev0])


# --- write-list pins: the instruction 2932 was missing ----------------------
#
# Computed on the live authored tests (tmp/anyhow/tests, 2026-08-26). The
# pre-fix extractor (`str.find("fn test_downcast")` + `[^>]+?`) on this
# same command returned HighLevel/MidLevel/LowLevel with no
# `downcast::<&str>` (glob-earlier `test_context.rs` answered with
# `test_downcast_ref`; nested `is::<<str as ToOwned>::Owned>` truncated).
# This output is the post-fix run (needle `fn <leaf>` then `(` or `<`,
# generic scanned by bracket depth):
#   python -c 'from pathlib import Path; from pipeline_core.domain.refill_evidence import pinned_observables_from_tests;
#     files = {p.name: p.read_text() for p in Path("tmp/anyhow/tests").glob("test_*.rs")};
#     print(pinned_observables_from_tests(("test_downcast","test_altdebug","test_whitespace","test_trailer"), files.values()))'
# → downcast::<&str>, downcast::<String>, downcast::<io::Error>, assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …)), assert_eq!(EXPECTED_ALTDEBUG_G, format!("{:#?}", …)), assert_eq!(EXPECTED_ALTDEBUG_H, format!("{:#?}", …)), is::<&str>, is::<<str as ToOwned>::Owned>
# No standalone `format!("{:#?}", …)`: the extractor suppresses it when an
# ALTDEBUG pin already carries that substring. Negative control: a
# BlockFillPlan with empty `pins` carries none of these.

_DOWNCAST_FN = """
#[test]
fn test_downcast() {
    assert_eq!(
        "oh no!",
        bail_literal().unwrap_err().downcast::<&str>().unwrap(),
    );
    assert_eq!(
        "oh no!",
        bail_fmt().unwrap_err().downcast::<String>().unwrap(),
    );
    assert_eq!(
        "oh no!",
        bail_error()
            .unwrap_err()
            .downcast::<io::Error>()
            .unwrap()
            .to_string(),
    );
}
"""
_ALTDEBUG_FN = """
#[test]
fn test_altdebug() {
    assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", f().unwrap_err()));
    assert_eq!(EXPECTED_ALTDEBUG_G, format!("{:#?}", g().unwrap_err()));
    assert_eq!(EXPECTED_ALTDEBUG_H, format!("{:#?}", h().unwrap_err()));
}
"""
_ENSURE_FNS = """
#[test]
fn test_trailer() {
    let err = anyhow!("");
    let test = || Ok(ensure!(err.is::<&str>() == false));
    assert_err(test, "Condition failed: `err.is::<&str>() == false`");
}
#[test]
fn test_whitespace() {
    let test = || Ok(ensure!("" == format!("{:#?}", point)));
}
"""


def test_pinned_observables_extract_the_2932_defect_pins() -> None:
    """The plan's fixture: 2932's attributed tests pin downcast::<&str>,
    is::<&str>, and assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))."""
    from pipeline_core.domain.refill_evidence import (
        pinned_observables,
        pinned_observables_from_tests,
        test_item_source,
    )
    # Whole-file extraction of test_downcast would also pick test_drop's
    # DetectDrop — slicing by leaf is the contract.
    assert "DetectDrop" not in test_item_source(
        _DOWNCAST_RS + "\nfn test_drop() { error.downcast::<DetectDrop>() }\n",
        "test_downcast")
    pins = pinned_observables_from_tests(
        ("test_downcast", "test_altdebug", "test_whitespace", "test_trailer"),
        (_DOWNCAST_FN, _ALTDEBUG_FN, _ENSURE_FNS))
    assert "downcast::<&str>" in pins
    assert "downcast::<String>" in pins
    assert "downcast::<io::Error>" in pins
    assert 'assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))' in pins
    assert "is::<&str>" in pins
    # Three ALTDEBUG pins each contain the format substring; a standalone
    # whitespace pin is suppressed when those exist (the assertion
    # `'format!("{:#?}", …)' in pins` used to pass on that substring).
    assert pins.count('format!("{:#?}", …)') == 3
    ws = test_item_source(_ENSURE_FNS, "test_whitespace")
    assert pinned_observables(ws) == 'format!("{:#?}", …)'
    assert "#[test]" in test_item_source(_DOWNCAST_FN, "test_downcast")
    # Prefix: glob-earlier test_downcast_ref must not answer for test_downcast.
    early = (
        "fn test_downcast_ref() { error.downcast::<DetectDrop>(); }\n"
        "fn test_downcast() { bail_literal().downcast::<&str>(); }\n"
    )
    assert "DetectDrop" not in pinned_observables_from_tests(
        ("test_downcast",), (early,))
    assert "downcast::<&str>" in pinned_observables_from_tests(
        ("test_downcast",), (early,))
    # Bracket depth: nested generics must survive the first `>`.
    nested = (
        "fn test_boxed_str() { err.is::<<str as ToOwned>::Owned>(); "
        "err.downcast::<Box<dyn StdError + Send + Sync>>(); }\n"
    )
    nested_pins = pinned_observables(nested)
    assert "is::<<str as ToOwned>::Owned>" in nested_pins
    assert "downcast::<Box<dyn StdError + Send + Sync>>" in nested_pins
    # NEGATIVE CONTROL: a body with no pin-shaped assert yields nothing.
    assert pinned_observables("fn test_len() { assert_eq!(1, 4); }") == ""
    # Evidence block carries the same pins.
    ev = RefillEvidence("construct_from_adhoc", ("test_downcast",),
                        Attribution.ESCALATED, (), ())
    text = render_refill_evidence([ev], pins=pins)
    assert "the attributed tests pin:" in text
    assert "downcast::<&str>" in text
    assert "the attributed tests pin:" not in render_refill_evidence([ev])
    # Run `82157-476ddc16`: pins listed in evidence was not enough; the
    # TypeId consequence sits on the same line when the pins name
    # downcast::<T>/is::<T>, and not when they are ALTDEBUG-only.
    live_pins = (
        "downcast::<&str>, downcast::<String>, is::<&str>, "
        'assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))'
    )
    typed = render_refill_evidence([ev], pins=live_pins, wraps=True)
    assert "stores a different TypeId" in typed
    assert typeid_pin_consequence(live_pins, wraps=True)
    alt_only = 'assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))'
    assert "stores a different TypeId" not in render_refill_evidence(
        [ev], pins=alt_only, wraps=True)
    assert typeid_pin_consequence(alt_only, wraps=True) == ""
    assert typeid_pin_consequence("", wraps=True) == ""
    # The gate: the same TypeId pins on a body that wraps nothing (the
    # `__dispatch_ensure` block of the same run's ensure prompt) carry no
    # sentence, on the evidence block as on the plan line.
    assert typeid_pin_consequence(live_pins, wraps=False) == ""
    untyped = render_refill_evidence([ev], pins=live_pins)
    assert "the attributed tests pin: downcast::<&str>" in untyped
    assert "stores a different TypeId" not in untyped


def test_pinned_observables_edges_the_prefix_and_bracket_fixes_named() -> None:
    """The review's residual branches: empty needle, unterminated generic,
    `->` inside the argument, fn at byte 0 (no prior line), skipped empty
    source, and a `downcast::<` that never closes."""
    from pipeline_core.domain.refill_evidence import (
        _generic_inner,
        pinned_observables,
        test_item_source,
    )
    assert test_item_source("", "test_downcast") == ""
    assert test_item_source("fn other() {}", "test_downcast") == ""
    assert test_item_source("fn test_downcast() { x }", "") == ""
    at_start = "fn test_downcast() { bail_literal().downcast::<&str>(); }"
    assert "downcast::<&str>" in test_item_source(at_start, "test_downcast")
    assert _generic_inner("nope", 0) == ""
    assert _generic_inner("downcast::<&str", 10) == ""
    # `->` must not close the generic (`Fn() -> i32` inside the arg).
    arrow = "fn test_x() { err.downcast::<fn() -> i32>(); }"
    assert "downcast::<fn() -> i32>" in pinned_observables(arrow)
    assert pinned_observables("", "fn test_x() { err.downcast::<(); }") == ""
    generic = "fn test_foo<T>() { err.is::<&str>(); }"
    assert "is::<&str>" in pinned_observables(test_item_source(generic, "test_foo"))


def test_escalated_grade_is_stated_when_the_frontier_does_not_convict() -> None:
    generic = _block(GENERIC, "Some(&self.error)")
    key = FnLocator(FILE, "source")
    (ev,) = collect_refill_evidence(
        generic, [generic], _frontier(direct=False),
        escalated=frozenset({key}))
    assert ev.attribution is Attribution.ESCALATED


# --- helper-mediated exact strings: the ensure pins -------------------------
#
# Computed on the live authored tests (tmp/anyhow/tests/test_ensure.rs,
# 2026-08-26) BEFORE the helper shape existed:
#   python -c 'from pathlib import Path; from pipeline_core.domain.refill_evidence import pinned_observables, test_item_source;
#     src = Path("tmp/anyhow/tests/test_ensure.rs").read_text();
#     print(repr(pinned_observables(test_item_source(src, "test_trailer"))))
#     print(repr(pinned_observables(test_item_source(src, "test_whitespace"))))'
# → 'is::<&str>, is::<<str as ToOwned>::Owned>'
# → 'format!("{:#?}", …)'
# Every `ensure!` message those tests pin goes through
# `assert_err(test, "Condition failed: …")` (test_ensure.rs:78, signature
# `(closure, &'static str)`), and no pin named one — so the `BothDebug`
# retrospective's "drop the 40-byte buffer … avoid the overflow fallback"
# removed an observable the pins never stated. The fixtures below are the
# live items verbatim: lines 77-90 (the helper), 455-492 (`test_trailer`,
# eight `assert_err` calls) and 494-510 (`test_whitespace`).
_LIVE_ENSURE_HELPER = '#[track_caller]\nfn assert_err<T: Debug>(result: impl FnOnce() -> Result<T>, expected: &\'static str) {\n    let actual = result().unwrap_err().to_string();\n\n    // In general different rustc versions will format the interpolated lhs and\n    // rhs $:expr fragment with insignificant differences in whitespace or\n    // punctuation, so we check the message in full against nightly and do just\n    // a cursory test on older toolchains.\n    if rustversion::cfg!(nightly) && !cfg!(miri) {\n        assert_eq!(actual, expected);\n    } else {\n        assert_eq!(actual.contains(" vs "), expected.contains(" vs "));\n    }\n}\n'
_LIVE_ENSURE_TRAILER = '#[test]\nfn test_trailer() {\n    let test = || Ok(ensure!((|| 1)() == 2));\n    assert_err(test, "Condition failed: `(|| 1)() == 2` (1 vs 2)");\n\n    let test = || Ok(ensure!(b"hmm"[1] == b\'c\'));\n    assert_err(test, "Condition failed: `b\\"hmm\\"[1] == b\'c\'` (109 vs 99)");\n\n    let test = || Ok(ensure!(PhantomData::<u8> {} != PhantomData));\n    assert_err(\n        test,\n        "Condition failed: `PhantomData::<u8> {} != PhantomData` (PhantomData<u8> vs PhantomData<u8>)",\n    );\n\n    let result = Ok::<_, Error>(1);\n    let test = || Ok(ensure!(result? == 2));\n    assert_err(test, "Condition failed: `result? == 2` (1 vs 2)");\n\n    let test = || Ok(ensure!((2, 3).1 == 2));\n    assert_err(test, "Condition failed: `(2, 3).1 == 2` (3 vs 2)");\n\n    #[rustfmt::skip]\n    let test = || Ok(ensure!((2, (3, 4)). 1.1 == 2));\n    assert_err(test, "Condition failed: `(2, (3, 4)).1.1 == 2` (4 vs 2)");\n\n    let err = anyhow!("");\n    let test = || Ok(ensure!(err.is::<&str>() == false));\n    assert_err(\n        test,\n        "Condition failed: `err.is::<&str>() == false` (true vs false)",\n    );\n\n    let test = || Ok(ensure!(err.is::<<str as ToOwned>::Owned>() == true));\n    assert_err(\n        test,\n        "Condition failed: `err.is::<<str as ToOwned>::Owned>() == true` (false vs true)",\n    );\n}\n'
_LIVE_ENSURE_WHITESPACE = '#[test]\nfn test_whitespace() {\n    #[derive(Debug)]\n    pub struct Point {\n        #[allow(dead_code)]\n        pub x: i32,\n        #[allow(dead_code)]\n        pub y: i32,\n    }\n\n    let point = Point { x: 0, y: 0 };\n    let test = || Ok(ensure!("" == format!("{:#?}", point)));\n    assert_err(\n        test,\n        "Condition failed: `\\"\\" == format!(\\"{:#?}\\", point)`",\n    );\n}\n'
_LIVE_ENSURE_FILE = (_LIVE_ENSURE_HELPER + "\n" + _LIVE_ENSURE_TRAILER + "\n"
                     + _LIVE_ENSURE_WHITESPACE)
_PRE_CHANGE_TRAILER_PINS = "is::<&str>, is::<<str as ToOwned>::Owned>"
_PRE_CHANGE_WHITESPACE_PINS = 'format!("{:#?}", …)'


def test_helper_asserted_literals_are_pinned_on_the_live_ensure_items() -> None:
    """The plan's fixture: `test_trailer` keeps its two `is::<T>` pins and
    gains exactly its eight `Condition failed:` literals; `test_whitespace`
    keeps `format!("{:#?}", …)` and gains its one; `test_downcast` items,
    whose literal is the FIRST `assert_eq!` argument, gain nothing."""
    from pipeline_core.domain.refill_evidence import (
        assertion_helper_names,
        helper_asserted_literals,
        pinned_observables,
        pinned_observables_from_tests,
        test_item_source,
    )
    assert assertion_helper_names(_LIVE_ENSURE_FILE) == {"assert_err"}
    item = test_item_source(_LIVE_ENSURE_FILE, "test_trailer")
    literals = helper_asserted_literals(item, frozenset())
    assert len(literals) == 8
    assert literals[0] == '"Condition failed: `(|| 1)() == 2` (1 vs 2)"'
    assert literals[-1] == (
        '"Condition failed: `err.is::<<str as ToOwned>::Owned>() == true` '
        '(false vs true)"')
    # Escapes survive as written — this is the string the test compares.
    assert '"Condition failed: `b\\"hmm\\"[1] == b\'c\'` (109 vs 99)"' in literals
    pins = pinned_observables_from_tests(("test_trailer",), (_LIVE_ENSURE_FILE,))
    # ADDS: the pre-change output is the prefix, unchanged and in order…
    assert pins.startswith(_PRE_CHANGE_TRAILER_PINS + ", ")
    # …and what follows is exactly the eight literals.
    assert pins == _PRE_CHANGE_TRAILER_PINS + ", " + ", ".join(literals)
    assert pins.count("Condition failed:") == 8
    ws = pinned_observables_from_tests(("test_whitespace",), (_LIVE_ENSURE_FILE,))
    assert ws == (_PRE_CHANGE_WHITESPACE_PINS + ', '
                  '"Condition failed: `\\"\\" == format!(\\"{:#?}\\", point)`"')
    # NEGATIVE CONTROL (the `assert` prefix half): `assert_err` starts with
    # `assert`, so the item alone already yields the literals; a helper
    # named `check_err` does NOT, and is recovered only through the file's
    # helper set (`assertion_helper_names` sees its `assert_eq!`).
    renamed = _LIVE_ENSURE_FILE.replace("assert_err", "check_err")
    renamed_item = test_item_source(renamed, "test_trailer")
    assert pinned_observables(renamed_item) == _PRE_CHANGE_TRAILER_PINS
    assert helper_asserted_literals(renamed_item, frozenset()) == []
    assert assertion_helper_names(renamed) == {"check_err"}
    assert pinned_observables_from_tests(("test_trailer",), (renamed,)) == pins
    # NEGATIVE CONTROL (unchanged pins): the downcast items' `assert_eq!`
    # carries its literal FIRST — the shape is last-argument only.
    assert pinned_observables_from_tests(("test_downcast",), (_DOWNCAST_FN,)) == (
        "downcast::<&str>, downcast::<String>, downcast::<io::Error>")
    assert pinned_observables_from_tests(("test_downcast",), (_DOWNCAST_FN,)) == (
        pinned_observables(test_item_source(_DOWNCAST_FN, "test_downcast")))
    # A test fn is never a helper, and a call with no literal last arg pins
    # nothing.
    assert "test_trailer" not in assertion_helper_names(_LIVE_ENSURE_FILE)
    assert helper_asserted_literals("assert_eq!(a, b); check(x, 4);",
                                    frozenset({"check"})) == []
    assert helper_asserted_literals('assert_eq!(a, "never closed',
                                    frozenset()) == []
    assert assertion_helper_names("") == frozenset()


def test_pins_are_per_block_and_a_block_with_no_attributed_test_has_none() -> None:
    """Run `82157-476ddc16`'s ensure prompt handed one file-wide pin list
    to every block; per block, a block with no leaf pins nothing, and the
    whole-file fallback is only for a leaf that could not be sliced."""
    from pipeline_core.domain.refill_evidence import pinned_observables_from_tests
    assert pinned_observables_from_tests((), (_LIVE_ENSURE_FILE, _DOWNCAST_FN)) == ""
    unsliceable = "fn helper() { err.is::<u8>(); }"
    assert pinned_observables_from_tests(("nope",), (unsliceable,)) == "is::<u8>"


# --- the wrap reading that gates the TypeId sentence -------------------------
#
# Live bodies from tmp/anyhow/.skeleton-filled/stable/src (2026-08-26):
# `src/error.rs::construct_from_adhoc` (the positive of run `82157-476ddc16`)
# and `src/ensure.rs::__dispatch_ensure` (the negative of the same run's
# ensure prompt), verbatim.
_LIVE_ADHOC_BODY = '\n        Self::construct_from_std(crate::wrapper::MessageError(message), backtrace)\n    '
_LIVE_DISPATCH_ENSURE_BODY = '\n        struct SmallBuf {\n            buf: [u8; 128],\n            pos: usize,\n        }\n        impl core::fmt::Write for SmallBuf {\n            fn write_str(&mut self, s: &str) -> core::fmt::Result {\n                let remaining = self.buf.len() - self.pos;\n                if s.len() > remaining {\n                    return Err(core::fmt::Error);\n                }\n                self.buf[self.pos..self.pos + s.len()].copy_from_slice(s.as_bytes());\n                self.pos += s.len();\n                Ok(())\n            }\n        }\n\n        let mut sb = SmallBuf { buf: [0; 128], pos: 0 };\n        let ok = core::fmt::write(&mut sb, format_args!("{} ({:?} vs {:?})", msg, self.0, self.1)).is_ok();\n\n        if ok {\n            let slice = &sb.buf[..sb.pos];\n            let s = core::str::from_utf8(slice).unwrap_or(msg);\n            let owned = alloc::string::String::from(s);\n            crate::Error::msg(owned)\n        } else {\n            crate::Error::msg(msg)\n        }\n    '


def test_wrap_reading_on_the_live_constructor_and_ensure_bodies() -> None:
    from pipeline_core.domain.models import FieldRecord, TypeRecord
    from pipeline_core.domain.refill_evidence import (
        tuple_struct_names,
        wraps_crate_tuple_struct,
    )
    # The extractor names unnamed fields "0", "1", …: only those structs.
    names = tuple_struct_names([
        TypeRecord(kind="struct", name="MessageError", generics="<M>",
                   fields=(FieldRecord(name="0", ty="M", is_pub=True),)),
        TypeRecord(kind="struct", name="BoxedError",
                   fields=(FieldRecord(name="0", ty="Box<dyn StdError>"),)),
        TypeRecord(kind="struct", name="Error",
                   fields=(FieldRecord(name="inner", ty="Own<ErrorImpl>"),)),
        TypeRecord(kind="struct", name="Adhoc"),
        TypeRecord(kind="enum", name="Kind", variants=("A",)),
    ])
    assert names == {"MessageError", "BoxedError"}
    live = {"MessageError", "DisplayError", "BoxedError", "Quoted"}
    assert wraps_crate_tuple_struct(_LIVE_ADHOC_BODY, live)
    assert not wraps_crate_tuple_struct(_LIVE_DISPATCH_ENSURE_BODY, live)
    # `crate::Error::msg(owned)` is a path call, not `Error(`; an
    # `Err(core::fmt::Error)` is a unit struct in an argument, not a
    # construction — the negative is not an accident of the name set.
    assert not wraps_crate_tuple_struct(_LIVE_DISPATCH_ENSURE_BODY, live | {"Error"})
    # CONTROL: the CORRECT constructor shape — the wrapper bound to a local
    # and handed to `construct` with its own table — is not a wrap; nor is
    # a tuple or a `let` binding that never enters a call.
    assert not wraps_crate_tuple_struct(
        "let error = BoxedError(error);\n        let vtable = &ErrorVTable { "
        "object_drop: object_drop::<BoxedError> };\n        unsafe { "
        "Error::construct(error, vtable, backtrace) }", live)
    assert not wraps_crate_tuple_struct("let pair = (MessageError(m), 1);", live)
    assert wraps_crate_tuple_struct(
        "Self::construct_from_std(DisplayError(message), backtrace)", live)
    assert wraps_crate_tuple_struct("outer(inner(x), MessageError(m))", live)
    assert not wraps_crate_tuple_struct(_LIVE_ADHOC_BODY, frozenset())


# --- 2b: the unit's own FLAT tries in the evidence line ---------------------
#
# Plan fixture: three recorded audits (263474 iter 2, 2932 iter 2, 82157
# iter 1) → three FLAT rows for construct_from_adhoc → the rendered line
# names three runs. `latest_by_unit` would collapse them to one; try_history
# does not. Negative control: a row with run_id "" (today's pre-2a ledger)
# contributes nothing, so the prompt is byte-identical to the pre-history
# render.

_ADHOC_BODY = (
    "Self::construct_from_std(crate::wrapper::MessageError(message), backtrace)")
_ADHOC_FILE = "src/error.rs"
_ADHOC_FN = "construct_from_adhoc"


def _adhoc_member() -> FnRecord:
    return FnRecord(file=_ADHOC_FILE, fn_name=_ADHOC_FN,
                    sig_text="fn construct_from_adhoc", body_text=_ADHOC_BODY,
                    has_unimplemented_body=False)


def _adhoc_block() -> ImplBlock:
    m = _adhoc_member()
    return ImplBlock(file=_ADHOC_FILE, container="impl Error {", members=(m,),
                     refill_members=(m,))


def _flat_row(run_id: str, iteration: int, *, body: str = _ADHOC_BODY,
              oracle_fp: str = "") -> LedgerEntry:
    return LedgerEntry(
        key=LedgerKey(project="anyhow", cluster="stable",
                      file=_ADHOC_FILE, fn_name=_ADHOC_FN),
        body_sha=body_sha((body,)), bodies=(body,),
        sig_sha="", layout_fp="",
        state=BodyState.FLAT, run_id=run_id, iteration=iteration,
        oracle_fp=oracle_fp)


_ORACLE = "fp-stable-constructors"


def test_try_history_names_the_three_recorded_flat_runs_not_the_latest() -> None:
    """The plan's fixture: three FLAT rows from three runs, all the body
    shown in the prompt, render as one line naming all three. `latest_by_unit`
    would name only 82157 — that is the collapse this function exists to
    refuse.

    The identity clause is gone: a FLAT row cannot tell a verbatim reply
    from a revert, so the line names the runs and that the tests still
    fail, and stops there."""
    rows = [
        _flat_row("263474-b0e608d3", 2, oracle_fp=_ORACLE),
        _flat_row("2932-cf516410", 2, oracle_fp=_ORACLE),
        _flat_row("82157-476ddc16", 1, oracle_fp=_ORACLE),
        _flat_row("", 9, oracle_fp=_ORACLE),            # cannot name a run
    ]
    sha = body_sha((_ADHOC_BODY,))
    tries = try_history(rows, file=_ADHOC_FILE, fn_name=_ADHOC_FN,
                        current_sha=sha, oracle_fp=_ORACLE)
    assert [t.run_id for t in tries] == [
        "263474-b0e608d3", "2932-cf516410", "82157-476ddc16"]
    assert all(t.same_body for t in tries)
    assert len(latest_by_unit(rows)) == 1
    block = _adhoc_block()
    hist = history_for_block(block, rows, oracle_fp=_ORACLE)
    (ev,) = collect_refill_evidence(
        block, [block],
        FailureFrontier.build(
            {FnLocator(_ADHOC_FILE, _ADHOC_FN): {"test_downcast", "test_boxed_str"}},
            {}),
        history=hist)
    text = render_refill_evidence([ev])
    assert "rewritten in 263474-b0e608d3 iter 2, 2932-cf516410 iter 2, 82157-476ddc16 iter 1 ×3" in text
    assert "each time the body came back identical to the one shown here" not in text
    assert "2 still fail on it" in text
    # NEGATIVE CONTROL: empty history (today's ledger) leaves the render
    # without a rewritten-in line.
    (bare,) = collect_refill_evidence(block, [block], None)
    assert bare.history == ()
    assert "rewritten in" not in render_refill_evidence([bare])


def test_try_history_refuses_rows_graded_under_another_oracle() -> None:
    """Same refuse-empty rule as reseed_allowed: a row whose oracle_fp is
    empty (written before the field) or a different fingerprint does not
    enter the prompt. The three-run fixture with a matching stamp still
    names three runs. A caller that omits oracle_fp (default "") gets
    nothing, so the prompt stays byte-identical to the pre-history render."""
    stamped = [
        _flat_row("263474-b0e608d3", 2, oracle_fp=_ORACLE),
        _flat_row("2932-cf516410", 2, oracle_fp=_ORACLE),
        _flat_row("82157-476ddc16", 1, oracle_fp=_ORACLE),
    ]
    empty_fp = [_flat_row("263474-b0e608d3", 2)]          # pre-field row
    other = [_flat_row("263474-b0e608d3", 2, oracle_fp="other-oracle")]
    sha = body_sha((_ADHOC_BODY,))
    assert [t.run_id for t in try_history(
        stamped, file=_ADHOC_FILE, fn_name=_ADHOC_FN,
        current_sha=sha, oracle_fp=_ORACLE)] == [
        "263474-b0e608d3", "2932-cf516410", "82157-476ddc16"]
    assert try_history(
        empty_fp, file=_ADHOC_FILE, fn_name=_ADHOC_FN,
        current_sha=sha, oracle_fp=_ORACLE) == ()
    assert try_history(
        other, file=_ADHOC_FILE, fn_name=_ADHOC_FN,
        current_sha=sha, oracle_fp=_ORACLE) == ()
    assert try_history(
        stamped, file=_ADHOC_FILE, fn_name=_ADHOC_FN,
        current_sha=sha) == ()                          # caller omitted fp
    assert history_for_block(_adhoc_block(), stamped) == {_ADHOC_FN: ()}


def test_three_leaf_file_first_pin_join_on_the_live_attributed_set() -> None:
    """The 82157 file-first attributed set {test_boxed_str, test_downcast,
    test_trailer} as pinned_observables_from_tests actually emits on the live
    tests/. The join is the addendum's computed fixture, not a hand-made
    five-name list. Trailer-only is the negative: no downcast::<, and the
    eight Condition failed: literals."""
    live = Path(__file__).resolve().parents[1] / "tmp" / "anyhow" / "tests"
    files = [p.read_text(encoding="utf-8") for p in sorted(live.glob("test_*.rs"))]
    assert files, "live anyhow tests/ missing — the fixture is computed there"
    join = pinned_observables_from_tests(
        ("test_boxed_str", "test_downcast", "test_trailer"), files)
    assert join == (
        "downcast::<Box<dyn StdError + Send + Sync>>, downcast::<&str>, "
        "downcast::<String>, downcast::<io::Error>, is::<&str>, "
        "is::<<str as ToOwned>::Owned>, "
        '"Condition failed: `(|| 1)() == 2` (1 vs 2)", '
        '"Condition failed: `b\\"hmm\\"[1] == b\'c\'` (109 vs 99)", '
        '"Condition failed: `PhantomData::<u8> {} != PhantomData` '
        '(PhantomData<u8> vs PhantomData<u8>)", '
        '"Condition failed: `result? == 2` (1 vs 2)", '
        '"Condition failed: `(2, 3).1 == 2` (3 vs 2)", '
        '"Condition failed: `(2, (3, 4)).1.1 == 2` (4 vs 2)", '
        '"Condition failed: `err.is::<&str>() == false` (true vs false)", '
        '"Condition failed: `err.is::<<str as ToOwned>::Owned>() == true` '
        '(false vs true)"')
    trailer = pinned_observables_from_tests(("test_trailer",), files)
    assert "downcast::<" not in trailer
    assert trailer.count("Condition failed:") == 8
