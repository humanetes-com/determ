"""A documented type carries its own statement of a pinned observable.

Run `617614-0abaf2d0` (2026-09-01) dispatched the formatter pair WITH its
two convicting tests, their panics and their source, and the write still
reverted on ten sibling tests: the reply delegated the asserted header to a
`std::backtrace::Backtrace` formatter, which emits no such string, because
the crate's own statement of the format — a fenced example on the
documented type, with the sentence stating the condition it holds under —
sat at doc line 50 of 101 and the renderer kept three.

The crate shape here is a synthetic one (a `Report` whose doc shows its
rendering): the mechanism must not know which crate it is on, and a fixture
carrying the live leftovers' names would be the steer the conventions
forbid. The live-records computation is
`scripts/rebuild_pair_prompt_fixture.py`.
"""
from __future__ import annotations

from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.refill_evidence import asserted_output_literals
from pipeline_core.prompts.sections import format_type_for_prompt

# A documented type in the shape rustdoc actually produces: a one-line
# summary, prose, then an introducing sentence and a fenced example. The
# observable the test below pins is stated only inside that fence.
DOCUMENTED = TypeRecord(
    file="src/report.rs",
    kind="struct",
    name="Report",
    definition="\n".join([
        "/// A diagnostic report.",
        "///",
        "/// `Report` collects the findings of one run and renders them for",
        "/// a terminal.",
        "///",
        "/// The rendering starts with the summary line.",
        "///",
        "/// ```console",
        "/// 3 findings",
        "/// ```",
        "///",
        "/// and when a source span was resolved:",
        "///",
        "/// ```console",
        "/// 3 findings",
        "///",
        "/// Diagnostics:",
        "///     src/main.rs:12",
        "/// ```",
        "#[repr(transparent)]",
        "pub struct Report {}",
    ]),
)

# A test asserting on that rendering, in the shape the extractor hands over.
PINNING_TEST = """
#[test]
fn report_renders_span() {
    let s = format!("{:?}", report());
    assert!(s.starts_with("3 findings"));
    assert!(s.contains("\\n\\n"));
    assert!(s.contains("Diagnostics:") || s.contains("diagnostics:"));
}
"""


def test_asserted_literals_reads_nested_predicates() -> None:
    """The literals a test asserts appear in a rendering, including the ones
    nested inside an assert's condition — the shape
    `helper_asserted_literals` cannot see (it reads a call's LAST argument,
    and only when the call has two or more)."""
    lits = asserted_output_literals(PINNING_TEST)
    assert "Diagnostics:" in lits
    assert "diagnostics:" in lits
    assert "3 findings" in lits


def test_short_and_non_alphanumeric_literals_are_not_observables() -> None:
    """A literal that occurs in everything identifies nothing: `"\\n\\n"` has
    no alphanumeric character and `"err"` is under the length floor, so
    neither may select a doc region."""
    lits = asserted_output_literals(
        'fn t() { assert!(s.contains("\\n\\n")); assert!(s.contains("err")); '
        'assert_eq!(x, format!("{:#?}", y)); }')
    assert lits == frozenset()


def test_documented_type_carries_the_region_stating_a_pinned_observable(
) -> None:
    """The plan's sentence: a type whose doc states an observable a failing
    test pins carries that doc region into the fill prompt — the fenced
    example AND the sentence introducing it, which is where the condition
    the example holds under is written."""
    observables = asserted_output_literals(PINNING_TEST)
    rendered = format_type_for_prompt(DOCUMENTED, observables)
    assert "Diagnostics:" in rendered
    assert "and when a source span was resolved:" in rendered
    assert "src/main.rs:12" in rendered


def test_negative_control_pre_fix_render_drops_the_region() -> None:
    """The pre-fix behaviour, which every prompt built before this argument
    existed got: the leading window and an ellipsis, with the crate's own
    statement of the observable absent. Without this failing, the test above
    proves nothing."""
    rendered = format_type_for_prompt(DOCUMENTED)
    assert "Diagnostics:" not in rendered
    assert "and when a source span was resolved:" not in rendered
    assert rendered.splitlines()[0] == "/// A diagnostic report."
    assert any(line.endswith("…") for line in rendered.splitlines())


def test_a_type_matching_no_observable_renders_unchanged() -> None:
    """Second negative control: observables that the doc does not state
    leave the render byte-identical, so a prompt whose targets have no
    attributed failing test is untouched by this mechanism."""
    unrelated = frozenset({"a totally unrelated sentence"})
    assert (format_type_for_prompt(DOCUMENTED, unrelated)
            == format_type_for_prompt(DOCUMENTED))


def test_pinned_region_is_bounded() -> None:
    """A doc whose matching region is unbounded cannot grow a prompt without
    bound: the region is cut at the pinned-line budget, and what is kept
    still carries the observable that selected it."""
    long_doc = "\n".join([
        "/// A diagnostic report.",
        "///",
        "/// intro paragraph.",
        "///",
        "/// and when a source span was resolved:",
        "///",
        "/// ```console",
        "/// Diagnostics:",
        *[f"///     src/f{i}.rs:{i}" for i in range(400)],
        "/// ```",
        "pub struct Report {}",
    ])
    rec = TypeRecord(file="src/report.rs", kind="struct", name="Report",
                     definition=long_doc)
    rendered = format_type_for_prompt(
        rec, asserted_output_literals(PINNING_TEST))
    assert "Diagnostics:" in rendered
    assert len(rendered) < 2_500
