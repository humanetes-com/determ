"""A still-stubbed function's first fill shows what its failing tests that
already run past every stub printed.

Run `39786-fd38e72f` (2026-10-09, cluster `stable`, fill 3):
`test_large_alignment` ran past every stub, owed the stubbed
`src/fmt.rs::debug`, and its failure (`called Option::unwrap() on a None
value` at `tests/test_downcast.rs:113:51`) reached none of the prompts
written for that stub: the re-fill sections read `refill_members` only and
the per-fn stub prompt prints no test's words. A test held in a stub
contributes nothing: its words are the stub's own `not implemented`."""
from __future__ import annotations

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.refill_evidence import observed_stub_excerpts
from pipeline_core.domain.refill_evidence import tests_in_prompt as prompt_tests
from pipeline_core.prompts.context import PerFnSections
from pipeline_core.prompts.per_fn import build_per_fn_prompt

_DEBUG = FnLocator("src/fmt.rs", "debug")
_DEBUG_SITE = FnSite("src/fmt.rs", "debug", "")
_RAN = ("thread 'test_large_alignment' panicked at tests/test_downcast.rs:113:51:\n"
        "called `Option::unwrap()` on a `None` value")
_HELD = "thread 'test_debug' panicked at src/fmt.rs:12:5:\nnot implemented"


def _frontier(held: tuple[str, ...]) -> FailureFrontier:
    tests = {"test_large_alignment", "test_debug"}
    return FailureFrontier.build(
        {_DEBUG: set(tests)}, {_DEBUG: set(tests)},
        evidence={"test_large_alignment": _RAN, "test_debug": _HELD},
        held=held)


def test_a_test_that_ran_past_every_stub_reaches_the_stubs_first_fill() -> None:
    text = observed_stub_excerpts((_DEBUG_SITE,), _frontier(("test_debug",)))
    assert "called `Option::unwrap()` on a `None` value" in text
    assert "OBSERVED for `debug`" in text


def test_control_a_test_held_in_a_stub_adds_nothing() -> None:
    text = observed_stub_excerpts((_DEBUG_SITE,), _frontier(("test_debug",)))
    assert "not implemented" not in text
    held_all = observed_stub_excerpts(
        (_DEBUG_SITE,), _frontier(("test_debug", "test_large_alignment")))
    assert held_all == ""


def test_held_names_match_whatever_path_prefix_the_test_carries() -> None:
    fr = _frontier(("tests/test_fmt.rs::test_debug",))
    assert fr.observed_tests_for(_DEBUG) == {"test_large_alignment"}


def test_an_owed_test_is_printed_ahead_of_the_alphabetical_rest() -> None:
    """Five observed tests, the owed one last alphabetically. Its words
    lead the stub prompt, and the cap of six still drops the last of the
    rest when a seventh test is observed."""
    names = [f"test_{c}" for c in "abcdef"] + ["test_z"]
    evidence = {n: f"panic {n} unique" for n in names}
    fr = FailureFrontier.build(
        {_DEBUG: set(names)}, {_DEBUG: set(names)}, evidence=evidence,
        owed={_DEBUG: frozenset({"test_z"})})
    text = observed_stub_excerpts((_DEBUG_SITE,), fr)
    assert "panic test_z unique" in text
    assert text.index("panic test_z unique") < text.index("panic test_a unique")
    assert "panic test_f unique" not in text
    held = observed_stub_excerpts(
        (_DEBUG_SITE,), FailureFrontier.build(
            {_DEBUG: set(names)}, {_DEBUG: set(names)}, evidence=evidence,
            held=names, owed={_DEBUG: frozenset({"test_z"})}))
    assert held == ""


def test_the_per_fn_stub_prompt_renders_the_observed_section() -> None:
    empty = {"macros": "", "notes": "", "container": "", "types": "",
             "invariants": "", "siblings": "", "crate_idioms": "",
             "bound_methods": ""}
    observed = observed_stub_excerpts((_DEBUG_SITE,), _frontier(("test_debug",)))
    with_it = build_per_fn_prompt(
        file_text="fn debug() { unimplemented!() }", fn_name="debug",
        sig_text="fn debug(&self) -> fmt::Result", test_blocks=[],
        module_name="anyhow", cluster_name="stable", file_rel="src/fmt.rs",
        sections=PerFnSections(**empty, observed=observed))
    assert "<OBSERVED>" in with_it and "Option::unwrap()" in with_it
    without = build_per_fn_prompt(
        file_text="fn debug() { unimplemented!() }", fn_name="debug",
        sig_text="fn debug(&self) -> fmt::Result", test_blocks=[],
        module_name="anyhow", cluster_name="stable", file_rel="src/fmt.rs",
        sections=PerFnSections(**empty))
    assert "<OBSERVED>" not in without


def test_a_body_is_shown_only_the_tests_that_convict_it() -> None:
    """Two bodies named `fmt` in one file, told apart by the sweep's
    convicting map. The refill seed, the file and impl refill sections and
    the stub's OBSERVED block read `tests_in_prompt`; the per-fn seed read the
    locator before, so the Display body was told a Debug test failed. The
    claim: a prompt for one body prints only the tests that convict that
    body. Control: when the sweep told the containers apart, an empty
    container is a site the map does not name, so the prompt shows nothing.
    One container still leaves the locator's tests."""
    loc = FnLocator("src/error.rs", "fmt")
    debug, display = "impl Debug for Error", "impl Display for Error"
    fr = FailureFrontier.build(
        {loc: {"test_debug", "test_display"}},
        {FnSite("src/error.rs", "fmt", debug): {"test_debug"},
         FnSite("src/error.rs", "fmt", display): {"test_display"}},
        evidence={"test_debug": "thread 'test_debug' panicked at tests/t.rs:1:1:\ndebug words",
                  "test_display": "thread 'test_display' panicked at tests/t.rs:2:1:\ndisplay words"})
    assert prompt_tests(fr, FnSite("src/error.rs", "fmt", display),
                           observed_only=False) == ("test_display",)
    assert prompt_tests(fr, FnSite("src/error.rs", "fmt", debug),
                           observed_only=False) == ("test_debug",)
    shown = observed_stub_excerpts((FnSite("src/error.rs", "fmt", display),), fr)
    assert "display words" in shown and "debug words" not in shown
    # Control: the sweep told the two containers apart, and the caller
    # named neither. The locator pool would credit one body with the other.
    assert prompt_tests(fr, FnSite("src/error.rs", "fmt", ""),
                        observed_only=False) == ()
    one = FailureFrontier.build(
        {loc: {"test_only"}},
        {FnSite("src/error.rs", "fmt", debug): {"test_only"}},
        evidence={})
    assert set(prompt_tests(one, FnSite("src/error.rs", "fmt", ""),
                            observed_only=False)) == {"test_only"}


def test_the_showed_words_audit_reads_every_body_of_the_name() -> None:
    """The ask ledger's unit is `file::fn`, so the audit that records whether
    an ask's prompt carried a test's words asks by name. Each body the sweep
    told apart has its own prompt pool; the name's is their union. Control:
    the empty-container site the audit passed before shows nothing on a name
    the sweep told apart, so every homonym read "words not shown"."""
    from pipeline_core.domain.refill_evidence import tests_shown_for_name

    loc = FnLocator("src/error.rs", "fmt")
    debug, display = "impl Debug for Error", "impl Display for Error"
    fr = FailureFrontier.build(
        {loc: {"test_debug", "test_display"}},
        {FnSite("src/error.rs", "fmt", debug): {"test_debug"},
         FnSite("src/error.rs", "fmt", display): {"test_display"}},
        evidence={"test_debug": "debug words", "test_display": "display words"})
    assert tests_shown_for_name(fr, "src/error.rs", "fmt") == {
        "test_debug", "test_display"}
    assert prompt_tests(fr, FnSite("src/error.rs", "fmt", "")) == ()
    # One body as far as the sweep can say: the name's pool is the locator's.
    one = FailureFrontier.build(
        {loc: {"test_debug"}},
        {FnSite("src/error.rs", "fmt", debug): {"test_debug"}},
        evidence={"test_debug": "debug words"})
    assert tests_shown_for_name(one, "src/error.rs", "fmt") == {"test_debug"}
