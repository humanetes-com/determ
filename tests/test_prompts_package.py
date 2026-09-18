"""Pins pipeline_core/prompts/ package.

Covers what the move changed (everything else is pinned by the existing
smoke tests through the pipeline.py aliases):
  1. extract_trait_from_container: regex-free rewrite FIXES two silent
     misses of the old regex (`impl<T> Trait for X`, nested generics).
  2. runtime facade: inert defaults without the pipeline; configure() wires.
  3. build_dispatch_family_section: explicit char-limit param caps macro defs.
"""

from pathlib import Path

from pipeline_core.domain.models import DispatchFamily
from pipeline_core.prompts import runtime
from pipeline_core.prompts.sections import (
    build_dispatch_family_section,
    extract_trait_from_container,
    trait_contract_for,
)


def test_extract_trait_from_container() -> None:
    # Forms the old regex handled.
    assert extract_trait_from_container("impl Display for X") == "Display"
    assert extract_trait_from_container("impl fmt::Display for Foo<'a>") == "Display"
    assert extract_trait_from_container("impl Foo") is None          # inherent
    assert extract_trait_from_container("") is None
    assert extract_trait_from_container("fn free()") is None
    # Forms the old regex silently MISSED (returned None, so the catalog
    # driver had no trait name to look up): generic impls (regex demanded
    # whitespace right after `impl`) ...
    assert extract_trait_from_container("impl<T> From<T> for X") == "From"
    assert extract_trait_from_container("impl<'a, T: ?Sized> AsRef<T> for Y<'a, T>") == "AsRef"
    # ... and nested generic arguments (regex stopped at the first `>`).
    assert extract_trait_from_container("impl Trait<Vec<U>> for X") == "Trait"
    assert extract_trait_from_container("impl<T: AsRef<str>> Bar for T") == "Bar"
    # Unbalanced generics → unsupported form, not a crash.
    assert extract_trait_from_container("impl<T Unclosed") is None
    print("  OK  extract_trait_from_container")


def test_trait_contract_lookup() -> None:
    """The catalog table still exists (LANGUAGE_DRIVER=catalog A/B).
    Production builders go through the bound driver; generic injects
    nothing — that pin lives in test_language_driver.py."""
    assert "Display::fmt" in trait_contract_for("impl Display for X")
    # The generic-impl fix makes contracts reachable for generic impls too.
    assert "From<T>::from" in trait_contract_for("impl<T> From<T> for X")
    assert trait_contract_for("impl UnknownTrait for X") == ""
    print("  OK  trait_contract_for")


def test_runtime_defaults_and_bind() -> None:
    old = runtime._DEPS
    try:
        runtime.bind(runtime.PromptRuntimeDeps())  # inert defaults
        assert runtime.annotate_file_view("fn a() {}", "a") == "fn a() {}"
        assert runtime.promoted_error_rules(Path("/nonexistent")) == ""
        runtime.bind(runtime.PromptRuntimeDeps(
            annotate_file_view=lambda text, fn: f"ANNOTATED:{fn}",
            promoted_error_rules=lambda fr_path=None: "RULES",
        ))
        assert runtime.annotate_file_view("x", "f") == "ANNOTATED:f"
        assert runtime.promoted_error_rules() == "RULES"
    finally:
        runtime.bind(old)  # frozen deps → restore the pipeline-bound instance
    print("  OK  prompts runtime facade")


def test_dispatch_family_section_cap() -> None:
    fam = DispatchFamily.from_dict({
        "traits": ["KindA", "KindB"], "method": "kind",
        "macros": ["dispatch!"], "chained_methods": ["build"],
        "macro_defs": ["macro_rules! dispatch { () => {" + "x" * 5000 + "} }"],
        "return_types": ["TagA", "TagB"],
    })
    out = build_dispatch_family_section(fam, single_call_char_limit=16000)
    assert "<DISPATCH_FAMILY>" in out and "`KindA`→`TagA`" in out
    # cap = max(2000, 16000 // 8) = 2000 → the 5k macro def is truncated
    assert "macro truncated" in out
    big = build_dispatch_family_section(fam, single_call_char_limit=160000)
    assert "macro truncated" not in big  # cap 20000 > def size
    print("  OK  dispatch family section cap")
