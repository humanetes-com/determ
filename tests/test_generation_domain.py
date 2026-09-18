"""Pure domain helpers in pipeline_core.domain.generation: filename parsing,
test-prefix stripping, TESTFILE marker scan, topo-sort, and the regex-free
gen_tests decl-strip (#9). All assert equivalence to the legacy regex behaviour."""
from __future__ import annotations

from pipeline_core.domain.generation import (
    coalesce_gen_tests_siblings,
    gen_tests_decl_blocks,
    iter_gen_tests_decls,
    merge_gen_tests_blocks,
    parse_gen_tests_filename,
    strip_all_gen_tests_decls,
    strip_gen_tests_decl,
    strip_gen_tests_decl_n,
    strip_test_prefix,
    topo_sort_files,
)
from pipeline_core.domain.generation import (
    testfile_paths as _testfile_paths,  # aliased: bare name would be collected as a test
)


def _decl(name: str, n: int, *, cfg: str = "", allow: str = "", pub: bool = False) -> str:
    """Mirror pipeline._build_gen_tests_decl's fixed output format."""
    lines: list[str] = []
    if cfg:
        lines.append(f"#[cfg({cfg})]")
    lines.append("#[cfg(test)]")
    if allow:
        lines.append(allow)
    lines.append(f'#[path = "{name}"]')
    mod = f"pub mod gen_tests_{n};" if pub else f"mod gen_tests_{n};"
    return "\n" + "\n".join(lines) + f"\n{mod}\n"


def test_parse_gen_tests_filename() -> None:
    assert parse_gen_tests_filename("error.gen_tests_3.rs") == ("error", 3)
    assert parse_gen_tests_filename("error.gen_tests_3.rs.broken") == ("error", 3)
    assert parse_gen_tests_filename("error.rs") is None
    assert parse_gen_tests_filename("error.gen_tests_x.rs") is None


def test_coalesce_merges_siblings_in_n_order() -> None:
    files = [
        ("src/error.gen_tests_2.rs", "B"),
        ("src/error.gen_tests_1.rs", "A"),
        ("src/error.gen_tests_10.rs", "C"),  # numeric, not lexicographic, order
    ]
    out = coalesce_gen_tests_siblings(files)
    assert len(out) == 1
    rel, body = out[0]
    assert rel == "src/error.rs (augmented tests)"
    assert body == "// gen_tests_1\nA\n\n// gen_tests_2\nB\n\n// gen_tests_10\nC"


def test_coalesce_passthrough_and_order_preserved() -> None:
    files = [
        ("tests/test_chain.rs", "chain"),
        ("src/error.gen_tests_1.rs", "A"),
        ("tests/test_fmt.rs", "fmt"),
        ("src/error.gen_tests_2.rs", "B"),
    ]
    out = coalesce_gen_tests_siblings(files)
    # Real test files pass through untouched; the merged unit lands at the
    # position of its first fragment.
    assert out == [
        ("tests/test_chain.rs", "chain"),
        ("src/error.rs (augmented tests)", "// gen_tests_1\nA\n\n// gen_tests_2\nB"),
        ("tests/test_fmt.rs", "fmt"),
    ]


def test_coalesce_groups_by_parent_dir_and_stem() -> None:
    files = [
        ("src/error.gen_tests_1.rs", "a"),
        ("src/sub/error.gen_tests_1.rs", "b"),   # same stem, different dir → distinct
        ("src/ptr.gen_tests_1.rs", "c"),
    ]
    out = coalesce_gen_tests_siblings(files)
    rels = [r for r, _ in out]
    assert rels == [
        "src/error.rs (augmented tests)",
        "src/sub/error.rs (augmented tests)",
        "src/ptr.rs (augmented tests)",
    ]


def test_coalesce_handles_broken_suffix_and_empty() -> None:
    assert coalesce_gen_tests_siblings([]) == []
    out = coalesce_gen_tests_siblings([("src/x.gen_tests_1.rs.broken", "q")])
    assert out == [("src/x.rs (augmented tests)", "// gen_tests_1\nq")]


def test_merge_blocks_flattens_disjoint_subsets() -> None:
    a = "mod gen_tests_1 {\n    use super::*;\n    #[test]\n    fn test_a() { assert!(true); }\n}"
    b = "mod gen_tests_2 {\n    use super::*;\n    #[test]\n    fn test_b() { assert!(true); }\n}"
    merged = merge_gen_tests_blocks([a, b])
    assert merged is not None
    # One mod shell, both test fns, the duplicate `use super::*;` collapsed to one.
    assert merged.count("use super::*;") == 1
    assert "fn test_a()" in merged and "fn test_b()" in merged
    assert merged.startswith("mod gen_tests_merged {")


def test_merge_blocks_returns_none_on_name_clash() -> None:
    a = "mod gen_tests_1 {\n    fn helper() {}\n    #[test]\n    fn test_a() {}\n}"
    b = "mod gen_tests_2 {\n    fn helper() {}\n    #[test]\n    fn test_b() {}\n}"
    assert merge_gen_tests_blocks([a, b]) is None      # `helper` clashes → keep separate


def test_merge_blocks_single_and_empty() -> None:
    assert merge_gen_tests_blocks([]) is None
    one = "mod gen_tests_1 {\n    #[test]\n    fn test_a() {}\n}"
    merged = merge_gen_tests_blocks([one])
    assert merged is not None and "fn test_a()" in merged


def test_strip_test_prefix() -> None:
    assert strip_test_prefix("test_foo") == "foo"
    assert strip_test_prefix("tests_foo") == "foo"   # not matched by test_
    assert strip_test_prefix("it_foo") == "foo"
    assert strip_test_prefix("foo") == "foo"


def test_testfile_paths() -> None:
    text = '<TESTFILE path="a.rs">x</TESTFILE><TESTFILE path="b/c.rs">'
    assert _testfile_paths(text) == ["a.rs", "b/c.rs"]
    assert _testfile_paths('<TESTFILE path="">') == []   # empty skipped (regex `+`)


def test_topo_sort_files() -> None:
    # The file that DECLARES `mod helper;` (lib.rs) is ordered before helper.rs.
    order = topo_sort_files(["src/lib.rs", "src/helper.rs"],
                            {"src/lib.rs": "mod helper;", "src/helper.rs": ""})
    assert set(order) == {"src/lib.rs", "src/helper.rs"}
    assert order.index("src/lib.rs") < order.index("src/helper.rs")


def test_strip_gen_tests_decl_basic() -> None:
    body = "pub fn f() {}\n"
    parent = body + _decl("error.gen_tests_3.rs", 3)
    out = strip_gen_tests_decl(parent, "error.gen_tests_3.rs", 3)
    assert "gen_tests_3" not in out and out.startswith("pub fn f() {}")


def test_strip_gen_tests_decl_with_cfg_and_allow_and_pub() -> None:
    parent = "x\n" + _decl("m.gen_tests_1.rs", 1, cfg="nightly_x",
                           allow="#[allow(clippy::all)]", pub=True)
    out = strip_gen_tests_decl(parent, "m.gen_tests_1.rs", 1)
    for frag in ("gen_tests_1", "nightly_x", "clippy::all", "#[path"):
        assert frag not in out
    assert out.rstrip() == "x"


def test_strip_gen_tests_decl_keeps_other_siblings() -> None:
    parent = ("y\n" + _decl("m.gen_tests_1.rs", 1)
              + _decl("m.gen_tests_2.rs", 2))
    out = strip_gen_tests_decl(parent, "m.gen_tests_2.rs", 2)
    assert "gen_tests_1" in out and "gen_tests_2" not in out


def test_strip_gen_tests_decl_wrong_name_or_absent_is_noop() -> None:
    parent = "z\n" + _decl("m.gen_tests_1.rs", 1)
    # right pass_n but wrong sibling name → no strip (path attr must match).
    assert strip_gen_tests_decl(parent, "other.gen_tests_1.rs", 1) == parent
    # no decl at all.
    assert strip_gen_tests_decl("just code\n", "m.gen_tests_1.rs", 1) == "just code\n"


def test_strip_gen_tests_decl_preserves_trailing_newline() -> None:
    # A file ending in the decl + newline keeps its trailing newline after strip.
    with_nl = "a\n" + _decl("m.gen_tests_1.rs", 1)
    out = strip_gen_tests_decl(with_nl, "m.gen_tests_1.rs", 1)
    assert out.endswith("\n") and "gen_tests_1" not in out


# --- decl toolkit (migration of the four legacy decl regexes) ---

def test_iter_gen_tests_decls_captures_gate_name_pass() -> None:
    text = ("pub fn f() {}\n"
            + _decl("m.gen_tests_1.rs", 1)
            + _decl("m.gen_tests_2.rs", 2, cfg="error_generic_member_access",
                    allow="#[allow(unused)]", pub=True))
    decls = list(iter_gen_tests_decls(text))
    assert [(d.pass_n, d.sibling_name, d.gate) for d in decls] == [
        (1, "m.gen_tests_1.rs", ""),
        (2, "m.gen_tests_2.rs", "error_generic_member_access"),
    ]


def test_strip_all_gen_tests_decls() -> None:
    text = ("use super::*;\n"
            + _decl("m.gen_tests_1.rs", 1)
            + _decl("m.gen_tests_2.rs", 2, cfg="nightly"))
    out = strip_all_gen_tests_decls(text)
    assert "gen_tests_1" not in out and "gen_tests_2" not in out
    assert "nightly" not in out and out.startswith("use super::*;")
    # A `mod` decl whose path is NOT a gen_tests sibling is left alone.
    keep = "#[path = \"other.rs\"]\nmod gen_tests_9;\n"
    assert strip_all_gen_tests_decls(keep) == keep  # path not a *.gen_tests_<N>.rs


def test_strip_gen_tests_decl_n_any_sibling() -> None:
    text = "x\n" + _decl("m.gen_tests_1.rs", 1) + _decl("m.gen_tests_2.rs", 2)
    out = strip_gen_tests_decl_n(text, 2)
    assert "gen_tests_2" not in out and "gen_tests_1" in out


def test_gen_tests_decl_blocks_only_real_siblings() -> None:
    text = (_decl("m.gen_tests_1.rs", 1)
            + "#[path = \"hand_written.rs\"]\nmod gen_tests_5;\n")  # not a sibling path
    blocks = gen_tests_decl_blocks(text)
    assert [b.pass_n for b in blocks] == [1]  # the non-sibling decl is excluded


# --- prettyplease re-emission: the decl the pipeline WROTE on one line per
# attribute comes back with a long `#[allow(...)]` wrapped over several. The
# gate sits ABOVE that attribute, so a line-at-a-time walk-back stops at its
# closing `)]` and reports the decl as ungated — the shape that let 20
# stable-only tests into the nightly oracle as un-runnable "vanished" tests.

_WRAPPED_ALLOW = "\n".join([
    "#[allow(",
    "    clippy::all,",
    "    dead_code,",
    "    non_snake_case,",
    "    unreachable_patterns,",
    "    unsafe_op_in_unsafe_fn,",
    "    unused,",
    "    unused_imports,",
    "    unused_mut",
    ")]",
])
_STABLE_GATE = 'not(any(error_generic_member_access, feature = "error_generic_member_access"))'


def test_iter_gen_tests_decls_reads_gate_past_a_wrapped_attribute() -> None:
    text = "pub fn f() {}\n" + _decl(
        "error.gen_tests_1.rs", 1, cfg=_STABLE_GATE, allow=_WRAPPED_ALLOW)
    (decl,) = list(iter_gen_tests_decls(text))
    assert decl.sibling_name == "error.gen_tests_1.rs"
    assert decl.gate == _STABLE_GATE


def test_strip_gen_tests_decl_removes_a_wrapped_attribute_run_whole() -> None:
    # `start` must cover the whole block: a strip that leaves the gate and the
    # wrapped allow behind orphans them onto whatever item follows.
    text = "pub fn f() {}\n" + _decl(
        "error.gen_tests_1.rs", 1, cfg=_STABLE_GATE, allow=_WRAPPED_ALLOW)
    out = strip_gen_tests_decl(text, "error.gen_tests_1.rs", 1)
    assert out.rstrip() == "pub fn f() {}"


def test_attr_run_walk_back_stops_at_the_item_above() -> None:
    # An unrelated item ending in `]` directly above the run must not be eaten,
    # and neither must its own attributes.
    text = ("#[derive(Debug)]\n"
            "pub struct S(pub [u8; 4]);\n"
            + _decl("m.gen_tests_1.rs", 1, cfg="nightly", allow=_WRAPPED_ALLOW))
    (decl,) = list(iter_gen_tests_decls(text))
    assert decl.gate == "nightly"
    out = strip_gen_tests_decl(text, "m.gen_tests_1.rs", 1)
    assert out.rstrip().endswith("pub struct S(pub [u8; 4]);")
    assert "#[derive(Debug)]" in out


# --- gen_tests artifact-naming pins (byte-equivalent to the old regexes) ---

import re  # noqa: E402

from pipeline_core.domain.generation import (  # noqa: E402
    gen_tests_sibling_number,
    has_gen_tests_mod_decl,
    is_gen_tests_sibling_name,
    strip_mod_test_prefix,
)


def test_is_gen_tests_sibling_name_matches_regex() -> None:
    pat = re.compile(r'\.gen_tests_\d+\.rs$')
    for name in ["foo.gen_tests_1.rs", "foo.gen_tests_12.rs", "foo.gen_tests_1.rs.broken",
                 "foo.rs", "foo.gen_tests_.rs", "foo.gen_tests_1a.rs", ".gen_tests_3.rs",
                 "a.gen_tests_2.gen_tests_5.rs", "foo_gen_tests_1.rs", ""]:
        assert is_gen_tests_sibling_name(name) == bool(pat.search(name)), name


def test_gen_tests_sibling_number_matches_regex() -> None:
    for stem in ["foo", "a.b", "mod"]:
        pat = re.compile(rf'^{re.escape(stem)}\.gen_tests_(\d+)\.rs$')
        for name in [f"{stem}.gen_tests_1.rs", f"{stem}.gen_tests_42.rs", f"{stem}.gen_tests_.rs",
                     f"{stem}.gen_tests_1.rs.broken", "other.gen_tests_1.rs", f"{stem}.gen_tests_1a.rs"]:
            m = pat.match(name)
            assert gen_tests_sibling_number(name, stem) == (int(m.group(1)) if m else None), (name, stem)


def test_has_gen_tests_mod_decl_matches_regex() -> None:
    for n in [0, 3, 30]:
        pat = re.compile(rf'\bmod\s+gen_tests_{n}\s*;')
        for src in [f"mod gen_tests_{n};", f"pub mod gen_tests_{n} ;", "mod gen_tests_30;",
                    f"amod gen_tests_{n};", f"mod  gen_tests_{n}\t;", "no decl", f"mod gen_tests_{n}x;"]:
            assert has_gen_tests_mod_decl(src, n) == bool(pat.search(src)), (src, n)


def test_strip_mod_test_prefix_matches_regex() -> None:
    for s in ["mod_foo", "test_foo", "tests_foo", "modfoo", "test", "tests_", "foo", "mod_test_x", ""]:
        assert strip_mod_test_prefix(s) == re.sub(r'^(mod|tests?)_', '', s), s


def test_tests_output_number_matches_regex() -> None:
    from pipeline_core.domain.generation import tests_output_number
    for name in ["tests_output_3.txt", "tests_output_12.txt", "tests_output_.txt",
                 "x_tests_output_5.txt", "tests_output_5.log", "other.txt", "tests_output_5x.txt"]:
        m = re.search(r"tests_output_(\d+)\.txt", name)
        assert tests_output_number(name) == (int(m.group(1)) if m else None), name


# --- ratchet sweep 2026-07-04: direct tests for regions previously reached ---
# --- only through e2e flows (coverage floor pipeline_core.domain) ---------- #
from pipeline_core.domain.generation import (  # noqa: E402
    _attr_path_value,
    _largest_fenced_test_body,
    extract_gen_tests_body,
    extract_test_mod,
    gen_tests_origin_cfgs,
    inject_gen_prelude_imports,
    prelude_import_names,
    strip_trailing_legacy_gen_block,
)


def test_prelude_import_names_groups_renames_dedupe() -> None:
    block = (
        "use std::string::{String, ToString};\n"
        "use std::error::Error as StdError;\n"
        "use std::vec::Vec;\n"
        "use std::vec::Vec;\n"
        "fn not_a_use() {}\n"
    )
    assert prelude_import_names(block) == "String, ToString, StdError, Vec"
    assert prelude_import_names("") == ""
    assert prelude_import_names("fn f() {}\n") == ""


def test_extract_test_mod_brace_counter() -> None:
    text = ('before\n#[cfg(test)]\n#[allow(dead_code)]\n'
            'mod gen_tests_1 {\n    fn t() { assert!(true); }\n}\nafter\n')
    out = extract_test_mod(text)
    assert out.startswith("#[cfg(test)]") and out.endswith("}")
    assert "assert!(true);" in out and "after" not in out


def test_extract_test_mod_absent_or_malformed() -> None:
    assert extract_test_mod("no cfg here") == ""
    assert extract_test_mod("#[cfg(test)]\nfn loose() {}") == ""
    assert extract_test_mod("#[cfg(test)]\nmod gen_tests_1;") == ""
    assert extract_test_mod("#[cfg(test)]\nmod gen_tests_1 { fn t() {") == ""


def test_extract_test_mod_takes_recorded_fenced_muse_replies() -> None:
    """Run 2881435-34981ab0: Muse wrote tests/{stem}_gen_1.rs as a ```rust
    fence. extract_test_mod keyed only on `#[cfg(test)] mod gen_` / `<FILE>`,
    so every block was dropped and t0c sat at the original-crate 83.9%.

    The recorded replies contain neither marker — that is the negative
    control (pre-fix extract returned "") — and must yield the tests the
    model wrote. The fmt reply also has a backtick path preamble, the
    other live miss shape."""
    from pathlib import Path

    root = Path(__file__).parent / "fixtures" / "task0c"
    bt = (root / "muse_fenced_backtrace_gen1.txt").read_text(encoding="utf-8")
    fmt = (root / "muse_fenced_fmt_gen1.txt").read_text(encoding="utf-8")
    assert "<FILE " not in bt and "#[cfg(test)]" not in bt
    assert "<FILE " not in fmt and "#[cfg(test)]" not in fmt
    out_bt = extract_test_mod(bt)
    assert "```" not in out_bt
    assert "fn test_error_can_be_moved_across_threads" in out_bt
    assert out_bt.count("#[test]") == 3
    out_fmt = extract_test_mod(fmt)
    assert "```" not in out_fmt
    assert "fn test_display_alternate_chain_three" in out_fmt
    assert out_fmt.count("#[test]") == 9
    wrapped = '<FILE path="tests/backtrace_gen_1.rs">\n' + bt + "\n</FILE>\n"
    out_w = extract_test_mod(wrapped)
    assert "fn test_error_can_be_moved_across_threads" in out_w
    assert "```" not in out_w


def test_extract_test_mod_file_tag_keeps_tests_written_outside_a_fence() -> None:
    """Inside a `<FILE>` tag the WHOLE body is the file, so the body may not be
    narrowed to one fence found within it.

    Negative control: the largest-fence rule, applied to this body, returns the
    doc-comment example and drops both real tests — asserted directly on
    `_largest_fenced_test_body` so the test fails if the FILE branch is ever
    routed back through it."""
    body = (
        "#![allow(unused)]\n"
        "use anyhow::*;\n"
        "/// Example:\n"
        "/// ```\n"
        "/// #[test] fn doc_example() {}\n"
        "/// ```\n"
        "#[test]\n"
        "fn real_one() { assert_eq!(1, 1); }\n"
        "#[test]\n"
        "fn real_two() { assert_eq!(2, 2); }\n"
    )
    assert _largest_fenced_test_body(body).count("#[test]") == 1        # the control
    assert "real_one" not in _largest_fenced_test_body(body)
    out = extract_test_mod(f'<FILE path="tests/x_gen_1.rs">\n{body}</FILE>')
    assert out.count("#[test]") == 3
    assert "fn real_one" in out and "fn real_two" in out


def test_extract_test_mod_recovers_a_fence_the_token_cap_truncated() -> None:
    """A reply cut off at the token cap has an opening fence and no closing one.

    Dropping it re-runs the whole generation; the tail is the compile-fix
    loop's job. The negative control is the same text with the opener removed —
    no fence at all is still not a file, and must stay "" so prose that merely
    mentions `#[test]` cannot be spliced."""
    truncated = (
        "```rust\n"
        "#![allow(unused)]\n"
        "use anyhow::*;\n"
        "#[test]\n"
        "fn t() { assert_eq!(1, 1); }\n"
    )
    out = extract_test_mod(truncated)
    assert out.count("#[test]") == 1
    assert "```" not in out
    assert extract_test_mod(truncated.split("\n", 1)[1]) == ""


def test_merge_gen_tests_blocks_drops_subset_inner_attributes() -> None:
    """Every subset reply opens with the prompt template's
    `#![allow(clippy::all, …)]`. Concatenating them puts the 2nd onwards after
    an item, which rustc refuses outright (`an inner attribute is not permitted
    in this context`) — measured on these two recorded replies merged, where it
    landed at line 41 of the sibling the pipeline would have written.

    The crate-level allow is re-injected by `as_integration_test`, so none may
    survive the merge, and every test of both subsets must."""
    from pathlib import Path

    root = Path(__file__).parent / "fixtures" / "task0c"
    subs = [extract_test_mod((root / f).read_text(encoding="utf-8"))
            for f in ("muse_fenced_backtrace_gen1.txt", "muse_fenced_fmt_gen1.txt")]
    assert all(b.count("#![allow(") == 1 for b in subs)   # the fixture still bites
    merged = merge_gen_tests_blocks(subs)
    assert merged is not None
    assert "#![" not in merged
    assert merged.count("#[test]") == 12                  # 3 + 9, none lost
    assert "fn test_error_can_be_moved_across_threads" in merged
    assert "fn test_display_alternate_chain_three" in merged


def test_merge_gen_tests_blocks_drops_a_multiline_inner_attribute() -> None:
    """The attribute may wrap; a line-wise drop would leave its tail behind as
    orphan tokens, which is a worse failure than the duplicate it replaces."""
    a = ("#![allow(\n    clippy::all,\n    dead_code,\n)]\n"
         "use anyhow::*;\n#[test]\nfn a() { assert_eq!(1, 1); }\n")
    b = "#![allow(unused)]\nuse anyhow::*;\n#[test]\nfn b() { assert_eq!(2, 2); }\n"
    merged = merge_gen_tests_blocks([a, b])
    assert merged is not None
    assert "#![" not in merged and "clippy::all" not in merged and ")]" not in merged
    assert merged.count("#[test]") == 2
    assert merged.count("use anyhow::*;") == 1


def test_extract_gen_tests_body_unbalanced_falls_through() -> None:
    block = "mod gen_tests_1 {\n    fn t() {"
    assert extract_gen_tests_body(block) == block.strip() + "\n"


def test_gen_tests_origin_cfgs_scan() -> None:
    text = (
        '#[cfg(feature = "std")]\n#[cfg(test)]\nmod gen_tests_1;\n'
        "#[cfg(test)]\nmod gen_tests_2;\n"
        "#[allow(dead_code)]\n#[cfg(test)]\nmod gen_tests_3;\n"
        "mod gen_tests_x;\n"
        "mod gen_tests_4\n"
    )
    assert gen_tests_origin_cfgs(text) == {
        1: 'feature = "std"', 2: "", 3: ""}


def test_gen_tests_origin_cfgs_reads_gate_past_a_wrapped_attribute() -> None:
    text = (_decl("m.gen_tests_1.rs", 1, allow=_WRAPPED_ALLOW)
            + _decl("m.gen_tests_2.rs", 2, cfg=_STABLE_GATE, allow=_WRAPPED_ALLOW))
    assert gen_tests_origin_cfgs(text) == {1: "", 2: _STABLE_GATE}


def test_inject_gen_prelude_imports_mod_opener_path() -> None:
    block = "mod gen_tests_1 {\n    #[test]\n    fn t() {}\n}\n"
    out = inject_gen_prelude_imports(block)
    assert out != block
    assert out.index("use std::string::{String, ToString};") < out.index("#[test]")
    # No gen-mod opener and no `use super::*;` → unchanged.
    bare = "#[test]\nfn t() {}\n"
    assert inject_gen_prelude_imports(bare) == bare
    # Integration-test crate: insert after `use anyhow::*;`.
    integ = "use anyhow::*;\n\n#[test]\nfn t() {}\n"
    out_i = inject_gen_prelude_imports(integ)
    assert out_i.index("use anyhow::*;") < out_i.index("use std::string::{String, ToString};")
    assert out_i.index("use std::string::{String, ToString};") < out_i.index("#[test]")


def test_parse_gen_tests_integration_filename_and_as_integration_test() -> None:
    from pipeline_core.domain.generation import (
        as_integration_test,
        extract_test_mod,
        gen_tests_integration_rel,
        parse_gen_tests_integration_filename,
    )
    assert parse_gen_tests_integration_filename("lib_gen_1.rs") == ("lib", 1)
    assert parse_gen_tests_integration_filename("error_gen_7.rs") == ("error", 7)
    assert parse_gen_tests_integration_filename("error.gen_tests_1.rs") is None
    assert parse_gen_tests_integration_filename("test_lib.rs") is None
    assert gen_tests_integration_rel("error", 3) == "tests/error_gen_3.rs"
    block = (
        "#[cfg(test)]\nmod gen_tests_1 {\n    use super::*;\n"
        "    #[test]\n    fn t() { assert!(true); }\n}\n")
    out = as_integration_test(block, crate="anyhow", allow_line="#[allow(clippy::all)]")
    assert "use super::*;" not in out
    assert "use anyhow::*;" in out
    assert "mod gen_tests_1" not in out
    assert out.startswith("#![allow(clippy::all)]")
    from pipeline_core.domain.generation import PIPELINE_GEN_TEST_ORIGIN
    assert PIPELINE_GEN_TEST_ORIGIN in out
    assert "fn t()" in out
    tagged = (
        'preamble\n<FILE path="tests/lib_gen_1.rs">\n'
        "use anyhow::*;\n#[test]\nfn t() {}\n</FILE>\n")
    extracted = extract_test_mod(tagged)
    assert extracted.startswith("use anyhow::*;")
    assert "FILE" not in extracted and "preamble" not in extracted
    assert extract_test_mod('<FILE path="src/error.rs">\nfn t() {}\n</FILE>') == ""


def test_authored_stem_gen_n_is_not_claimed_without_the_writer_origin() -> None:
    """`tests/proc_gen_2.rs` parses as `(proc, 2)` — occupancy, not origin.
    Claiming it as pipeline-generated would unprotect / skip-harvest / delete
    an authored crate file. The marker `as_integration_test` writes is the
    claim."""
    from pipeline_core.domain.generation import (
        PIPELINE_GEN_TEST_ORIGIN,
        as_integration_test,
        is_claimed_pipeline_gen_test,
        parse_gen_tests_integration_filename,
    )
    from pipeline_core.domain.test_assertions import harvest_test_assertion_behaviours
    assert parse_gen_tests_integration_filename("proc_gen_2.rs") == ("proc", 2)
    authored = (
        "#[test]\nfn t() {\n"
        "    let err = anyhow!(\"x\");\n"
        "    assert!(err.is::<&str>());\n"
        "}\n")
    assert not is_claimed_pipeline_gen_test("proc_gen_2.rs", authored)
    written = as_integration_test(authored, crate="anyhow")
    assert PIPELINE_GEN_TEST_ORIGIN in written
    assert is_claimed_pipeline_gen_test("lib_gen_1.rs", written)
    # Harvest: authored name is the oracle; marked file is ours.
    assert harvest_test_assertion_behaviours(
        authored, source_file="tests/proc_gen_2.rs")
    assert harvest_test_assertion_behaviours(
        written, source_file="tests/lib_gen_1.rs") == []


def test_parse_gen_tests_filename_edges() -> None:
    assert parse_gen_tests_filename("a.gen_tests_2.txt") is None
    assert parse_gen_tests_filename(".gen_tests_1.rs") is None
    assert parse_gen_tests_filename("a.gen_tests_x.rs") is None
    assert parse_gen_tests_filename("a.gen_tests_3.rs.broken") == ("a", 3)


def test_attr_path_value_quote_edges() -> None:
    assert _attr_path_value('#[path = "x.rs"]') == "x.rs"
    assert _attr_path_value("#[path = x]") is None
    assert _attr_path_value('#[path = "x') is None


def test_strip_trailing_legacy_gen_block_malformed_kept() -> None:
    unterminated_attr = "fn f() {}\n\n#[cfg(test)]\n#[allow(x"
    assert strip_trailing_legacy_gen_block(unterminated_attr) == unterminated_attr
    empty_mod_name = "fn f() {}\n\n#[cfg(test)]\nmod gen_ {\n}\n"
    assert strip_trailing_legacy_gen_block(empty_mod_name) == empty_mod_name


def test_testfile_paths_unclosed_quote_stops_scan() -> None:
    text = '<TESTFILE path="">\n<TESTFILE path="tests/a.rs">\n<TESTFILE path="'
    assert _testfile_paths(text) == ["tests/a.rs"]


def test_has_gen_tests_mod_decl_mod_word_edges() -> None:
    for src in ["module gen_tests_1;", "xx mod", "mod\tgen_tests_1  ;"]:
        pat = re.compile(r'\bmod\s+gen_tests_1\s*;')
        assert has_gen_tests_mod_decl(src, 1) == bool(pat.search(src)), src
