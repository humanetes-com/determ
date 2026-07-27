"""Pins for locating and relocating inline `#[cfg(test)] mod` blocks."""
from __future__ import annotations

from pipeline_core.domain.inline_tests import (
    find_inline_test_mods,
    splice_out_inline_test_mod,
)

_SIMPLE = (
    "pub fn indent() {}\n"
    "#[cfg(test)]\n"
    "mod tests {\n"
    "    use super::*;\n"
    "    #[test]\n"
    "    fn no_digits() {\n"
    "        assert_eq!(1, 1);\n"
    "    }\n"
    "}\n"
)


def test_locates_top_level_test_mod() -> None:
    (block,) = find_inline_test_mods(_SIMPLE)
    assert block.name == "tests"
    assert block.relocatable
    assert block.extra_cfg == ""


def test_span_starts_at_the_attribute_not_the_mod_keyword() -> None:
    """The swap must take `#[cfg(test)] mod X { … }` whole. Replacing only the
    `mod` item strands the attribute on whatever item follows it."""
    (block,) = find_inline_test_mods(_SIMPLE)
    assert _SIMPLE[block.start:].startswith("#[cfg(test)]")
    assert _SIMPLE[:block.end].endswith("}")
    out = splice_out_inline_test_mod(_SIMPLE, block, "mod gen_tests_1;\n")
    assert "#[cfg(test)]" not in out
    assert out == "pub fn indent() {}\nmod gen_tests_1;\n\n"


def test_body_is_dedented_and_excludes_the_braces() -> None:
    (block,) = find_inline_test_mods(_SIMPLE)
    assert block.body == (
        "use super::*;\n#[test]\nfn no_digits() {\n    assert_eq!(1, 1);\n}\n")


def test_path_decl_is_not_an_inline_mod() -> None:
    """`mod gen_tests_1;` is already the target shape — nothing to relocate."""
    text = '#[cfg(test)]\n#[path = "a.gen_tests_1.rs"]\nmod gen_tests_1;\n'
    assert find_inline_test_mods(text) == ()


def test_non_test_mod_is_ignored_and_walked_into() -> None:
    text = "mod ext {\n    pub trait T {}\n}\n"
    assert find_inline_test_mods(text) == ()


def test_nested_test_mod_is_refused_not_moved() -> None:
    """A `#[path]` decl resolves against the parent FILE's directory, so hoisting
    a test mod out of an inline `mod` wrapper would change which file is read."""
    text = "mod ext {\n#[cfg(test)]\nmod tests { #[test] fn a() {} }\n}\n"
    (block,) = find_inline_test_mods(text)
    assert not block.relocatable
    assert "nested" in block.refusal


def test_conjunctive_gate_keeps_its_other_conditions() -> None:
    """`all(test, feature = "x")` requires test, so the relocated decl restates
    it as `#[cfg(feature="x")] #[cfg(test)]` — the same conjunction."""
    text = '#[cfg(all(test, feature = "x"))]\nmod tests { #[test] fn a() {} }\n'
    (block,) = find_inline_test_mods(text)
    assert block.relocatable
    assert block.extra_cfg == 'feature="x"'


def test_optional_test_gate_is_refused() -> None:
    """`any(test, feature = "x")` also compiles OUTSIDE test builds; the decl's
    `#[cfg(test)]` would narrow that silently, so the block stays inline."""
    text = '#[cfg(any(test, feature = "x"))]\nmod tests { #[test] fn a() {} }\n'
    (block,) = find_inline_test_mods(text)
    assert not block.relocatable
    assert "does not require test" in block.refusal


def test_not_test_mod_is_production_code() -> None:
    """`#[cfg(not(test))]` is excluded exactly when tests build — the opposite
    of a test module, and not something to relocate or report."""
    assert find_inline_test_mods(
        "#[cfg(not(test))]\nmod real { pub fn f() {} }\n") == ()


def test_feature_whose_name_contains_test_is_not_a_test_mod() -> None:
    """The predicate never names the `test` atom — only its own feature does."""
    assert find_inline_test_mods(
        '#[cfg(feature = "testing")]\nmod helpers { pub fn f() {} }\n') == ()


def test_separate_cfg_attr_becomes_the_sibling_gate() -> None:
    text = ('#[cfg(feature = "std")]\n#[cfg(test)]\n'
            "mod tests { #[test] fn a() {} }\n")
    (block,) = find_inline_test_mods(text)
    assert block.relocatable
    assert block.extra_cfg == 'feature="std"'
    assert text[block.start:].startswith('#[cfg(feature = "std")]')


def test_commented_out_block_is_not_located() -> None:
    text = "// #[cfg(test)]\n// mod tests { #[test] fn a() {} }\nfn f() {}\n"
    assert find_inline_test_mods(text) == ()


def test_multiline_attribute_is_one_span() -> None:
    text = "#[cfg(\n  test\n)]\nmod tests { #[test] fn a() {} }\n"
    (block,) = find_inline_test_mods(text)
    assert block.relocatable
    assert text[block.start:].startswith("#[cfg(")


def test_two_blocks_report_in_source_order() -> None:
    text = ("#[cfg(test)]\nmod a { #[test] fn x() {} }\n"
            "fn mid() {}\n"
            "#[cfg(test)]\nmod b { #[test] fn y() {} }\n")
    blocks = find_inline_test_mods(text)
    assert [b.name for b in blocks] == ["a", "b"]
    assert blocks[0].end <= blocks[1].start


def test_brace_in_a_doc_example_does_not_shift_depth() -> None:
    """A `{` inside a doc comment must not make the test mod look nested."""
    text = ("/// ```\n/// let x = Foo { a: 1 };\n/// ```\n"
            "pub fn f() {}\n#[cfg(test)]\nmod tests { #[test] fn a() {} }\n")
    (block,) = find_inline_test_mods(text)
    assert block.relocatable


# --- visibility prefixes ---------------------------------------------------
# The span must swallow the item's visibility keyword. Leaving `pub` or
# `pub(crate)` stranded in front of the removed `mod` produces `pub #[cfg(test)]
# #[path…] mod tests;` — the attribute lands between the visibility and the
# item, which does not parse.
def test_pub_test_mod_span_includes_the_visibility() -> None:
    src = "#[cfg(test)]\npub mod tests {\n    fn a() {}\n}\n"
    (block,) = find_inline_test_mods(src)
    assert block.relocatable
    assert src[block.start:block.end].startswith("#[cfg(test)]")
    assert "pub mod tests" in src[block.start:block.end]
    assert splice_out_inline_test_mod(src, block, "REPLACED").strip() == "REPLACED"


def test_pub_crate_test_mod_span_includes_the_restricted_visibility() -> None:
    src = "#[cfg(test)]\npub(crate) mod tests {\n    fn a() {}\n}\n"
    (block,) = find_inline_test_mods(src)
    assert "pub(crate) mod tests" in src[block.start:block.end]
    assert splice_out_inline_test_mod(src, block, "X").strip() == "X"


def test_pub_in_path_test_mod_span_includes_the_whole_restriction() -> None:
    src = "#[cfg(test)]\npub(in crate::a) mod tests {\n    fn a() {}\n}\n"
    (block,) = find_inline_test_mods(src)
    assert "pub(in crate::a) mod tests" in src[block.start:block.end]


def test_inner_attribute_stops_the_attribute_walk_back() -> None:
    """`#![…]` is the FILE's attribute, not the mod's — the span must not
    swallow it, or relocating the tests would move a crate-level attribute."""
    src = ("#![allow(dead_code)]\n"
           "#[cfg(test)]\nmod tests {\n    fn a() {}\n}\n")
    (block,) = find_inline_test_mods(src)
    assert src[block.start:].startswith("#[cfg(test)]")
    assert "#![allow(dead_code)]" not in src[block.start:block.end]


def test_unbalanced_braces_locate_nothing() -> None:
    """A file that does not parse must yield no block rather than a span that
    runs to EOF and deletes the rest of the file on splice."""
    assert find_inline_test_mods("#[cfg(test)]\nmod tests {\n  fn a() {}\n") == ()


def test_bare_test_mod_without_any_cfg_is_not_relocated() -> None:
    # No cfg gate at all: not a conditionally-compiled test mod.
    assert find_inline_test_mods("mod tests {\n    fn a() {}\n}\n") == ()
