"""Splice-gate pins — transactional, parse-gated fill-tree writes."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.application.reconstruct.splice import (
    replace_fn_signature,
    splice_fn_body,
    splice_fn_body_at,
)
from pipeline_core.application.reconstruct.splice_txn import parse_guarded_write
from pipeline_core.domain.llm_output import (
    xml_unescape_strict,
    xml_unescaped_variant,
)
from pipeline_core.domain.rust_scan import (
    body_without_own_closing_brace,
    top_level_item_names,
)

_REJECT = lambda _p: False   # noqa: E731 — "syn says broken"
_ACCEPT = lambda _p: True    # noqa: E731


def test_parse_guarded_write_commits_when_valid(tmp_path) -> None:
    f = tmp_path / "a.rs"
    f.write_bytes(b"old")
    assert parse_guarded_write(f, b"new", b"old", _ACCEPT) is True
    assert f.read_bytes() == b"new"


def test_parse_guarded_write_rolls_back_when_broken(tmp_path) -> None:
    f = tmp_path / "a.rs"
    f.write_bytes(b"old")
    assert parse_guarded_write(f, b"BROKEN", b"old", _REJECT) is False
    assert f.read_bytes() == b"old"   # pre-image restored, byte-exact


def test_parse_guarded_write_no_validator_always_commits(tmp_path) -> None:
    f = tmp_path / "a.rs"
    f.write_bytes(b"old")
    assert parse_guarded_write(f, b"new", b"old", None) is True
    assert f.read_bytes() == b"new"


def test_splice_fn_body_rejected_leaves_bytes_untouched(tmp_path) -> None:
    f = tmp_path / "m.rs"
    original = "fn a() {\n    unimplemented!()\n}\n"
    f.write_text(original, encoding="utf-8")
    out = splice_fn_body(f, "a", "let x = 1; x", validate_parse=_REJECT)
    assert out.ok is False and out.reason == "parse_broken"
    assert f.read_text(encoding="utf-8") == original   # rolled back


def test_splice_fn_body_committed_when_parse_ok(tmp_path) -> None:
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n", encoding="utf-8")
    out = splice_fn_body(f, "a", "1 + 1", validate_parse=_ACCEPT)
    assert out.ok is True and out.reason == "ok"
    assert "1 + 1" in f.read_text(encoding="utf-8")


def test_splice_fn_body_at_rolls_back(tmp_path) -> None:
    f = tmp_path / "m.rs"
    original = "fn a() { unimplemented!() }\n"
    f.write_bytes(original.encode())
    bs = original.index("unimplemented")
    be = original.index(" }")
    out = splice_fn_body_at(f, bs, be, "0", validate_parse=_REJECT)
    assert out.ok is False and out.reason == "parse_broken"
    assert f.read_bytes() == original.encode()


def test_all_body_splice_writers_expose_the_gate() -> None:
    # Invariant: every fill-tree body-splice writer accepts `validate_parse`, so
    # no model-output write can bypass the parse gate. If a new splice writer is
    # added, wire it through parse_guarded_write and extend this list.
    import inspect

    from pipeline_core.application.reconstruct import splice as _s
    for name in ("splice_fn_body", "splice_fn_body_at", "splice_fn_with_where"):
        sig = inspect.signature(getattr(_s, name))
        assert "validate_parse" in sig.parameters, name


# --- real extractor integration (the true syn signal) ----------------------- #

_BIN = (Path(__file__).parent.parent / "tools" / "skeleton_extractor"
        / "target" / "release" / "skeleton-extractor")


@pytest.mark.skipif(not _BIN.exists(), reason="extractor binary not built")
def test_real_parse_check_catches_broken_splice(tmp_path) -> None:
    from pipeline_core.adapters.extractor import CliExtractor
    from pipeline_core.adapters.structure import CliStructureReader

    reader = CliStructureReader(CliExtractor(_BIN))
    good = tmp_path / "good.rs"
    good.write_text("fn a() -> u32 { 1 }\n", encoding="utf-8")
    bad = tmp_path / "bad.rs"
    bad.write_text("fn a() -> u32 { let x = ; }\n", encoding="utf-8")
    assert reader.parse_ok(good) is True
    assert reader.parse_ok(bad) is False

    # end-to-end: an invalid body is rejected + bytes restored via the real gate.
    f = tmp_path / "m.rs"
    original = "fn a() -> u32 {\n    unimplemented!()\n}\n"
    f.write_text(original, encoding="utf-8")
    out = splice_fn_body(f, "a", "let x = ; x", validate_parse=reader.parse_ok)
    assert out.reason == "parse_broken"
    assert f.read_text(encoding="utf-8") == original


# --- XML-entity bodies: the reply escapes the body, not just the attributes ---


def test_xml_unescape_strict_resolves_the_five_named_entities() -> None:
    assert xml_unescape_strict("match &amp;mut self.state") == "match &mut self.state"
    assert xml_unescape_strict("Vec&lt;T&gt;") == "Vec<T>"
    assert xml_unescape_strict("&quot;x&quot; &apos;y&apos;") == '"x" \'y\''
    assert xml_unescape_strict("&#38;&#x3C;") == "&<"


def test_xml_unescape_strict_leaves_bare_idents_alone() -> None:
    # The reason html.unescape is banned here: HTML5 resolves a named
    # reference WITHOUT its semicolon, so these ordinary Rust borrows would
    # come back as `&ersand` / `>r_bound` / `<_cmp`.
    for src in ("&ampersand", "&gtr_bound", "&lt_cmp", "&quotient", "&aposition"):
        assert xml_unescape_strict(src) == src
    assert xml_unescape_strict("let r = &amp;") == "let r = &"   # terminated → real


def test_xml_unescaped_variant_is_none_when_nothing_changes() -> None:
    assert xml_unescaped_variant("match &mut self.state") is None
    assert xml_unescaped_variant("&amp;x") == "&x"


def test_splice_falls_back_to_unescaped_body_only_after_parse_failure(tmp_path) -> None:
    seen: list[str] = []

    def _validate(p: Path) -> bool:
        text = p.read_text(encoding="utf-8")
        seen.append(text)
        return "&amp;" not in text          # stands in for syn rejecting it

    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n", encoding="utf-8")
    out = splice_fn_body(f, "a", "foo(&amp;self.x)", validate_parse=_validate)
    assert out.ok is True and out.reason == "ok_xml_unescaped"
    assert "foo(&self.x)" in f.read_text(encoding="utf-8")
    # verbatim was tried FIRST — the fallback is gated on the parse failure
    assert "&amp;" in seen[0]


def test_splice_keeps_a_body_that_parses_verbatim(tmp_path) -> None:
    # A legitimate `&amp;` inside a string literal parses, so the entity
    # reading must never be substituted for it.
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n", encoding="utf-8")
    body = 'let s = "Tom &amp; Jerry"; println!("{}", s)'
    out = splice_fn_body(f, "a", body, validate_parse=_ACCEPT)
    assert out.ok is True and out.reason == "ok"
    assert "&amp;" in f.read_text(encoding="utf-8")


def test_splice_reports_parse_broken_when_neither_reading_parses(tmp_path) -> None:
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n", encoding="utf-8")
    original = f.read_text(encoding="utf-8")
    out = splice_fn_body(f, "a", "let x = &amp;;", validate_parse=_REJECT)
    assert out.ok is False and out.reason == "parse_broken"
    assert f.read_text(encoding="utf-8") == original


# ---- the reply that closes the body itself -------------------------------
# nightly/src/error.rs, 2026-08-03 10:37: every one of a 17-body file-first
# reply ended `\n    }`, every splice refused `item_set_changed`, and the whole
# attempt landed nothing. The reply for the same file 12 minutes earlier closed
# no body and spliced 17 for 17 — a per-reply slip, not a prompt property.

def test_body_that_restates_the_fns_closing_brace_still_lands(tmp_path) -> None:
    f = tmp_path / "m.rs"
    f.write_text("fn a() -> i32 {\n    unimplemented!()\n}\n"
                 "fn b() -> i32 {\n    7\n}\n", encoding="utf-8")
    out = splice_fn_body(f, "a", "    let x = 1;\n    x\n    }",
                         validate_parse=_ACCEPT)
    assert out.ok is True and out.reason == "ok_own_brace_dropped"
    text = f.read_text(encoding="utf-8")
    assert top_level_item_names(text) == ["fn a", "fn b"]
    assert text.count("}") == 2          # no brace left over


def test_the_brace_repair_is_gated_on_the_item_set_refusal(tmp_path) -> None:
    # A balanced body is written verbatim: the repair may only ever turn a
    # refusal into a fill, never rewrite an answer that was already right.
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n", encoding="utf-8")
    out = splice_fn_body(f, "a", "if c { g() } else { h() }", validate_parse=_ACCEPT)
    assert out.ok is True and out.reason == "ok"


def test_a_body_that_escapes_its_braces_some_other_way_is_still_refused(
        tmp_path) -> None:
    # Two unmatched closes — one brace short of the shape the repair can prove,
    # so the fingerprint's refusal stands and the file is untouched. The
    # following `fn b` is what makes the escape VISIBLE to the fingerprint: at
    # depth -2 it is no longer a depth-0 item.
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n"
                 "fn b() -> i32 {\n    7\n}\n", encoding="utf-8")
    original = f.read_text(encoding="utf-8")
    out = splice_fn_body(f, "a", "let x = 1;\n}\n}", validate_parse=_ACCEPT)
    assert out.ok is False and out.reason == "item_set_changed"
    assert f.read_text(encoding="utf-8") == original


def test_body_escaping_to_declare_a_sibling_item_is_still_refused(tmp_path) -> None:
    # The defect the fingerprint exists for: the body closes the fn early and
    # declares its own item at file scope (E0255 against the real definition).
    # Balance is -1 and it ends `}`, so the repair is ATTEMPTED — and rejected,
    # because dropping that brace does not restore the item list.
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n", encoding="utf-8")
    original = f.read_text(encoding="utf-8")
    out = splice_fn_body(f, "a", "let x = 1;\n}\nfn sneak() -> i32 { 1 }",
                         validate_parse=_ACCEPT)
    assert out.ok is False and out.reason == "item_set_changed"
    assert f.read_text(encoding="utf-8") == original


def test_own_brace_repair_reads_braces_as_code_not_as_text() -> None:
    # A `}` in a comment or a string closes nothing, so a body carrying one is
    # balanced and must be left exactly as the model wrote it.
    assert body_without_own_closing_brace('let s = "}";') is None
    assert body_without_own_closing_brace("let x = 1; // }") is None
    assert body_without_own_closing_brace("if c { g() }") is None
    # ...and the real shape, with the brace masked-safe at the end
    assert body_without_own_closing_brace(
        "let s = \"}\";\n    s.len()\n    }") == 'let s = "}";\n    s.len()'


def test_own_brace_repair_sees_past_a_trailing_comment() -> None:
    # `}  // done` is the same slip wearing a comment: the brace is still the
    # last thing the body CLOSES, and a comment closes nothing.
    assert (body_without_own_closing_brace("let x = 1;\n    }  // done")
            == "let x = 1;\n      // done")


@pytest.mark.skipif(not _BIN.exists(), reason="extractor binary not built")
def test_real_parser_recovers_the_escaped_body_that_was_being_lost(tmp_path) -> None:
    from pipeline_core.adapters.extractor import CliExtractor
    from pipeline_core.adapters.structure import CliStructureReader

    reader = CliStructureReader(CliExtractor(_BIN))
    f = tmp_path / "m.rs"
    f.write_text("fn a(s: &str) -> usize {\n    unimplemented!()\n}\n",
                 encoding="utf-8")
    # Verbatim, this is the body chain.rs/error.rs lost all run: not Rust.
    out = splice_fn_body(f, "a", "core::mem::size_of_val(&amp;s)",
                         validate_parse=reader.parse_ok)
    assert out.ok is True and out.reason == "ok_xml_unescaped"
    assert reader.parse_ok(f) is True


def test_replace_fn_signature_is_parse_gated(tmp_path) -> None:
    # A signature edit has no body offsets to fall back to, so an unguarded
    # write of a bad sig would PERSIST an unparseable file.
    f = tmp_path / "m.rs"
    original = "fn a(x: u32) -> u32 {\n    x\n}\n"
    f.write_text(original, encoding="utf-8")
    assert replace_fn_signature(f, "a", "fn a(x: u64) -> u64",
                                validate_parse=_REJECT) is False
    assert f.read_text(encoding="utf-8") == original


def test_replace_fn_signature_recovers_an_escaped_signature(tmp_path) -> None:
    f = tmp_path / "m.rs"
    f.write_text("fn a(x: u32) -> u32 {\n    x\n}\n", encoding="utf-8")

    def _validate(p: Path) -> bool:
        return "&amp;" not in p.read_text(encoding="utf-8")

    # `declares_fn` accepts this as a declaration of `a` — the parse gate is
    # what stops the escaped rendering from being written verbatim.
    assert replace_fn_signature(f, "a", "fn a(x: &amp;str) -> usize",
                                validate_parse=_validate) is True
    assert "fn a(x: &str) -> usize" in f.read_text(encoding="utf-8")


def test_revert_writes_a_stub_back_but_a_fill_still_cannot(tmp_path) -> None:
    # Undoing a fill legitimately restores `unimplemented!()`; the stub refusal
    # exists to stop the MODEL answering with one, not the pipeline reverting.
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    let x = 1;\n}\n", encoding="utf-8")
    assert splice_fn_body(f, "a", "unimplemented!()", allow_nonstub=True,
                          validate_parse=_ACCEPT).reason == "body_is_stub"
    out = splice_fn_body(f, "a", "unimplemented!()", allow_nonstub=True,
                         allow_stub_body=True, validate_parse=_ACCEPT)
    assert out.ok is True
    assert "unimplemented!()" in f.read_text(encoding="utf-8")


def test_revert_body_scoped_touches_only_its_own_fn(tmp_path) -> None:
    # The regression this exists for: a losing unit used to restore a WHOLE-FILE
    # snapshot taken before it queued for the splice lock, erasing sibling fills
    # that landed meanwhile and had already been reported as filled.
    from pipeline_core.adapters.fill_engine_roles import MutatorAdapter

    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n"
                 "fn b() {\n    unimplemented!()\n}\n", encoding="utf-8")
    mut = MutatorAdapter(
        locate_fn_by_record_fn=lambda *a, **k: None,   # extractor-absent path
        locate_fn_fn=lambda *a: None,
        splice_fn_body_at_fn=splice_fn_body_at,
        splice_fn_body_fn=splice_fn_body,
        replace_fn_signature_fn=lambda *a, **k: False,
        extract_signature_block_fn=lambda _r: None,
        lock_for_fn=lambda _t: __import__("threading").Lock(),
    )
    # a sibling fills while our unit is working
    assert splice_fn_body(f, "b", "let sibling = 1;", validate_parse=_ACCEPT).ok
    # our unit loses and reverts its own fn
    assert mut.revert_body_scoped(f, "a", "", "", "unimplemented!()").ok

    text = f.read_text(encoding="utf-8")
    assert "let sibling = 1;" in text     # sibling's work survives
    assert text.count("unimplemented!()") == 1   # only `a` is back to a stub
