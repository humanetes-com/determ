"""Strip adapter: original-structure recording + language guard."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters import strip


def test_save_original_structure_records_rs_files(tmp_path: Path) -> None:
    mod = tmp_path / "crate"
    (mod / "src").mkdir(parents=True)
    (mod / "src" / "lib.rs").write_text("pub fn f() {}", encoding="utf-8")
    (mod / "src" / "util.rs").write_text("pub fn g() {}", encoding="utf-8")
    (mod / "target").mkdir()
    (mod / "target" / "junk.rs").write_text("x", encoding="utf-8")  # skip-dir

    strip.save_original_structure(tmp_path, mod)

    manifest = (tmp_path / strip.ORIGINAL_STRUCTURE_DIR / "crate.txt").read_text()
    recorded = set(manifest.splitlines())
    assert recorded == {"crate/src/lib.rs", "crate/src/util.rs"}


def test_strip_module_source_skips_non_rust(tmp_path: Path) -> None:
    # No Cargo.toml → detect_language != "rust" → no mutation, no manifest.
    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")
    strip.strip_module_source(tmp_path, tmp_path)
    assert not (tmp_path / strip.ORIGINAL_STRUCTURE_DIR).exists()


def test_strip_module_source_rust_records_and_strips(tmp_path: Path) -> None:
    # Cargo.toml → rust branch: original structure recorded, impl body stripped
    # while the test block is preserved.
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="c"\nversion="0.1.0"\n', encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "pub fn f() -> i32 { 41 + 1 }\n"
        "#[cfg(test)]\nmod t { #[test] fn x() { assert_eq!(super::f(), 42); } }\n",
        encoding="utf-8")

    strip.strip_module_source(tmp_path, tmp_path)

    assert (tmp_path / strip.ORIGINAL_STRUCTURE_DIR / ".txt").exists() or \
        list((tmp_path / strip.ORIGINAL_STRUCTURE_DIR).glob("*.txt"))
    after = (src / "lib.rs").read_text()
    assert "#[test]" in after            # test content preserved
    assert "41 + 1" not in after         # implementation body stripped
