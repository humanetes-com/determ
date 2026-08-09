"""The strip walk must not descend into the pipeline's own scratch directories,
and the two lists that say so must agree.

`strip_to_tests.SKIP_DIRS` is a deliberate copy of
`pipeline_core.domain.workspace_layout.SKIP_DIR_NAMES` — that module is a
standalone stdlib-only CLI and importing the package would cost that. The copy
drifted, and the drift destroyed data: the canonical set carried `.best_tree`
and the copy did not, so on 2026-08-08 run `491052-1af6b4f4`'s strip logged 36
consecutive lines inside the recovery slot. `chain.rs`, `kind.rs`, `macros.rs`
and `nightly.rs` were DELETED from `.best_tree`; the other eight files were cut
back to signatures, leaving `.best_tree/src` at 8 files and 136 lines. That slot
is the tier heal restores a corrupted file from before it falls through to
nuking the file back to the baked skeleton.

One test pins the invariant (the sets are equal) and one pins the consequence
(a body inside `.best_tree` survives a strip byte-for-byte), because the first
alone would keep passing if both lists lost the same name.
"""
from __future__ import annotations

from pathlib import Path

import strip_to_tests
from pipeline_core.adapters import strip
from pipeline_core.domain.workspace_layout import SKIP_DIR_NAMES

BODY = "pub fn construct_from_boxed(b: Box<dyn StdError>) -> Error { Error::from(b) }\n"


def test_the_two_skip_lists_are_the_same_set() -> None:
    canonical, copy = set(SKIP_DIR_NAMES), set(strip_to_tests.SKIP_DIRS)
    assert canonical == copy, (
        "strip_to_tests.SKIP_DIRS has drifted from workspace_layout.SKIP_DIR_NAMES.\n"
        f"  missing from the strip copy: {sorted(canonical - copy)}\n"
        f"  missing from the canonical set: {sorted(copy - canonical)}\n"
        "A name present in only one of these is how the 2026-08-08 strip walked "
        "into .best_tree. Add it to both.")


def test_a_body_inside_best_tree_survives_a_strip(tmp_path: Path) -> None:
    """The consequence, end to end through the real `process_rust`. Reproduces
    the production shape: a filled cluster tree parked in the recovery slot
    while the crate's own `src/` is what strip is aimed at."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="c"\nversion="0.1.0"\n', encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn f() -> i32 { 41 + 1 }\n", encoding="utf-8")

    slots = [tmp_path / ".best_tree" / "stable" / "src",
             tmp_path / ".best_tree" / "_merged",
             tmp_path / ".skeleton-filled" / "stable" / "src",
             tmp_path / ".skeleton" / "stable" / "src"]
    for slot in slots:
        slot.mkdir(parents=True)
        (slot / "error.rs").write_text(BODY, encoding="utf-8")
        # chain.rs is implementation-only: no test block, so the strip walk
        # DELETES rather than strips it. That is what happened to the real
        # .best_tree/_merged/chain.rs, and a survival test that only planted a
        # strippable file would miss it.
        (slot / "chain.rs").write_text(BODY, encoding="utf-8")

    strip.strip_module_source(tmp_path, tmp_path)

    for slot in slots:
        rel = slot.relative_to(tmp_path)
        assert (slot / "chain.rs").exists(), f"{rel}/chain.rs was deleted by strip"
        assert (slot / "error.rs").read_text(encoding="utf-8") == BODY, \
            f"{rel}/error.rs was rewritten by strip"
    # ... and the strip still did its actual job on the crate's own source.
    assert "41 + 1" not in (src / "lib.rs").read_text(encoding="utf-8")


def test_the_original_structure_manifest_excludes_the_scratch_dirs(tmp_path: Path) -> None:
    """The manifest is TASK 2's file plan. A `.best_tree` path in it would make
    the recovery slot look like part of the crate to every later stage."""
    mod = tmp_path / "crate"
    (mod / "src").mkdir(parents=True)
    (mod / "src" / "lib.rs").write_text("pub fn f() {}", encoding="utf-8")
    for d in (".best_tree", ".skeleton-filled", ".skeleton", ".cluster_augmented"):
        (mod / d / "stable").mkdir(parents=True)
        (mod / d / "stable" / "error.rs").write_text(BODY, encoding="utf-8")

    strip.save_original_structure(tmp_path, mod)

    recorded = (tmp_path / strip.ORIGINAL_STRUCTURE_DIR / "crate.txt").read_text(
        encoding="utf-8").splitlines()
    assert recorded == ["crate/src/lib.rs"]
