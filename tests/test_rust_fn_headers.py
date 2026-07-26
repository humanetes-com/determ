"""Byte-equivalence pins for `pipeline_core.domain.rust_fn_headers` (P7).

Each test compares a header projection against the exact regex it replaced in
pipeline.py: the line-anchored fn/macro scans of `_condense_rust_to_signatures`
and the lookbehind fn scan of `_merge_fn_blocks_into_disk` / `_FN_RE_GLOBAL`.
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_fn_headers import (
    fn_headers,
    locate_all_fns,
    macro_headers,
)

_FN_L = re.compile(
    r'((?:^|\n)[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
    r'(?:const\s+)?fn\s+[A-Za-z_]\w*[^{]*)\{', re.MULTILINE)
_MAC_L = re.compile(
    r'((?:^|\n)[ \t]*(?:pub(?:\([^)]*\))?\s+)?macro_rules!\s+[A-Za-z_]\w*\s*)\{',
    re.MULTILINE)
_FN_G = re.compile(
    r'((?<![A-Za-z_0-9])(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
    r'(?:const\s+)?fn\s+([A-Za-z_]\w*)[^{]*\{)')


_CASES = [
    "fn foo() {}",
    "pub fn foo() {}\nfn bar() { foo(); }",
    "    fn indented() {}",
    "pub(crate) async unsafe fn x<T>() -> T { todo!() }",
    "const fn c() {}",
    "x fn mid() {}",                      # mid-line: matches G, not L
    "} fn after_brace() {}",             # G boundary, not L
    "myfn nope() {}\nfn yes() {}",        # `myfn` must not match
    "fn no_brace()",                      # no `{` → no match
    "macro_rules! m { () => {} }",
    "pub macro_rules! pm {\n  () => {};\n}",
    "fn a() { fn nested() {} }",          # nested fn (finditer resumes inside body)
    "\n\n   pub fn blank_led() {}",       # blank line before
    "structfn glued() {}",                # `structfn` not a fn
    "",
]


# A BODYLESS declaration is where the code deliberately DIVERGES from the old
# `[^{]*\{` regexes above: they scan past the `;` and adopt the next item's
# brace, so the declaration's "body" swallows whole items. `_CASES` contains no
# such input, which is why the equivalence pins still hold — do not "restore"
# equivalence by removing the terminator check.
_TRAIT_DECL = (
    "mod not {\n"
    "    pub trait Bool {\n"
    "        fn not(self) -> bool;\n"
    "    }\n"
    "    impl Bool for bool {\n"
    "        #[inline]\n"
    "        fn not(self) -> bool {\n"
    "            !self\n"
    "        }\n"
    "    }\n"
    "    impl Bool for &bool {\n"
    "        fn not(self) -> bool {\n"
    "            !*self\n"
    "        }\n"
    "    }\n"
    "}\n"
)


def test_bodyless_trait_decl_is_not_a_locatable_fn():
    # Only the two impls define `not`; the trait declaration has no body. The
    # old scan returned a third span whose "signature" ran `fn not(self) ->
    # bool; } impl Bool for bool` — reverting it spliced over the trait's
    # closing brace and the impl header, leaving the file unparseable.
    locs = locate_all_fns(_TRAIT_DECL, "not")
    assert len(locs) == 2
    for sig, _ss, body_start, body_end in locs:
        assert sig == "fn not(self) -> bool"
        assert "impl" not in _TRAIT_DECL[body_start:body_end]
    assert _TRAIT_DECL[locs[0][2]:locs[0][3]].strip() == "!self"
    assert _TRAIT_DECL[locs[1][2]:locs[1][3]].strip() == "!*self"


def test_reverting_a_bodyless_decl_name_keeps_braces_balanced():
    # What `_revert_implicated` does, on the shape that broke the crate.
    text = _TRAIT_DECL
    for _sig, _ss, body_start, body_end in reversed(
            locate_all_fns(text, "not")):
        text = text[:body_start] + "\nunimplemented!()\n" + text[body_end:]
    assert text.count("{") == text.count("}")
    assert "impl Bool for bool {" in text
    assert "impl Bool for &bool {" in text
    assert text.count("unimplemented!()") == 2


def test_terminator_scan_ignores_semicolons_that_do_not_end_the_item():
    # `;` inside a bracketed type, a comment, or a string is not an item
    # terminator — these all have real bodies and must still be located.
    cases = [
        "fn f(x: [u8; 4]) -> bool {\n    true\n}",
        "fn f(x: u8) -> bool { // note; here\n    true\n}",
        'fn f(x: u8) -> bool {\n    let s = "a;b"; true\n}',
        "fn f<T>(x: T) -> bool\nwhere\n    T: Copy,\n{\n    true\n}",
    ]
    for src in cases:
        assert len(locate_all_fns(src, "f")) == 1, src


def test_bodyless_declarations_are_dropped_everywhere():
    for src in [
        "extern \"C\" {\n    fn abort();\n}\nfn abort() {\n    loop {}\n}",
        "trait T {\n    fn f<A>(a: A) -> bool\n    where\n        A: Copy;\n}\n"
        "fn f() {\n    ()\n}",
    ]:
        names = [h.name for h in fn_headers(src, line_anchored=False)]
        assert names.count("abort") + names.count("f") == 1, src


def test_fn_headers_line_anchored_matches_regex():
    for t in _CASES:
        old = [(m.start(1), m.group(1), m.end()) for m in _FN_L.finditer(t)]
        new = [(h.start, t[h.start:h.brace_open], h.brace_open + 1)
               for h in fn_headers(t, line_anchored=True)]
        assert new == old, t


def test_macro_headers_matches_regex():
    for t in _CASES:
        old = [(m.start(1), m.group(1), m.end()) for m in _MAC_L.finditer(t)]
        new = [(h.start, t[h.start:h.brace_open], h.brace_open + 1)
               for h in macro_headers(t)]
        assert new == old, t


def test_fn_headers_lookbehind_matches_regex():
    for t in _CASES:
        old = [(m.start(), m.group(2), m.end()) for m in _FN_G.finditer(t)]
        new = [(h.start, h.name, h.brace_open + 1)
               for h in fn_headers(t, line_anchored=False)]
        assert new == old, t

# --------------------------------------------------------------------------- #
# unwrap_own_fn_definition — a model reply that wraps the requested body in the
# target's full `fn` definition must be reduced to the inner body, or the
# splice nests a duplicate header: the outer fn then returns `()` (E0308) and
# the duplicate name makes the bare-name revert refuse `ambiguous_name`, so
# the broken body cannot even be undone (observed live: ensure.rs::render).
# --------------------------------------------------------------------------- #
from pipeline_core.domain.rust_fn_headers import unwrap_own_fn_definition


def test_unwrap_full_definition_returns_inner_body():
    body = ("fn render(msg: &'static str, lhs: &dyn Debug) -> Error {\n"
            "    let mut buf = Buf::new();\n"
            "    Error::msg(msg)\n"
            "}")
    assert unwrap_own_fn_definition(body, "render") == (
        "let mut buf = Buf::new();\nError::msg(msg)")


def test_unwrap_accepts_leading_attrs_comments_and_modifiers():
    body = ("// helper\n#[inline]\n"
            "pub(crate) unsafe fn object_drop<E>(e: Own<ErrorImpl>) {\n"
            "    drop(e)\n"
            "}")
    assert unwrap_own_fn_definition(body, "object_drop") == "drop(e)"


def test_unwrap_refuses_different_name_or_extra_content():
    # A body legitimately STARTING with a nested helper keeps its tail
    # expression — never unwrapped.
    nested = "fn helper() -> u8 { 1 }\nhelper() as usize"
    assert unwrap_own_fn_definition(nested, "len") is None
    # Trailing code after the definition = not a pure wrapper.
    trailing = "fn len(&self) -> usize { 1 }\nlet x = 2;"
    assert unwrap_own_fn_definition(trailing, "len") is None
    # Preceding real code = not a pure wrapper.
    leading = "let x = 1;\nfn len(&self) -> usize { x }"
    assert unwrap_own_fn_definition(leading, "len") is None


def test_unwrap_ignores_plain_and_truncated_bodies():
    assert unwrap_own_fn_definition("self.0.len()", "len") is None
    assert unwrap_own_fn_definition("fn len(&self) -> usize {", "len") is None
