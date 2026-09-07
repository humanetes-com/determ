"""External (standard-library) type records for the fill prompts.

The prompts render TYPES_IN_SCOPE from the crate's own records, so a type
the crate binds with `use std::…` has no record behind its name: run
`3003-93e9a9bf` (2026-09-02) filled the Debug formatter from a prompt that
named `Backtrace` in fields and signatures and said nothing about
`status()` or `BacktraceStatus`, and the body printed the backtrace
unconditionally. These tests pin the pure rules (`domain/external_types`),
the adapter over `rust-src` (`adapters/std_types`), the structure-reader
wiring, and the negative control — the pre-existing hand table cannot
answer `Backtrace`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.std_types import (
    ENV_SWITCH,
    StdTypeSource,
    cache_dir_for,
    crate_root_of,
    external_type_records,
)
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_filter_type_members,
)
from pipeline_core.domain import external_types as et
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.models import (
    BuildCluster,
    FieldRecord,
    MethodRecord,
    TypeRecord,
    UseRecord,
)
from pipeline_core.domain.std_api_sigs import std_api_def
from pipeline_core.domain.type_refs import types_referenced_in_text
from pipeline_core.prompts.sections import format_type_for_prompt

BIN = Path(__file__).resolve().parent.parent / "tools/skeleton_extractor/target/release/skeleton-extractor"


def _rust_src() -> Path | None:
    try:
        r = subprocess.run(["rustc", "--print", "sysroot"], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    lib = Path(r.stdout.strip()) / et.SYSROOT_LIBRARY_REL
    return lib if (lib / "std/src/backtrace.rs").is_file() else None


RUST_SRC = _rust_src()
needs_toolchain = pytest.mark.skipif(
    RUST_SRC is None or not BIN.exists(),
    reason="needs the rust-src component and the release extractor binary")

# The variant string exactly as the extractor emits it for
# `std::backtrace::BacktraceStatus` on rustc 1.95.0 (2026-09-02).
NOISY_UNSUPPORTED = (
    '# [doc = " Capturing a backtrace is not supported, likely because it\'s not"] '
    '# [doc = " implemented for the current platform."] '
    '# [stable (feature = "backtrace" , since = "1.65.0")] Unsupported')

# Records shaped like the extractor's output for a copy of `std/src/backtrace.rs`.
RAW_BACKTRACE = TypeRecord(
    file="src/backtrace.rs", kind="struct", name="Backtrace",
    definition=(
        "/// A captured OS thread stack backtrace.\n///\n"
        "/// For more information see `Backtrace::capture`.\n"
        '#[stable(feature = "backtrace", since = "1.65.0")]\n'
        "pub struct Backtrace {\n    inner: Inner,\n}"),
    fields=(FieldRecord(name="inner", ty="Inner", is_pub=False),),
    methods=(
        MethodRecord(name="enabled", sig="fn enabled () -> bool", is_pub=False, is_exactly_pub=False),
        MethodRecord(name="capture", sig="fn capture () -> Backtrace", is_pub=True),
        MethodRecord(name="status", sig="fn status (& self) -> BacktraceStatus", is_pub=True),
        MethodRecord(name="frames", sig="fn frames (& 'a self) -> & 'a [BacktraceFrame]", is_pub=True),
    ),
    is_pub=True, is_exactly_pub=True)
RAW_STATUS = TypeRecord(
    file="src/backtrace.rs", kind="enum", name="BacktraceStatus",
    definition=(
        "/// The current status of a backtrace.\n"
        '#[stable(feature = "backtrace", since = "1.65.0")]\n#[non_exhaustive]\n'
        "#[derive(Debug, PartialEq, Eq)]\npub enum BacktraceStatus {\n"
        "    Unsupported,\n    Disabled,\n    Captured,\n}"),
    variants=(
        NOISY_UNSUPPORTED,
        '# [doc = " Capturing has been disabled."] # [stable (feature = "backtrace" , since = "1.65.0")] Disabled',
        '# [stable (feature = "backtrace" , since = "1.65.0")] Captured',
    ),
    is_pub=True, is_exactly_pub=True)
RAW_FRAME = TypeRecord(
    file="src/backtrace.rs", kind="struct", name="BacktraceFrame",
    definition='#[unstable(feature = "backtrace_frames", issue = "79676")]\npub struct BacktraceFrame {\n    frame: RawFrame,\n}',
    fields=(FieldRecord(name="frame", ty="RawFrame", is_pub=False),), is_pub=True, is_exactly_pub=True)
RAW_RAWFRAME = TypeRecord(
    file="src/backtrace.rs", kind="enum", name="RawFrame", definition="enum RawFrame {\n    Actual(Frame),\n    #[cfg(test)]\n    Fake,\n}",
    variants=("Actual (backtrace_rs :: Frame)", "# [cfg (test)] Fake"), is_pub=False, is_exactly_pub=False)

SOURCE = """\
#[stable(feature = "backtrace", since = "1.65.0")]
pub struct Backtrace {
    inner: Inner,
}

#[unstable(
    feature = "backtrace_frames",
    issue = "79676"
)]
pub struct BacktraceFrame {
    frame: RawFrame,
}

impl Backtrace {
    #[stable(feature = "backtrace", since = "1.65.0")]
    pub fn status(&self) -> BacktraceStatus { unimplemented!() }

    #[unstable(feature = "backtrace_frames", issue = "79676")]
    pub fn frames(&'a self) -> &'a [BacktraceFrame] { unimplemented!() }
}

impl fmt::Display for Backtrace {
    fn fmt(&self, fmt: &mut fmt::Formatter<'_>) -> fmt::Result { Ok(()) }
}

impl fmt::Debug for Backtrace {
    fn fmt(&self, fmt: &mut fmt::Formatter<'_>) -> fmt::Result { Ok(()) }
}

impl<'a> Iterator for Frames<'a> {
    type Item = &'a BacktraceFrame;
}

unsafe impl<T: ?Sized + Sync + Send> Send for Arc<T> {}

impl<T> !Send for Lonely<T> {}

impl Error for crate::fmt::Error {}
"""


# ── pure: bindings and paths ──────────────────────────────────────────────────

def _use(file: str, alias: str, *path: str, glob: bool = False) -> UseRecord:
    return UseRecord(file=file, alias=alias, path=tuple(path), is_glob=glob)


def test_resolve_external_paths_maps_aliases_modules_and_binding_files() -> None:
    uses = [
        _use("src/nightly.rs", "Backtrace", "std", "backtrace", "Backtrace"),
        _use("src/backtrace.rs", "Backtrace", "std", "backtrace", "Backtrace"),
        _use("src/lib.rs", "StdError", "std", "error", "Error"),
        _use("src/fmt.rs", "self", "core", "fmt", "self"),
        _use("src/error.rs", "ptr", "core", "ptr"),
        _use("src/error.rs", "Own", "crate", "ptr", "Own"),
        _use("src/error.rs", "*", "core", "fmt", glob=True),
    ]
    b = et.resolve_external_paths(uses)
    assert b.aliases == {"Backtrace": "std::backtrace::Backtrace", "StdError": "std::error::Error"}
    assert b.modules == {"fmt": "core::fmt", "ptr": "core::ptr"}
    # Every binding file, in FILE order — not the order the extractor walked
    # the tree in, which is filesystem order and put `nightly.rs` first.
    assert b.binding_files["Backtrace"] == ("src/backtrace.rs", "src/nightly.rs")
    assert b.binding_files["fmt"] == ("src/fmt.rs",)


def test_qualify_resolves_alias_module_and_rooted_paths() -> None:
    aliases = {"Backtrace": "std::backtrace::Backtrace", "StdError": "std::error::Error"}
    modules = {"fmt": "core::fmt"}
    assert et.qualify("Backtrace", aliases, modules) == "std::backtrace::Backtrace"
    assert et.qualify("StdError", aliases, modules) == "std::error::Error"
    assert et.qualify("fmt::Formatter", aliases, modules) == "core::fmt::Formatter"
    assert et.qualify("std::backtrace::Backtrace", aliases, modules) == "std::backtrace::Backtrace"
    assert et.qualify("crate::fmt::Formatter", aliases, modules) is None
    assert et.qualify("Unknown", aliases, modules) is None


def test_sysroot_file_candidates_walks_module_segments_deepest_first() -> None:
    assert et.sysroot_file_candidates("std::backtrace::Backtrace") == (
        "std/src/backtrace.rs", "std/src/backtrace/mod.rs")
    assert et.sysroot_file_candidates("core::fmt::Formatter") == (
        "core/src/fmt.rs", "core/src/fmt/mod.rs")
    assert et.sysroot_file_candidates("std::collections::hash_map::HashMap") == (
        "std/src/collections/hash_map.rs", "std/src/collections/hash_map/mod.rs",
        "std/src/collections.rs", "std/src/collections/mod.rs")
    assert et.sysroot_file_candidates("std::Nothing") == ()
    for rel, module in (("std/src/backtrace.rs", "std::backtrace"),
                        ("std/src/collections/mod.rs", "std::collections"),
                        ("core/src/lib.rs", "core")):
        assert et.module_of_library_file(rel) == module


def test_resolve_use_path_inside_a_library_file() -> None:
    assert et.resolve_use_path(("alloc_crate", "sync", "Arc"), crate="std", module="std::sync") == "alloc::sync::Arc"
    assert et.resolve_use_path(("non_null", "NonNull"), crate="core", module="core::ptr") == "core::ptr::non_null::NonNull"
    assert et.resolve_use_path(("self", "hash_map", "HashMap"), crate="std", module="std::collections") == "std::collections::hash_map::HashMap"
    assert et.resolve_use_path(("super", "x", "Y"), crate="core", module="core::a::b") == "core::a::x::Y"
    assert et.resolve_use_path(("crate", "error", "Error"), crate="core", module="core::x") == "core::error::Error"
    assert et.resolve_use_path(("core", "fmt", "self"), crate="std", module="std::x") is None


# ── pure: text cleaning ───────────────────────────────────────────────────────

def test_strip_attributes_and_clean_variant_on_the_recorded_noise() -> None:
    assert et.clean_variant(NOISY_UNSUPPORTED) == "Unsupported"
    attrs, rest = et.split_attributes(NOISY_UNSUPPORTED)
    assert len(attrs) == 3 and attrs[2].startswith("stable") and rest.strip() == "Unsupported"
    # A `]` inside a doc string does not end the group.
    assert et.strip_attributes('#[doc = "see [link]"] pub struct X') == "pub struct X"
    assert et.clean_variant("Captured (LazyLock < Capture , LazyResolve >)") == "Captured(LazyLock<Capture, LazyResolve>)"
    assert et.variant_is_test_only("# [cfg (test)] Fake") and not et.variant_is_test_only(NOISY_UNSUPPORTED)


def test_compact_rust_spacing_on_extractor_shaped_signatures() -> None:
    cases = {
        "fn status (& self) -> BacktraceStatus": "fn status(&self) -> BacktraceStatus",
        "fn frames (& 'a self) -> & 'a [BacktraceFrame]": "fn frames(&'a self) -> &'a [BacktraceFrame]",
        "fn provide_ref < T : ? Sized + 'static > (& mut self , value : & 'a T) -> & mut Self":
            "fn provide_ref<T: ?Sized + 'static>(&mut self, value: &'a T) -> &mut Self",
        "fn source (& self) -> Option < & (dyn Error + 'static) >": "fn source(&self) -> Option<&(dyn Error + 'static)>",
        "fn ip (& self) -> * mut c_void": "fn ip(&self) -> *mut c_void",
        "impl (FnOnce() -> Capture) + Send": "impl (FnOnce() -> Capture) + Send",
        "for < 'a > Fn (& 'a str) -> bool": "for<'a> Fn(&'a str) -> bool",
    }
    for spaced, compact in cases.items():
        assert et.compact_rust_spacing(spaced) == compact
        assert et.compact_rust_spacing(compact) == compact  # already compact: unchanged
    assert et.clean_generics(
        '< T : ? Sized , # [unstable (feature = "allocator_api" , issue = "32838")] A : Allocator = Global , >'
    ) == "<T: ?Sized, A: Allocator = Global>"


def test_trait_impls_for_reads_impl_headers() -> None:
    assert et.trait_impls_for(SOURCE, "Backtrace") == ("Display", "Debug")
    assert et.trait_impls_for(SOURCE, "Frames") == ("Iterator",)
    assert et.trait_impls_for(SOURCE, "Arc") == ("Send",)
    assert et.trait_impls_for(SOURCE, "Lonely") == ("!Send",)
    # `impl Error for crate::fmt::Error` is an impl for ANOTHER module's
    # `Error`, not for a trait `Error` declared in this file.
    assert et.trait_impls_for(SOURCE, "Error") == ()
    assert et.parse_impl_header("impl<T: ?Sized, A: Allocator> Deref for Box<T, A> {") == ("Deref", "Box")
    assert et.parse_impl_header("impl Backtrace {") is None


def test_unstable_items_reads_wrapped_attributes_and_feature_gates() -> None:
    items = et.unstable_items(SOURCE)
    assert items == {"BacktraceFrame": "backtrace_frames", "frames": "backtrace_frames"}
    assert et.unstable_item_names(SOURCE) == frozenset({"BacktraceFrame", "frames"})
    assert "status" not in items


def test_external_names_in_resolves_aliases_and_qualified_paths_not_crate_names() -> None:
    aliases = {"Backtrace": "std::backtrace::Backtrace", "Own": "std::x::Own"}
    modules = {"fmt": "core::fmt"}
    text = ("fn f(b: &Backtrace, w: &mut fmt::Formatter<'_>, o: Own) -> fmt::Result {"
            " crate::fmt::Hidden; std::backtrace::Backtrace::capture(); core::result::Result::Ok(1) }")
    # `Own` and `Result` are names the crate declares: neither the bare alias
    # nor a qualified path ending in them yields a record, because the record
    # would carry the crate's own name. `Result::Ok` is read back to its type.
    assert et.external_names_in(text, aliases, modules, {"Own", "Result"}) == [
        "Backtrace", "fmt::Formatter", "std::backtrace::Backtrace"]
    assert et.external_names_in(text, aliases, modules, {"Own"})[-1] == "core::result::Result"
    # Doc comments are not code:
    assert et.external_names_in(et.code_only("/// use std::fs::File;\nfn g() {}"), {}, {}, set()) == []


def test_cfg_complement_is_the_arm_the_shim_does_not_compile() -> None:
    shim = '#[cfg(not(feature = "std"))]\npub(crate) enum Backtrace {}'
    preds = et.leading_cfg_predicates(shim)
    assert preds == ('not(feature = "std")',)
    comp = et.cfg_complement(preds)
    assert comp == 'not(not(feature = "std"))'
    std_build = CfgSet.of({'feature = "std"'}, set())
    assert std_build.evaluate(preds[0]) is False and std_build.evaluate(comp) is True
    assert et.cfg_complement(()) is None
    assert et.cfg_complement(("a", "b")) == "not(all(a, b))"


# ── pure: record building and rendering ───────────────────────────────────────

def _canonical() -> tuple[TypeRecord, TypeRecord]:
    unstable = et.unstable_items(SOURCE)
    bt = et.canonical_external_record(
        RAW_BACKTRACE, external_path="std::backtrace::Backtrace", source_text=SOURCE, unstable=unstable)
    st = et.canonical_external_record(
        RAW_STATUS, external_path="std::backtrace::BacktraceStatus", source_text=SOURCE, unstable=unstable)
    return bt, st


def test_canonical_record_drops_private_members_and_marks_unstable() -> None:
    bt, st = _canonical()
    assert bt.kind == "external struct" and bt.external_path == "std::backtrace::Backtrace"
    assert bt.file == "" and bt.mod_path == "" and bt.is_pub and bt.is_exactly_pub
    assert bt.fields == ()  # `inner: Inner` is private: invisible from outside std
    assert [m.name for m in bt.methods] == ["capture", "status", "frames"]  # `enabled` is private
    assert bt.methods[1].sig == "fn status(&self) -> BacktraceStatus"
    assert all(m.file == "" and m.mod_path == "" for m in bt.methods)
    # Unstable stays, gated: its feature is a cfg predicate the cluster
    # filter can decide, and a doc line names it for the ones that cannot.
    assert bt.methods[2].cfg_predicates == ("backtrace_frames",)
    assert "/// nightly-only methods: frames() need #![feature(backtrace_frames)]" in bt.definition
    assert bt.external_trait_impls == ("Display", "Debug")
    assert bt.definition.splitlines()[0] == (
        "/// external type std::backtrace::Backtrace (standard library); trait impls: Display, Debug")
    assert "pub struct Backtrace { /* private fields */ }" in bt.definition
    assert bt.definition.splitlines()[-1] == "// trait impls: Display, Debug"
    assert st.variants == ("Unsupported", "Disabled", "Captured")
    assert "#[derive(Debug, PartialEq, Eq)]" in st.definition and "#[non_exhaustive]" in st.definition
    assert "#[stable" not in st.definition
    raw_frame = et.canonical_external_record(RAW_RAWFRAME, external_path="std::backtrace::RawFrame", source_text=SOURCE)
    assert raw_frame.variants == ("Actual(backtrace_rs::Frame)",)  # the `cfg(test)` variant is gone


def test_hop_names_follow_public_method_signatures() -> None:
    siblings = {"BacktraceStatus": RAW_STATUS, "BacktraceFrame": RAW_FRAME, "RawFrame": RAW_RAWFRAME}
    assert et.hop_names_for(RAW_BACKTRACE, siblings) == ["BacktraceStatus", "BacktraceFrame"]


def test_annotated_record_renders_through_the_prompt_renderer_as_it_stands() -> None:
    bt, st = _canonical()
    rec = et.annotate_external_record(
        bt, name="Backtrace", binding_file="src/backtrace.rs",
        binding_note="re-exported by this crate's use lines", hops=[st],
        cfg_complement='not(not(feature = "std"))')
    assert rec.name == "Backtrace" and rec.file == "src/backtrace.rs"
    lines = rec.definition.splitlines()
    assert lines[0] == ("/// external type std::backtrace::Backtrace (standard library; re-exported by "
                        "this crate's use lines); trait impls: Display, Debug")
    assert lines[1] == ("/// status() -> BacktraceStatus { Unsupported | Disabled | Captured } "
                        "(#[non_exhaustive]: match with a `_` arm)")
    assert '#[cfg(not(not(feature = "std")))]' in lines
    out = format_type_for_prompt(rec)
    for needle in ("status(&self) -> BacktraceStatus", "Captured", "Display", "external struct Backtrace"):
        assert needle in out, out
    assert "construct:" not in out  # no struct-literal hint for a type with private fields
    # The alias case: the crate's name for the type is what the prompt shows.
    err = et.annotate_external_record(
        TypeRecord(kind="external trait", name="Error", external_path="std::error::Error",
                   definition="/// external type std::error::Error (standard library)\npub trait Error: Debug + Display"),
        name="StdError", binding_file="src/lib.rs", binding_note="re-exported by this crate's use lines")
    assert "external trait StdError" in format_type_for_prompt(err)
    assert "std::error::Error" in format_type_for_prompt(err)


def test_std_api_sigs_cannot_answer_backtrace() -> None:
    # NEGATIVE CONTROL — the pre-fix path. The hand-written card table
    # (`std_api_def`) is the only std fallback the type_def pull had, and it
    # answers one nightly family; `Backtrace` came back empty, so the prompt
    # of run `3003-93e9a9bf` had nothing to say about `status()`.
    assert std_api_def("Backtrace") == ""
    assert std_api_def("BacktraceStatus") == ""
    assert std_api_def("Request") != ""  # the one family it does answer stays the fallback


def test_type_record_wire_shape_carries_the_fields_only_when_external() -> None:
    crate = TypeRecord(name="Foo", kind="struct")
    assert "external_path" not in crate.to_dict()
    assert TypeRecord.from_dict(crate.to_dict()) == crate
    ext = TypeRecord(name="Foo", kind="external struct", external_path="std::x::Foo",
                     external_trait_impls=("Debug",))
    d = ext.to_dict()
    assert d["external_path"] == "std::x::Foo" and d["external_trait_impls"] == ["Debug"]
    assert TypeRecord.from_dict(d) == ext


# ── pure: the edges each rule guards ──────────────────────────────────────────

def test_path_rules_refuse_what_they_cannot_place() -> None:
    assert et.qualify("", {}, {}) is None and et.qualify("::", {}, {}) is None
    assert et.sysroot_file_candidates("std") == ()
    assert et.module_of_library_file("std/backtrace.rs") == ""
    # Two `super`s out of a one-deep module leave the crate: not a path.
    assert et.resolve_use_path(("super", "super", "X"), crate="core", module="core::a") is None
    assert et.is_external_record(TypeRecord(name="X", external_path="std::x::X"))
    assert not et.is_external_record(TypeRecord(name="X"))
    assert et.external_kind("external enum") == "external enum"


def test_attribute_scanner_handles_inner_attributes_and_unbalanced_brackets() -> None:
    attrs, rest = et.split_attributes("#![allow(dead_code)] # ! [doc = \"x\"] pub struct X")
    assert attrs == ("allow(dead_code)", 'doc = "x"') and rest.strip() == "pub struct X"
    # An unbalanced group is not an attribute: the text passes through untouched.
    assert et.strip_attributes("#[unbalanced pub struct X") == "#[unbalanced pub struct X"


def test_doc_and_attribute_blocks_skip_inner_docs_and_join_wrapped_attributes() -> None:
    definition = "//! module doc\n\n// note\n/// real doc\n#[derive(\n    Clone,\n    Debug\n)]\npub struct X {\n    a: u8,\n}"
    assert et.leading_doc_texts(definition) == ["real doc"]
    assert et.leading_attribute_text(definition) == "#[derive( Clone, Debug )]"
    rec = et.canonical_external_record(
        TypeRecord(kind="struct", name="X", definition=definition, fields=(FieldRecord(name="a", ty="u8"),),
                   is_pub=True, is_exactly_pub=True),
        external_path="core::x::X", source_text="")
    assert "#[derive( Clone, Debug )]" in rec.definition and "pub struct X { /* private fields */ }" in rec.definition
    # A head that never reaches `{` or `;` is cut after twelve lines.
    long_head = "pub trait Long:\n" + "\n".join(f"    Bound{i} +" for i in range(14))
    trait = et.canonical_external_record(
        TypeRecord(kind="trait", name="Long", definition=long_head, is_pub=True, is_exactly_pub=True),
        external_path="core::x::Long", source_text="")
    assert "Bound10 +" in trait.definition and "Bound12" not in trait.definition


def test_impl_header_parser_edges() -> None:
    # Header wrapped after the generics: `for` arrives on the next line.
    wrapped = "impl<T: ?Sized>\n    Deref for Wrapped<T>\n{\n}\n"
    assert et.trait_impls_for(wrapped, "Wrapped") == ("Deref",)
    assert et.parse_impl_header("fn not_an_impl() {") is None
    assert et.parse_impl_header("impl<T Deref for Broken {") is None  # unbalanced generics
    assert et.parse_impl_header("impl<T> const Deref for Constant<T> {") == ("Deref", "Constant")
    assert et.parse_impl_header("impl () for Tuple {") is None  # no trait name
    assert et.parse_impl_header("impl Trait for &mut Borrowed {") == ("Trait", "Borrowed")
    assert et.parse_impl_header("impl Trait for *const Raw {") == ("Trait", "Raw")
    assert et.parse_impl_header("impl Trait for &'a Lived {") == ("Trait", "Lived")
    assert et.parse_impl_header("impl Trait for dyn Object {") == ("Trait", "Object")
    assert et.parse_impl_header("impl Trait for (A, B) {") is None


def test_unstable_attribute_without_a_feature_string_marks_the_item_gateless() -> None:
    src = '#[unstable(issue = "1")]\npub fn a() {}\n#[unstable(feature = x)]\npub fn b() {}\n'
    assert et.unstable_items(src) == {"a": "", "b": ""}
    rec = et.canonical_external_record(
        TypeRecord(kind="struct", name="G", definition="pub struct G;",
                   methods=(MethodRecord(name="a", sig="fn a ()", is_pub=True),
                            MethodRecord(name="a", sig="fn a ()", is_pub=True),  # second impl block: once
                            MethodRecord(name="", sig="", is_pub=True)),
                   is_pub=True, is_exactly_pub=True),
        external_path="core::g::G", source_text=src, unstable=et.unstable_items(src))
    assert [m.name for m in rec.methods] == ["a"] and rec.methods[0].cfg_predicates == ()
    assert "/// nightly-only methods: a() need a nightly feature gate" in rec.definition
    assert et.external_doc_line("core::g::G", "standard library", (), unstable_gate="") == (
        "/// external type core::g::G (standard library) [unstable: nightly-only]")


def test_qualified_path_scanner_edges() -> None:
    assert et._qualified_paths("::Leading") == []           # nothing before the separator
    assert et._qualified_paths("a::b::") == []              # nothing after the last one
    assert et._qualified_paths("1::Digit") == []            # a segment cannot start with a digit
    assert et._qualified_paths("A::b::C") == []             # the type is the first segment: no module
    assert et._qualified_paths("fmt::X fmt::X") == ["fmt::X"]  # reported once
    assert et._qualified_paths("x::y::Z::w") == ["x::y::Z"]


def test_public_fields_render_in_the_head_and_hop_lines_follow_return_types() -> None:
    rec = et.canonical_external_record(
        TypeRecord(kind="struct", name="Range", definition="pub struct Range<Idx> {\n    pub start: Idx,\n    pub end: Idx,\n}",
                   generics="< Idx >",
                   fields=(FieldRecord(name="start", ty="Idx", is_pub=True), FieldRecord(name="end", ty="Idx", is_pub=True)),
                   is_pub=True, is_exactly_pub=True),
        external_path="core::ops::Range", source_text="")
    assert "pub struct Range<Idx> { pub start: Idx, pub end: Idx }" in rec.definition
    assert rec.generics == "<Idx>" and [f.name for f in rec.fields] == ["start", "end"]
    _bt, st = _canonical()
    # A hop no method returns gets no summary line; a struct hop never does.
    assert et.hop_doc_line(rec, st) is None
    assert et.hop_doc_line(RAW_BACKTRACE, RAW_FRAME) is None
    wide = TypeRecord(kind="external enum", name="BacktraceStatus", definition="pub enum BacktraceStatus {}",
                      variants=tuple(f"VariantNumber{i}" for i in range(12)))
    line = et.hop_doc_line(RAW_BACKTRACE, wide)
    assert line is not None and line.endswith("… }") and len(line) < 200


# ── adapter wiring, with a stub extractor and a canned source ─────────────────

CRATE_REC = {"file": "src/lib.rs", "kind": "struct", "name": "Error", "is_pub": True, "is_exactly_pub": True,
             "definition": "pub struct Error {\n    inner: Own<ErrorImpl>,\n}"}
SHIM_REC = {"file": "src/backtrace.rs", "kind": "enum", "name": "Backtrace", "is_pub": True, "is_exactly_pub": False,
            "definition": '#[cfg(not(feature = "std"))]\npub(crate) enum Backtrace {}'}
USE_REC = {"file": "src/backtrace.rs", "alias": "Backtrace", "path": ["std", "backtrace", "Backtrace"], "is_glob": False}


class _StubExtractor(CliExtractor):
    """The real adapter with its one subprocess seam (`_enumerate`) canned:
    the JSON→record parsing and the reader wiring above it run for real."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(Path("/nonexistent/skeleton-extractor"))
        self.rows = rows
        self.calls: list[str] = []

    def _enumerate(self, subcmd: str, src_dir: Path, cfg_true: tuple[str, ...],
                   cfg_false: tuple[str, ...], *, timeout: int = 60) -> list[Any] | None:
        self.calls.append(subcmd)
        return self.rows.get(subcmd)


class _CannedSource:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def records_for(self, path: str, *, cache_dir: Path | None = None) -> list[TypeRecord]:
        self.asked.append(path)
        if path != "std::backtrace::Backtrace":
            return []
        bt, st = _canonical()
        return [bt, st]


def _cluster(name: str, *, default_features: tuple[str, ...] = (),
             excluded: tuple[str, ...] = ()) -> BuildCluster:
    return BuildCluster(name=name, env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=list(excluded), active_cfg=[], prompt_nightly_rule="",
                        default_features=list(default_features))


def _crate(tmp_path: Path, backtrace_rs: str) -> Path:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("mod backtrace;\npub struct Error { inner: Own<ErrorImpl> }\n")
    (tmp_path / "src" / "backtrace.rs").write_text(backtrace_rs)
    return tmp_path


def test_reader_appends_external_records_last_and_the_env_switch_disables(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _crate(tmp_path, "pub(crate) use std::backtrace::Backtrace;\n")
    stub = _StubExtractor({"--enumerate-types": [CRATE_REC], "--enumerate-uses": [USE_REC]})
    source = _CannedSource()
    reader = CliStructureReader(stub, std_types=source)
    monkeypatch.delenv(ENV_SWITCH, raising=False)
    recs = reader.enumerate_types(root)
    assert recs is not None
    assert recs[0] == TypeRecord.from_dict(CRATE_REC)
    assert [r.name for r in recs] == ["Error", "Backtrace", "BacktraceStatus"]
    assert recs[-1].external_path == "std::backtrace::BacktraceStatus"
    bt = recs[1]
    assert bt.external_path == "std::backtrace::Backtrace" and bt.kind == "external struct"
    assert bt.file == "src/backtrace.rs" and bt.is_pub and bt.is_exactly_pub
    assert "re-exported by this crate's use lines" in bt.definition.splitlines()[0]
    assert "#[cfg(" not in bt.definition  # no same-file crate item: no cfg complement
    assert source.asked == ["std::backtrace::Backtrace"]
    assert stub.calls == ["--enumerate-types", "--enumerate-uses"]
    # Off switch: crate records only, and `use` bindings are not even read.
    stub.calls.clear()
    monkeypatch.setenv(ENV_SWITCH, "0")
    assert reader.enumerate_types(root) == [TypeRecord.from_dict(CRATE_REC)]
    assert stub.calls == ["--enumerate-types"]


def test_reader_gates_the_external_arm_by_the_shim_complement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-std shim pattern: `#[cfg(not(feature = "std"))] enum Backtrace {}`
    beside `#[cfg(feature = "std")] use std::backtrace::Backtrace;`. Both
    records are emitted; the external one carries the shim's complement, so
    the cluster's record-level cfg filter keeps exactly the compiled arm."""
    monkeypatch.delenv(ENV_SWITCH, raising=False)
    root = _crate(tmp_path, '#[cfg(feature = "std")]\npub(crate) use std::backtrace::Backtrace;\n'
                            '#[cfg(not(feature = "std"))]\npub(crate) enum Backtrace {}\n')
    stub = _StubExtractor({"--enumerate-types": [CRATE_REC, SHIM_REC], "--enumerate-uses": [USE_REC]})
    recs = CliStructureReader(stub, std_types=_CannedSource()).enumerate_types(root)
    assert recs is not None and [r.name for r in recs] == ["Error", "Backtrace", "Backtrace", "BacktraceStatus"]
    external = recs[2]
    assert external.external_path == "std::backtrace::Backtrace"
    assert '#[cfg(not(not(feature = "std")))]' in external.definition.splitlines()
    std_cluster = _cluster("stable", default_features=("std",))
    kept = cluster_filter_type_members(list(recs), std_cluster)
    assert [(r.kind, r.name) for r in kept if r.name == "Backtrace"] == [("external struct", "Backtrace")]
    no_std_cluster = _cluster("nostd", excluded=('feature = "std"',))
    kept = cluster_filter_type_members(list(recs), no_std_cluster)
    assert [(r.kind, r.name) for r in kept if r.name == "Backtrace"] == [("enum", "Backtrace")]
    # What the formatter fill's selection now sees under the std cluster: the
    # field hop from `Error { inner: … }` is unchanged, and `Backtrace` written
    # in a signature resolves to the external record.
    sel = types_referenced_in_text("fn backtrace(this: Ref<Self>) -> &Backtrace",
                                   cluster_filter_type_members(list(recs), std_cluster), visible_from="src/fmt.rs")
    assert [(r.kind, r.name) for r in sel] == [
        ("external struct", "Backtrace"),
        ("external enum", "BacktraceStatus"),
    ]


def test_reader_skips_a_name_the_crate_declares_elsewhere_and_non_packages(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SWITCH, raising=False)
    root = _crate(tmp_path, "pub(crate) use std::backtrace::Backtrace;\n")
    other = {**SHIM_REC, "file": "src/other.rs", "definition": "pub enum Backtrace {}"}
    stub = _StubExtractor({"--enumerate-types": [CRATE_REC, other], "--enumerate-uses": [USE_REC]})
    source = _CannedSource()
    recs = CliStructureReader(stub, std_types=source).enumerate_types(root)
    assert recs is not None and [r.name for r in recs] == ["Error", "Backtrace"]
    assert source.asked == []  # a genuinely different crate item owns the name

    class _NoUses:
        """An extractor without `enumerate_uses` at all: a tree that is not a
        cargo package (the public-API view's scratch dir) must never need it."""

        def enumerate_types(self, src_dir: Path, **_k: Any) -> list[TypeRecord]:
            return [TypeRecord.from_dict(CRATE_REC)]

    scratch = tmp_path / "scratch"
    (scratch / "src").mkdir(parents=True)
    (scratch / "src" / "lib.rs").write_text("use std::backtrace::Backtrace;\n")
    assert CliStructureReader(_NoUses(), std_types=source).enumerate_types(scratch) == [  # type: ignore[arg-type]
        TypeRecord.from_dict(CRATE_REC)]
    assert source.asked == []


def test_crate_root_and_cache_dir_follow_the_workspace_layout(tmp_path: Path) -> None:
    ws = tmp_path / "anyhow"
    cluster = ws / ".skeleton-filled" / "stable"
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text("[package]\n")
    assert crate_root_of(cluster) == cluster
    assert crate_root_of(cluster / "src") == cluster
    assert crate_root_of(tmp_path) is None
    assert cache_dir_for(cluster) == ws / ".phase_c_logs" / "std_types"
    (ws / "Cargo.toml").write_text("[package]\n")
    assert cache_dir_for(ws) == ws / ".phase_c_logs" / "std_types"


def test_external_type_records_reads_module_qualified_names_from_code_only(tmp_path: Path) -> None:
    root = _crate(tmp_path, "use core::fmt;\n/// use std::fs::File;\nfn f(w: &mut fmt::Formatter) {}\n")
    asked: list[str] = []

    class _Src:
        def records_for(self, path: str, *, cache_dir: Path | None = None) -> list[TypeRecord]:
            asked.append(path)
            return [] if "fs" in path else [TypeRecord(
                kind="external struct", name=path.rsplit("::", 1)[-1], external_path=path,
                definition=f"/// external type {path} (standard library)\npub struct X")]

    out = external_type_records(
        root, [TypeRecord.from_dict(CRATE_REC)],
        [UseRecord(file="src/backtrace.rs", alias="self", path=("core", "fmt", "self"))],
        _Src(), log=lambda _m: None)
    assert asked == ["core::fmt::Formatter"]  # the doc-comment `std::fs::File` is not code
    assert [r.name for r in out] == ["Formatter"] and out[0].file == "src/backtrace.rs"
    assert "this crate writes it as `fmt::Formatter`" in out[0].definition.splitlines()[0]


@needs_toolchain
def test_an_extractor_failure_is_not_cached_as_a_negative_answer(tmp_path: Path) -> None:
    """A path the library does not spell is a durable fact and is cached; an
    extractor that cannot run is a fact about this process and must not
    leave `[]` on disk for every later process to trust."""
    logs: list[str] = []
    broken = StdTypeSource(CliExtractor(tmp_path / "absent-binary"), log=logs.append)
    assert broken.records_for("std::backtrace::Backtrace", cache_dir=tmp_path / "cache") == []
    assert not list((tmp_path / "cache").glob("*.json")) if (tmp_path / "cache").exists() else True
    assert len(logs) == 1 and "could not enumerate" in logs[0]
    working = StdTypeSource(CliExtractor(BIN), log=logs.append)
    assert working.records_for("std::nowhere::Nothing", cache_dir=tmp_path / "cache") == []
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1  # the durable negative IS cached


# ── integration: the real extractor over the real rust-src ────────────────────

@needs_toolchain
def test_std_type_source_reads_backtrace_from_rust_src(tmp_path: Path) -> None:
    logs: list[str] = []
    source = StdTypeSource(CliExtractor(BIN), log=logs.append)
    recs = source.records_for("std::backtrace::Backtrace", cache_dir=tmp_path / "cache")
    names = [r.name for r in recs]
    assert names[0] == "Backtrace" and "BacktraceStatus" in names
    bt = recs[0]
    assert bt.kind == "external struct" and bt.external_path == "std::backtrace::Backtrace"
    assert any("status" in m.sig and "BacktraceStatus" in m.sig for m in bt.methods)
    assert "Display" in bt.external_trait_impls and "Debug" in bt.external_trait_impls
    status = recs[names.index("BacktraceStatus")]
    assert status.kind == "external enum"
    assert status.variants == ("Unsupported", "Disabled", "Captured")
    assert status.external_path == "std::backtrace::BacktraceStatus"
    assert logs == []
    # The disk cache answers the next process without the extractor.
    cache_files = list((tmp_path / "cache").glob("*.json"))
    assert len(cache_files) == 1
    data = json.loads(cache_files[0].read_text())
    assert data["path"] == "std::backtrace::Backtrace" and data["toolchain"].startswith("rustc ")

    class _Counting(CliExtractor):
        calls = 0

        def _enumerate(self, *a: Any, **k: Any) -> list[Any] | None:
            type(self).calls += 1
            return super()._enumerate(*a, **k)

    again = StdTypeSource(_Counting(BIN), log=logs.append).records_for(
        "std::backtrace::Backtrace", cache_dir=tmp_path / "cache")
    assert [r.name for r in again] == names and _Counting.calls == 0


@needs_toolchain
def test_std_type_source_follows_a_reexport_and_reports_an_unknown_path_once(tmp_path: Path) -> None:
    logs: list[str] = []
    source = StdTypeSource(CliExtractor(BIN), log=logs.append)
    # `std::error::Error` is `pub use core::error::Error;` in std/src/error.rs.
    recs = source.records_for("std::error::Error", cache_dir=tmp_path)
    assert recs and recs[0].kind == "external trait" and recs[0].external_path == "std::error::Error"
    assert "source" in [m.name for m in recs[0].methods]
    provide = next(m for m in recs[0].methods if m.name == "provide")
    assert provide.cfg_predicates == ("error_generic_member_access",)
    assert "Request" in [r.name for r in recs[1:]]
    assert source.records_for("std::nowhere::Nothing", cache_dir=tmp_path) == []
    assert source.records_for("std::nowhere::Nothing", cache_dir=tmp_path) == []
    assert [m for m in logs if "std::nowhere::Nothing" in m] and len(logs) == 1


@needs_toolchain
def test_reader_over_the_real_extractor_appends_the_backtrace_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SWITCH, raising=False)
    root = _crate(tmp_path, '#[cfg(feature = "std")]\npub(crate) use std::backtrace::Backtrace;\n'
                            '#[cfg(not(feature = "std"))]\npub(crate) enum Backtrace {}\n')
    logs: list[str] = []
    reader = CliStructureReader(CliExtractor(BIN), std_types=StdTypeSource(CliExtractor(BIN), log=logs.append))
    recs = reader.enumerate_types(root)
    assert recs is not None
    crate_recs = [r for r in recs if not r.external_path]
    assert recs[:len(crate_recs)] == crate_recs  # crate records keep their positions
    external = [r for r in recs if r.name == "Backtrace" and r.external_path]
    assert len(external) == 1 and external[0].file == "src/backtrace.rs"
    kept = cluster_filter_type_members(list(recs), _cluster("stable", default_features=("std",)))
    assert [(r.kind, r.name) for r in kept if r.name == "Backtrace"] == [("external struct", "Backtrace")]
    assert (root / ".phase_c_logs" / "std_types").is_dir()
