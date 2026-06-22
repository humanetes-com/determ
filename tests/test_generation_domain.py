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
