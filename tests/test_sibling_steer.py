"""`domain/sibling_steer.py` — the SIBLING_REFERENCE vs LAST_ERROR contradiction.

The instance is run `902902-daee577d` (2026-08-20), prompt `11899c17478d`:
attempt 2 of `impl<C> StdError for ContextError<C, Error>` carried the E0277
on `Some(&self.error)` in LAST_ERROR and, under "follow the same structural
pattern", the `<C, E>` sibling whose body contains that very expression. The
model followed the sibling: `identical errors after attempt 2 — stopping`.
"""
from __future__ import annotations

from pipeline_core.domain.sibling_steer import rejected_expressions, sibling_steer

LAST_ERROR_E0277 = """# Errors in target file:
error[E0277]: the trait bound `Error: core::error::Error` is not satisfied
   --> src/context.rs:148:14
    |
148 |         Some(&self.error)
    |              ^^^^^^^^^^^ unsatisfied trait bound
    |
help: the trait `core::error::Error` is not implemented for `Error`
   --> src/lib.rs:273:1
    |
273 | pub struct Error {
    | ^^^^^^^^^^^^^^^^
    = note: required for the cast from `&Error` to `&dyn core::error::Error`
"""

# The sibling AS IT STOOD at attempt 2 — not byte-equal to the failing body:
# it had grown a TypeId branch around the same `Some(&self.error)`.
SIBLING_TYPEID = """fn source(&self) -> Option<&(dyn StdError + 'static)> {
if core::any::TypeId::of::<E>() == core::any::TypeId::of::<crate::Error>() {
            None
        } else {
            Some(&self.error)
        }
}"""


def test_rejected_expressions_are_the_quoted_source_lines_minus_structure() -> None:
    # `273 | pub struct Error {` is quoted too (the help span) — a declaration,
    # not an expression; and the caret/label lines have no line number.
    assert rejected_expressions(LAST_ERROR_E0277) == ("Some(&self.error)",)


def test_the_instance_flips_even_though_the_sibling_is_not_byte_equal() -> None:
    steer = sibling_steer(SIBLING_TYPEID, LAST_ERROR_E0277)
    assert steer.contradicts
    assert steer.shared == ("Some(&self.error)",)


def test_no_contradiction_on_attempt_one_or_without_a_sibling() -> None:
    assert not sibling_steer(SIBLING_TYPEID, "").contradicts
    assert not sibling_steer("", LAST_ERROR_E0277).contradicts


def test_a_sibling_on_a_different_path_is_still_endorsed() -> None:
    other = ("fn source(&self) -> Option<&(dyn StdError + 'static)> {\n"
             "Some(unsafe { crate::ErrorImpl::error(self.error.inner.by_ref()) })\n}")
    assert not sibling_steer(other, LAST_ERROR_E0277).contradicts


def test_a_shared_signature_is_not_a_shared_pattern() -> None:
    # An error that points at the fn HEADER (E0053-style) quotes a line every
    # same-named sibling shares by construction. That is not steering.
    err = ("error[E0053]: method `source` has an incompatible type for trait\n"
           "  --> src/context.rs:147:5\n"
           "   |\n"
           "147 |     fn source(&self) -> Option<&(dyn StdError + 'static)> {\n"
           "   |     ^^^ expected ...\n")
    assert rejected_expressions(err) == ()
    assert not sibling_steer(SIBLING_TYPEID, err).contradicts


def test_fragments_too_short_to_name_a_pattern_are_dropped() -> None:
    err = ("error[E0308]: mismatched types\n"
           "  --> src/x.rs:9:9\n"
           "   |\n"
           "9  |         None\n"
           "10 |     }\n"
           "11 |         self.x.by_ref().chain()\n")
    assert rejected_expressions(err) == ("self.x.by_ref().chain()",)


def test_whitespace_differences_do_not_hide_the_match() -> None:
    sib = "fn f(&self) -> u32 {\n    self.tag\n        .wrapping_add(1)\n}"
    err = ("error[E0599]: no method named `wrapping_add`\n"
           "  --> src/lib.rs:4:9\n"
           "   |\n"
           "4  |         self.tag .wrapping_add(1)\n")
    assert sibling_steer(sib, err).shared == ("self.tag .wrapping_add(1)",)
