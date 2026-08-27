"""Unit tests for gen_tests_storage — filesystem I/O for gen_tests sibling files."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.gen_tests_storage import (
    gen_tests_count,
    gen_tests_path,
    gen_tests_siblings,
    load_gen_tests_origin,
    next_gen_tests_n,
    read_all_gen_tests,
    read_oracle_sibling_sources,
    revert_gen_tests_pass,
    strip_all_gen_tests_artifacts,
    write_gen_tests_integration,
    write_gen_tests_pass,
)
from pipeline_core.domain.generation import (
    has_gen_tests_mod_decl,
    strip_all_gen_tests_decls,
)


def _src(tmp_path: Path, name: str = "lib.rs", content: str = "pub fn x() {}\n") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ── sibling enumeration ─────────────────────────────────────────────────────

def test_siblings_empty(tmp_path: Path) -> None:
    src = _src(tmp_path)
    assert gen_tests_siblings(src) == []
    assert gen_tests_count(src) == 0


def test_siblings_sorted(tmp_path: Path) -> None:
    src = _src(tmp_path)
    (tmp_path / "lib.gen_tests_3.rs").write_text("// 3\n", encoding="utf-8")
    (tmp_path / "lib.gen_tests_1.rs").write_text("// 1\n", encoding="utf-8")
    (tmp_path / "lib.gen_tests_10.rs").write_text("// 10\n", encoding="utf-8")
    result = gen_tests_siblings(src)
    assert [n for n, _ in result] == [1, 3, 10]


def test_count_and_next(tmp_path: Path) -> None:
    src = _src(tmp_path)
    (tmp_path / "lib.gen_tests_2.rs").write_text("", encoding="utf-8")
    (tmp_path / "lib.gen_tests_5.rs").write_text("", encoding="utf-8")
    assert gen_tests_count(src) == 2
    assert next_gen_tests_n(src) == 6  # max existing + 1


def test_next_n_when_no_siblings(tmp_path: Path) -> None:
    src = _src(tmp_path)
    assert next_gen_tests_n(src) == 1


def test_gen_tests_path(tmp_path: Path) -> None:
    src = tmp_path / "error.rs"
    assert gen_tests_path(src, 3) == tmp_path / "error.gen_tests_3.rs"


# ── read_all_gen_tests ───────────────────────────────────────────────────────

def test_read_all_gen_tests_empty(tmp_path: Path) -> None:
    src = _src(tmp_path)
    assert read_all_gen_tests(src) == ""


def test_read_all_gen_tests_concatenates(tmp_path: Path) -> None:
    src = _src(tmp_path)
    (tmp_path / "lib.gen_tests_1.rs").write_text("fn a() {}", encoding="utf-8")
    (tmp_path / "lib.gen_tests_2.rs").write_text("fn b() {}", encoding="utf-8")
    out = read_all_gen_tests(src)
    assert "mod gen_tests_1" in out
    assert "fn a()" in out
    assert "mod gen_tests_2" in out
    assert "fn b()" in out


# ── load_gen_tests_origin ────────────────────────────────────────────────────

def test_load_gen_tests_origin_missing(tmp_path: Path) -> None:
    assert load_gen_tests_origin(tmp_path) == {}


def test_load_gen_tests_origin_valid(tmp_path: Path) -> None:
    skel = tmp_path / ".skeleton"
    skel.mkdir()
    (skel / "_gen_tests_origin.json").write_text(
        json.dumps({"lib.gen_tests_1": "nightly", "lib.gen_tests_2": ""}),
        encoding="utf-8",
    )
    result = load_gen_tests_origin(tmp_path)
    assert result == {"lib.gen_tests_1": "nightly", "lib.gen_tests_2": ""}


def test_load_gen_tests_origin_bad_json(tmp_path: Path) -> None:
    skel = tmp_path / ".skeleton"
    skel.mkdir()
    (skel / "_gen_tests_origin.json").write_text("not-json", encoding="utf-8")
    assert load_gen_tests_origin(tmp_path) == {}


# ── write_gen_tests_pass ─────────────────────────────────────────────────────

_VALID_BLOCK = "#[cfg(test)]\nmod gen_tests_1 {\n    fn t() {}\n}\n"
_BARE_BLOCK = "fn t() {}"  # bare: no outer mod wrapper


def test_write_gen_tests_pass_happy(tmp_path: Path) -> None:
    src = _src(tmp_path, content="pub fn x() {}\n")
    sibling = write_gen_tests_pass(src, "pub fn x() {}\n", _VALID_BLOCK, "", pass_n=1)
    assert sibling.exists()
    assert "fn t()" in sibling.read_text(encoding="utf-8")
    parent_text = src.read_text(encoding="utf-8")
    assert has_gen_tests_mod_decl(parent_text, 1)


def test_write_gen_tests_pass_broken_delimiters_quarantined(tmp_path: Path) -> None:
    src = _src(tmp_path, content="pub fn x() {}\n")
    unbalanced = "fn t() { { { } }\n"  # extra opening brace
    sibling = write_gen_tests_pass(src, "pub fn x() {}\n", unbalanced, "", pass_n=1)
    assert not sibling.exists()
    quarantine = sibling.with_suffix(".rs.broken")
    assert quarantine.exists()
    # parent must NOT have been updated
    assert not has_gen_tests_mod_decl(src.read_text(encoding="utf-8"), 1)


def test_write_gen_tests_pass_with_cluster_gate(tmp_path: Path) -> None:
    src = _src(tmp_path, content="pub fn x() {}\n")
    write_gen_tests_pass(src, "pub fn x() {}\n", _VALID_BLOCK, "", pass_n=1, cluster_cfg_attr="nightly")
    parent_text = src.read_text(encoding="utf-8")
    assert "#[cfg(nightly)]" in parent_text


# ── revert_gen_tests_pass ────────────────────────────────────────────────────

def test_revert_gen_tests_pass(tmp_path: Path) -> None:
    original = "pub fn x() {}\n"
    src = _src(tmp_path, content=original)
    sibling = write_gen_tests_pass(src, original, _VALID_BLOCK, "", pass_n=1)
    assert sibling.exists()
    revert_gen_tests_pass(src, original, sibling)
    assert src.read_text(encoding="utf-8") == original
    assert not sibling.exists()


# ── strip_all_gen_tests_artifacts ────────────────────────────────────────────

def test_strip_all_gen_tests_artifacts(tmp_path: Path) -> None:
    original = "pub fn x() {}\n"
    src = _src(tmp_path, content=original)
    write_gen_tests_pass(src, original, _VALID_BLOCK, "", pass_n=1)
    write_gen_tests_pass(src, src.read_text(encoding="utf-8"), _VALID_BLOCK, "", pass_n=2)
    count = strip_all_gen_tests_artifacts(src)
    assert count == 2
    siblings = gen_tests_siblings(src)
    assert siblings == []
    clean = src.read_text(encoding="utf-8")
    assert strip_all_gen_tests_decls(clean) == clean


def test_strip_all_gen_tests_artifacts_unlinks_this_stems_integration_files(
        tmp_path: Path) -> None:
    """Harness pre-clean / last-resort strip must own `tests/{stem}_gen_N.rs`
    the pipeline wrote (origin marker), the same way it owns leftover siblings.
    A different stem's file, an authored `tests/test_*.rs`, and an authored
    file whose name merely parses as `{stem}_gen_{N}` stay — those are not
    this src file's artefacts."""
    from pipeline_core.domain.generation import as_integration_test
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.0.0"\nedition = "2021"\n',
        encoding="utf-8")
    src = tmp_path / "src" / "lib.rs"
    src.parent.mkdir()
    src.write_text(
        'pub fn x() {}\n\n#[path = "lib.gen_tests_2.rs"]\nmod gen_tests_2;\n',
        encoding="utf-8")
    (src.parent / "lib.gen_tests_2.rs").write_text("// sibling\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    ours = tests / "lib_gen_1.rs"
    ours.write_text(as_integration_test("#[test] fn ours() {}\n", crate="x"),
                    encoding="utf-8")
    other_stem = tests / "error_gen_1.rs"
    other_stem.write_text("#[test] fn other() {}\n", encoding="utf-8")
    authored = tests / "test_downcast.rs"
    authored.write_text("#[test] fn oracle() {}\n", encoding="utf-8")
    name_collision = tests / "lib_gen_9.rs"
    name_collision.write_text("#[test] fn authored_lookalike() {}\n", encoding="utf-8")
    count = strip_all_gen_tests_artifacts(src)
    assert count == 2
    assert not ours.exists()
    assert not (src.parent / "lib.gen_tests_2.rs").exists()
    assert other_stem.exists() and authored.exists() and name_collision.exists()
    assert "mod gen_tests_2" not in src.read_text(encoding="utf-8")


# ── read_oracle_sibling_sources ──────────────────────────────────────────────
# The layout phase reads these to learn which member paths the crate's own
# write-protected tests take, so an authored private layout cannot rename a
# field out from under a file no stage may edit.

def _oracle_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "error.gen_tests_3.rs").write_text(
        "let _ = err.inner.ptr;\n", encoding="utf-8")
    (tmp_path / "src" / "error.rs").write_text("pub struct Error {}\n", encoding="utf-8")
    return tmp_path


def test_read_oracle_sibling_sources_reads_the_siblings_and_nothing_else(
    tmp_path: Path,
) -> None:
    read = read_oracle_sibling_sources(_oracle_tree(tmp_path))
    assert list(read) == ["src/error.gen_tests_3.rs"]
    assert read["src/error.gen_tests_3.rs"] == "let _ = err.inner.ptr;\n"


def test_read_oracle_sibling_sources_skips_the_working_trees(tmp_path: Path) -> None:
    """`.skeleton/`, `.skeleton-filled/`, `.best_tree/` and `.cluster_augmented/`
    each hold a COPY of the same siblings (38 apiece on `anyhow`, 2026-08-06).
    Reading them would multiply every path by the number of trees on disk and
    add no constraint; `target/` is build output for the same reason a wipe
    skips it."""
    root = _oracle_tree(tmp_path)
    for d in (".skeleton/stable/src", ".best_tree/src", "target/debug"):
        (root / d).mkdir(parents=True)
        (root / d / "error.gen_tests_3.rs").write_text("copy\n", encoding="utf-8")
    assert list(read_oracle_sibling_sources(root)) == ["src/error.gen_tests_3.rs"]


def test_write_gen_tests_integration_does_not_touch_src(tmp_path: Path) -> None:
    src = tmp_path / "src" / "lib.rs"
    src.parent.mkdir()
    original = "pub fn x() {}\n"
    src.write_text(original, encoding="utf-8")
    dest = write_gen_tests_integration(
        tmp_path, src,
        "#[cfg(test)]\nmod gen_tests_1 {\n    use super::*;\n    #[test]\n    fn t() {}\n}",
        crate="demo", allow_line="#[allow(unused)]", pass_n=1)
    assert dest == tmp_path / "tests" / "lib_gen_1.rs"
    body = dest.read_text(encoding="utf-8")
    assert "use demo::*;" in body and "use super::*;" not in body
    assert "mod gen_tests_1" not in body
    assert "#![allow(unused)]" in body
    from pipeline_core.domain.generation import PIPELINE_GEN_TEST_ORIGIN
    assert PIPELINE_GEN_TEST_ORIGIN in body
    assert src.read_text(encoding="utf-8") == original


def test_next_n_counts_integration_files_and_read_all_concatenates_them(
        tmp_path: Path) -> None:
    from pipeline_core.domain.generation import as_integration_test
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.0.0"\nedition = "2021"\n',
        encoding="utf-8")
    src = tmp_path / "src" / "lib.rs"
    src.parent.mkdir()
    src.write_text("pub fn x() {}\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "lib_gen_2.rs").write_text(
        as_integration_test("#[test] fn integ() {}\n", crate="x"), encoding="utf-8")
    assert next_gen_tests_n(src) == 3
    out = read_all_gen_tests(src)
    assert "tests/lib_gen_2.rs" in out and "fn integ" in out


def test_read_oracle_sibling_sources_on_a_missing_project_is_empty(
    tmp_path: Path,
) -> None:
    assert read_oracle_sibling_sources(tmp_path / "nope") == {}
