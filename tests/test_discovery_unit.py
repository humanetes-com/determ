"""Test/example discovery adapter (P5): pure filesystem walks extracted from
pipeline.py."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters import discovery


def test_match_gen_tests_sibling() -> None:
    assert discovery.match_gen_tests_sibling("lib.gen_tests_3.rs") == ("lib", 3)
    assert discovery.match_gen_tests_sibling("a.b.gen_tests_12.rs") == ("a.b", 12)
    assert discovery.match_gen_tests_sibling("lib.rs") is None
    assert discovery.match_gen_tests_sibling("lib.gen_tests_x.rs") is None
    assert discovery.match_gen_tests_sibling("gen_tests_1.rs") is None
    assert discovery.match_gen_tests_sibling("lib.gen_tests_1.txt") is None


def test_is_test_file_conventions(tmp_path: Path) -> None:
    def f(rel: str) -> bool:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return discovery.is_test_file(p, tmp_path)

    assert f("tests/foo.rs")          # test dir
    assert f("src/test_foo.py")       # snake prefix
    assert f("src/foo_test.go")       # snake suffix
    assert f("src/FooTest.java")      # camel
    assert f("src/foo.spec.ts")       # dot-separated
    assert f("src/lib.gen_tests_2.rs")  # gen_tests sibling
    assert not f("src/lib.rs")
    assert not f("src/helper.py")


def test_collect_examples(tmp_path: Path) -> None:
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "demo.rs").write_text("fn main() {}", encoding="utf-8")
    (tmp_path / "examples" / "skip.txt").write_text("x", encoding="utf-8")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "build.rs").write_text("x", encoding="utf-8")
    out = discovery.collect_examples(tmp_path)
    assert out == [("examples/demo.rs", "fn main() {}")]


def test_collect_test_and_protected_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "it.rs").write_text("#[test] fn t() {}", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "pub fn f() {}\n#[cfg(test)]\nmod t { #[test] fn x() {} }", encoding="utf-8")
    (tmp_path / "src" / "plain.rs").write_text("pub fn g() {}", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "test_x.rs").write_text("#[test]", encoding="utf-8")

    test_files = dict(discovery.collect_test_files(tmp_path))
    assert "tests/it.rs" in test_files          # pure test file
    assert "src/lib.rs" in test_files           # inline #[cfg(test)]
    assert "src/plain.rs" not in test_files     # no test markers
    assert not any(".git" in k for k in test_files)  # skip-dir excluded

    protected = discovery.collect_protected_test_files(tmp_path)
    assert protected == {"tests/it.rs"}         # excludes inline-test src files


def test_collect_test_files_skips_decl_only_src(tmp_path: Path) -> None:
    """A stripped src file holding only gen_tests decls is dropped via the
    injected predicate; the real test content lives in the sibling."""
    decl_only = ('#[cfg(test)]\n#[path = "lib.gen_tests_1.rs"]\nmod gen_tests_1;')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(decl_only, encoding="utf-8")

    discovery.configure(is_gen_tests_decl_only=lambda c: c == decl_only)
    try:
        out = dict(discovery.collect_test_files(tmp_path))
        assert "src/lib.rs" not in out
    finally:
        discovery.configure(is_gen_tests_decl_only=lambda _c: False)
