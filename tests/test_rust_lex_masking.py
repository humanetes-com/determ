"""Pins for comment/string masking in the fn-header scan (rust_lex + rust_fn_headers).

The corruption class these prevent: a `fn fmt(...) {` inside a `///` doc-comment
usage example was matched as a real header and got a body spliced into the
comment, breaking the file's syntax crate-wide (`unexpected closing delimiter`).
"""
from __future__ import annotations

from pipeline_core.adapters.retros import is_fn_fully_exhausted, retro_key
from pipeline_core.domain.rust_fn_headers import (
    fn_headers,
    locate_all_fns,
    locate_fn,
)
from pipeline_core.domain.rust_lex import code_mask


def _masked(text: str, needle: str) -> bool:
    i = text.index(needle)
    m = code_mask(text)
    return all(m[i:i + len(needle)])


def test_mask_line_and_doc_comments():
    t = 'let x = 1; // fn bogus() {}\n/// # fn fmt(&self) -> R {\nfn real() {}'
    assert _masked(t, "fn bogus")
    assert _masked(t, "fn fmt")
    assert not _masked(t, "fn real")


def test_mask_block_comment_nested():
    t = "/* outer /* fn inner() {} */ still */ fn real() {}"
    assert _masked(t, "fn inner")
    assert not _masked(t, "fn real")


def test_mask_strings_raw_and_escaped():
    t = 'let s = "fn s() {"; let r = r#"fn raw() {"#; fn real() {}'
    assert _masked(t, "fn s")
    assert _masked(t, "fn raw")
    assert not _masked(t, "fn real")
    escaped = 'let s = "fn x\\ny() {"; fn real() {}'
    assert _masked(escaped, "fn x")
    byte_s = 'let s = b"fn bytes() {"; fn real() {}'
    assert _masked(byte_s, "fn bytes")


def test_mask_char_literal_but_not_lifetime():
    t = "let c = '}'; fn real<'a>(x: &'a str) {}"
    m = code_mask(t)
    assert m[t.index("'}'")]
    assert not _masked(t, "fn real")
    assert not m[t.index("'a>")]  # lifetime stays code


def test_fn_headers_skip_doc_comment_examples():
    t = (
        "/// ```\n"
        "/// # impl Debug for E {\n"
        "/// #     fn fmt(&self, f: &mut Formatter) -> Result {\n"
        "/// #         todo!()\n"
        "/// #     }\n"
        "/// # }\n"
        "/// ```\n"
        "pub fn context(self) -> E { self }\n"
    )
    names = [h.name for h in fn_headers(t, line_anchored=False)]
    assert names == ["context"]
    assert locate_fn(t, "fmt") is None


def test_locate_all_fns_finds_every_real_occurrence():
    t = (
        "impl Debug for E {\n"
        "    fn fmt(&self, f: &mut F) -> R { a() }\n"
        "}\n"
        "impl Display for E {\n"
        "    fn fmt(&self, f: &mut F) -> R { b() }\n"
        "}\n"
    )
    locs = locate_all_fns(t, "fmt")
    assert len(locs) == 2
    bodies = [t[bs:be] for _, _, bs, be in locs]
    assert "a()" in bodies[0] and "b()" in bodies[1]


def test_locate_fn_brace_in_string_does_not_end_body():
    t = 'fn f() { let s = "}"; tail() }\nfn g() {}'
    loc = locate_fn(t, "f")
    assert loc is not None
    _, _, bs, be = loc
    assert "tail()" in t[bs:be]


def test_downstream_only_records_never_read_as_exhausted():
    # Records persisted before the writer stopped exhausting on
    # downstream_only must heal on read, not via a state migration.
    key = retro_key("src/nightly.rs", "provide", "impl Error for MyError")
    retros = {key: {"fully_exhausted": True, "signal": "downstream_only"}}
    assert not is_fn_fully_exhausted(
        retros, "src/nightly.rs", "provide", "impl Error for MyError")
    retros[key]["signal"] = ""
    assert is_fn_fully_exhausted(
        retros, "src/nightly.rs", "provide", "impl Error for MyError")
