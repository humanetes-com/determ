"""Member paths the crate's own write-protected tests read (t1.5) — pure domain.

The defect these pin: on `anyhow` (2026-08-06 17:27 UTC) t1.5 authored
`#[repr(transparent)] pub struct Error { ptr: NonNull<ErrorImpl> }`. It satisfied
all six harvested layout pins and passed `cargo check --lib`, because that never
builds `#[cfg(test)]` code. `src/error.gen_tests_3.rs:29` reads
`err.inner.ptr.as_ptr()`, so the first `cargo test` failed with
`error[E0609]: no field 'inner' on type 'Error'` and took all 56 unit tests of
both clusters with it — 44% of the 126-test oracle, unfixable by any fill route
because the only diagnostic pointed into a file no stage may rewrite.
"""
from __future__ import annotations

from pipeline_core.domain.oracle_field_paths import (
    FieldPath,
    field_path_requirements_text,
    read_field_paths,
)

# The verbatim shape of the test that convicted the authored layout.
_ANYHOW_GEN_TEST = """\
fn test_errorimpl_debug_fmt() {
    let err = Error::msg("impl debug test");

    // SAFETY: Error's inner pointer points to a valid heap allocation.
    let heap_ptr: *const ErrorImpl = err.inner.ptr.as_ptr();
    let err_impl: &ErrorImpl<()> = unsafe { &*heap_ptr.cast::<ErrorImpl<()>>() };

    assert!(format!("{:?}", err_impl).contains("impl debug test"));
}
"""


def _exprs(paths) -> list[str]:
    return [p.expr for p in paths]


def test_reads_the_path_that_convicted_the_authored_layout():
    paths = read_field_paths(
        "src/error.gen_tests_3.rs", _ANYHOW_GEN_TEST, types={"Error"})
    assert _exprs(paths) == ["Error.inner.ptr"]
    assert paths[0].root_type == "Error"
    assert paths[0].fields == ("inner", "ptr")


def test_origin_carries_file_and_line_for_the_message():
    paths = read_field_paths(
        "src/error.gen_tests_3.rs", _ANYHOW_GEN_TEST, types={"Error"})
    assert paths[0].origin == "src/error.gen_tests_3.rs:5"


def test_prose_about_a_field_is_not_a_read_of_it():
    """The SAFETY comment above the failing line says "Error's inner pointer".
    Read as code it would bind a path to a type that never appears there."""
    src = '    // SAFETY: Error\'s inner pointer is valid.\n    let e = Error::msg("x");\n'
    assert read_field_paths("t.rs", src, types={"Error"}) == ()


def test_a_slash_inside_a_string_literal_does_not_start_a_comment():
    src = 'let e = Error::msg("http://x");\nlet _ = e.inner.ptr;\n'
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_an_escaped_quote_does_not_end_the_string():
    src = 'let e = Error::msg("a\\"// b");\nlet _ = e.inner.ptr;\n'
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_method_call_ends_the_chain_before_itself():
    """`err.inner.ptr.as_ptr()` constrains `inner` and `ptr`; `as_ptr` is a
    method, and demanding it as a FIELD would reject every correct layout."""
    src = "let e = Error::msg(\"x\");\nlet _ = e.inner.ptr.as_ptr();\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_turbofish_segment_ends_the_chain():
    src = "let e = Error::msg(\"x\");\nlet _ = e.inner.downcast::<u8>();\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner"]


def test_await_is_not_a_field():
    src = "let e = Error::msg(\"x\");\nlet _ = e.inner.await;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner"]


def test_a_tuple_struct_index_is_a_field():
    """`DisplayError<M>(pub M)` is read as `.0` — a numeric segment is a real
    member constraint, not punctuation to skip."""
    src = "let e: DisplayError = make();\nlet _ = e.0;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"DisplayError"})) == [
        "DisplayError.0"]


def test_a_prefix_path_is_dropped_in_favour_of_the_deepest_read():
    """`Error.inner` is implied by `Error.inner.ptr`; keeping both would gate
    twice and point the diagnostic at the shallower hop that did not fail."""
    src = ("let e = Error::msg(\"x\");\n"
           "let _ = e.inner;\n"
           "let _ = e.inner.ptr;\n")
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_sibling_paths_on_one_root_are_both_kept():
    src = ("let e = Error::msg(\"x\");\n"
           "let _ = e.inner.ptr;\n"
           "let _ = e.vtable;\n")
    assert sorted(_exprs(read_field_paths("t.rs", src, types={"Error"}))) == [
        "Error.inner.ptr", "Error.vtable"]


def test_a_root_outside_the_component_constrains_nothing():
    """A path rooted in another component's type is that component's design to
    make; admitting it here would gate this one on a decision it does not own."""
    src = "let e = Error::msg(\"x\");\nlet _ = e.inner.ptr;\n"
    assert read_field_paths("t.rs", src, types={"Chain"}) == ()


def test_a_binding_with_no_stated_type_is_left_unresolved():
    """`let e = make_error();` names no type. Guessing the root is how a path
    gets attributed to the wrong component and freezes a layout that was fine."""
    src = "let e = make_error();\nlet _ = e.inner.ptr;\n"
    assert read_field_paths("t.rs", src, types={"Error"}) == ()


def test_a_type_annotation_resolves_the_binding():
    src = "let e: Error = make();\nlet _ = e.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_reference_annotation_resolves_the_binding():
    src = "let e: &Error = make();\nlet _ = e.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_mut_reference_annotation_resolves_the_binding():
    src = "let e: &mut Error = make();\nlet _ = e.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_qualified_path_annotation_resolves_to_its_last_segment():
    src = "let e: crate::error::Error = make();\nlet _ = e.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_generic_annotation_is_rejected():
    """`Own<ErrorImpl>` would force the probe to invent type parameters, so the
    binding is left unresolved rather than rendered as `fn p(v: &Own)`."""
    src = "let e: Own<ErrorImpl> = make();\nlet _ = e.ptr;\n"
    assert read_field_paths("t.rs", src, types={"Own"}) == ()


def test_a_raw_pointer_annotation_is_rejected():
    """Reading a field through `*const ErrorImpl` needs an unsafe deref, so the
    path is not a statement about the field's name."""
    src = "let p: *const ErrorImpl = make();\nlet _ = p.vtable;\n"
    assert read_field_paths("t.rs", src, types={"ErrorImpl"}) == ()


def test_a_lowercase_annotation_is_not_a_type_name():
    src = "let e: usize = 0;\nlet _ = e.inner;\n"
    assert read_field_paths("t.rs", src, types={"usize"}) == ()


def test_a_struct_literal_initializer_resolves_the_binding():
    src = "let e = Error { inner: x };\nlet _ = e.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_an_unsafe_block_initializer_resolves_the_binding():
    src = "let e = unsafe { Error::from_raw(p) };\nlet _ = e.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_method_chain_initializer_is_left_unresolved():
    src = "let e = other.clone();\nlet _ = e.inner.ptr;\n"
    assert read_field_paths("t.rs", src, types={"Error"}) == ()


def test_a_let_mut_binding_is_resolved():
    src = "let mut e = Error::msg(\"x\");\nlet _ = e.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_name_bound_twice_to_different_types_is_dropped():
    """With no scope tracking, the second binding's fields would be attributed
    to the first binding's type — a wrong root is what freezes a good layout."""
    src = ("let e = Error::msg(\"x\");\n"
           "let e = ErrorImpl { vtable: v };\n"
           "let _ = e.inner.ptr;\n")
    assert read_field_paths("t.rs", src, types={"Error", "ErrorImpl"}) == ()


def test_a_name_bound_twice_to_the_SAME_type_is_kept():
    src = ("let e = Error::msg(\"a\");\n"
           "let e = Error::msg(\"b\");\n"
           "let _ = e.inner.ptr;\n")
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_the_binding_name_must_match_as_a_whole_word():
    """`err_impl` starts with `err`; a substring match would read `_impl.vtable`
    as a path on `err`'s type."""
    src = ("let err = Error::msg(\"x\");\n"
           "let err_impl: ErrorImpl = other();\n"
           "let _ = err_impl.vtable;\n")
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == []


def test_a_bare_mention_of_the_binding_is_not_a_path():
    src = "let e = Error::msg(\"x\");\ndrop(e);\n"
    assert read_field_paths("t.rs", src, types={"Error"}) == ()


def test_an_uppercase_binding_name_is_not_a_local():
    src = "let E = 1;\nlet _ = E.inner;\n"
    assert read_field_paths("t.rs", src, types={"Error"}) == ()


def test_the_word_let_inside_an_identifier_is_not_a_binding():
    src = "let complete_x = Error::msg(\"x\");\nlet _ = complete_x.inner.ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_let_inside_an_identifier_followed_by_a_space_is_not_a_binding():
    """The scan looks for `let ` with its space, so most identifiers containing
    the letters cannot match — but `outlet x` can, and reading it as a binding
    would bind the name `x` to whatever followed on that line."""
    src = ("let e = Error::msg(\"x\");\n"
           "let _ = e.inner.ptr;\n"
           "let outlet = 0;\n")
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_declaration_without_type_or_initializer_binds_nothing():
    src = "let e;\ne = Error::msg(\"x\");\nlet _ = e.inner.ptr;\n"
    assert read_field_paths("t.rs", src, types={"Error"}) == ()


def test_whitespace_after_a_dot_does_not_break_the_chain():
    src = "let e = Error::msg(\"x\");\nlet _ = e. inner. ptr;\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_a_chain_continued_on_the_next_line_is_not_read():
    """The scan is per-line, so a path broken across lines yields nothing —
    under-constraining, never mis-constraining. It costs nothing on the crate
    this was built for: every `^\\s*\\.` continuation in `anyhow`'s oracle
    (6 in src/fmt.gen_tests_3.rs, the rest in tests/test_boxed.rs and
    tests/test_downcast.rs, 2026-08-06) is a METHOD call — `.unwrap()`,
    `.write_str(..)`, `.downcast_ref::<..>()` — which ends a chain on any line."""
    src = "let e = Error::msg(\"x\");\nlet _ = e\n    .inner\n    .ptr;\n"
    assert read_field_paths("t.rs", src, types={"Error"}) == ()


def test_whitespace_before_a_method_call_still_ends_the_chain():
    src = "let e = Error::msg(\"x\");\nlet _ = e.inner.ptr .as_ptr ();\n"
    assert _exprs(read_field_paths("t.rs", src, types={"Error"})) == ["Error.inner.ptr"]


def test_no_oracle_sources_means_no_constraint():
    assert read_field_paths("t.rs", "", types={"Error"}) == ()


def test_requirements_text_is_empty_when_there_is_nothing_to_require():
    """An empty note must add nothing to the prompt: a heading with no rows
    reads as a constraint the model then invents content for."""
    assert field_path_requirements_text([]) == ""


def test_requirements_text_names_every_path_and_its_origin():
    text = field_path_requirements_text([
        FieldPath("Error", ("inner", "ptr"), "src/error.gen_tests_3.rs:29"),
        FieldPath("Error", ("vtable",)),
    ])
    assert "`Error.inner.ptr`" in text
    assert "src/error.gen_tests_3.rs:29" in text
    assert "`Error.vtable`" in text
    assert "WRITE-PROTECTED" in text


def test_requirements_text_leaves_the_member_types_free():
    """The constraint is the NAME at the DEPTH, not the type: `inner: Own<T>`
    and `inner: Box<T>` both satisfy the oracle, and saying otherwise would
    reject the layout upstream actually ships."""
    text = field_path_requirements_text([FieldPath("Error", ("inner",))])
    assert "free to choose the TYPE" in text
