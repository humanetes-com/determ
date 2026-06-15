"""Pure domain helpers in pipeline_core.domain.generation: filename parsing,
test-prefix stripping, TESTFILE marker scan, topo-sort, and the regex-free
gen_tests decl-strip (#9). All assert equivalence to the legacy regex behaviour."""
from __future__ import annotations

from pipeline_core.domain.generation import (
    parse_gen_tests_filename,
    strip_gen_tests_decl,
    strip_test_prefix,
    testfile_paths,
    topo_sort_files,
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


def test_strip_test_prefix() -> None:
    assert strip_test_prefix("test_foo") == "foo"
    assert strip_test_prefix("tests_foo") == "foo"   # not matched by test_
    assert strip_test_prefix("it_foo") == "foo"
    assert strip_test_prefix("foo") == "foo"


def test_testfile_paths() -> None:
    text = '<TESTFILE path="a.rs">x</TESTFILE><TESTFILE path="b/c.rs">'
    assert testfile_paths(text) == ["a.rs", "b/c.rs"]
    assert testfile_paths('<TESTFILE path="">') == []   # empty skipped (regex `+`)


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
