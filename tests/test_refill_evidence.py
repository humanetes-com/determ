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

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_evidence import (
    Attribution,
    RefillEvidence,
    attributed_test_leaves,
    collect_refill_evidence,
    container_header,
    inferred_refill_names,
    render_refill_evidence,
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
    typed = render_refill_evidence([ev], pins=live_pins)
    assert "stores a different TypeId" in typed
    assert typeid_pin_consequence(live_pins)
    alt_only = 'assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))'
    assert "stores a different TypeId" not in render_refill_evidence(
        [ev], pins=alt_only)
    assert typeid_pin_consequence(alt_only) == ""
    assert typeid_pin_consequence("") == ""


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
