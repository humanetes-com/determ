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


# --- decl toolkit (R7 migration of the four legacy decl regexes) ---

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


# --- P7: gen_tests artifact-naming pins (byte-equivalent to the old regexes) ---

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
