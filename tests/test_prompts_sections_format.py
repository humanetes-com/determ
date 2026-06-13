"""Unit tests for the pure prompt formatters in pipeline_core.prompts.sections:
format_type_for_prompt (TypeRecord → Rust-like def) and
format_impl_block_for_prompt (ImplBlock → target-form member listing).
"""
from __future__ import annotations

from pipeline_core.domain.models import (
    FieldRecord,
    FnRecord,
    ImplBlock,
    MethodRecord,
    TypeRecord,
)
from pipeline_core.prompts.sections import (
    format_impl_block_for_prompt,
    format_type_for_prompt,
)


def test_format_type_struct_with_fields():
    rec = TypeRecord(kind="struct", name="Cfg", generics="<T>",
                     fields=(FieldRecord(name="n", ty="usize", is_pub=True),
                             FieldRecord(name="hidden", ty="T")))
    out = format_type_for_prompt(rec)
    assert "struct Cfg<T>" in out
    assert "pub n: usize" in out
    assert "hidden: T" in out
    assert "fields (access with .name)" in out


def test_format_type_enum_truncates_variants():
    rec = TypeRecord(kind="enum", name="E",
                     variants=tuple(f"V{i}" for i in range(12)))
    out = format_type_for_prompt(rec)
    assert "variants:" in out
    assert "V0" in out and "V7" in out
    assert "... (4 more)" in out  # only first 8 shown


def test_format_type_methods_dedup_truncate_and_overflow():
    methods = [MethodRecord(name="new", sig="fn new() -> Self", is_pub=True),
               MethodRecord(name="new", sig="fn new(dup)")]  # dup name dropped
    methods += [MethodRecord(name=f"m{i}", sig=f"fn m{i}()") for i in range(12)]
    methods.append(MethodRecord(name="big", sig="fn big(" + "x" * 200 + ")"))
    rec = TypeRecord(kind="struct", name="S", methods=tuple(methods))
    out = format_type_for_prompt(rec)
    assert "methods (sig shown" in out
    assert "pub fn new() -> Self" in out
    assert out.count("fn new(") == 1  # dedup kept first only
    assert "// also:" in out  # >10 methods → overflow line


def test_format_type_method_without_sig():
    rec = TypeRecord(kind="struct", name="S",
                     methods=(MethodRecord(name="go", sig=""),))
    out = format_type_for_prompt(rec)
    assert "fn go(...)  // sig unknown" in out


def test_format_type_empty_record():
    out = format_type_for_prompt(TypeRecord())
    assert out == "? ?"  # kind/name default to "?"


def _fn(name, body="", stub=False):
    return FnRecord(fn_name=name, sig_text=f"fn {name}(&self)", body_text=body,
                    has_unimplemented_body=stub)


def test_format_impl_block_stub_and_filled():
    block = ImplBlock(container="impl Foo {",
                      members=(_fn("a", stub=True), _fn("b", body="return 1;")))
    out = format_impl_block_for_prompt(block, mark_target=True)
    assert "impl Foo {" in out
    assert "[STUB] — fill in this response" in out
    assert "/* FILLED */" in out and "return 1;" in out
    assert out.rstrip().endswith("}")


def test_format_impl_block_stub_unmarked():
    block = ImplBlock(container="impl Foo", members=(_fn("a", stub=True),))
    out = format_impl_block_for_prompt(block, mark_target=False)
    assert "// [STUB]" in out
    assert "fill in this response" not in out


def test_format_impl_block_long_body_preview_truncated():
    long_body = "\n".join(f"line{i};" for i in range(60))
    block = ImplBlock(container="impl Foo {", members=(_fn("a", body=long_body),))
    out = format_impl_block_for_prompt(block)
    assert "/* ... */" in out  # body preview capped at 200 chars


def test_format_impl_block_free_fns_no_container():
    block = ImplBlock(container="", members=(_fn("free", stub=True),))
    out = format_impl_block_for_prompt(block)
    assert "// free fns at file level" in out
    assert "[STUB]" in out
