"""Characterization for `adapters.workspace_lifecycle.clean_state`
(P6 relocation of `pipeline.clean_state`).

`delete_source_files` is injected as a fake (records its call); everything else
is real filesystem mutation against a temp workspace.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from pipeline_core.adapters.workspace_lifecycle import clean_state
from pipeline_core.domain.workspace_layout import (
    CLUSTER_AUGMENTED_DIR,
    FAILURE_LOG_NAME,
    LOCKED_MODULES_DIR,
    MODULE_AUGMENTED_DIR,
    PROJECT_PROFILE_NAME,
)


def test_clean_state_wipes_generated_artifacts() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "ws"
        (proj / "src").mkdir(parents=True)

        # Per-project documents + failure log + a generated-files manifest.
        (proj / PROJECT_PROFILE_NAME).write_text("x")
        (proj / FAILURE_LOG_NAME).write_text("x")
        (proj / ".generated_files_mod.txt").write_text("x")
        # Sentinel dirs.
        (proj / CLUSTER_AUGMENTED_DIR).mkdir()
        (proj / MODULE_AUGMENTED_DIR).mkdir()
        (proj / LOCKED_MODULES_DIR).mkdir()
        # A gen_tests sibling + a source file carrying a gen_tests decl.
        (proj / "src" / "lib.gen_tests_1.rs").write_text("fn t() {}")
        (proj / "src" / "lib.rs").write_text(
            'pub fn f() {}\n#[cfg(test)]\n#[path = "lib.gen_tests_1.rs"]\nmod gen_tests_1;\n')

        manifest = proj / "manifest.txt"
        manifest.write_text("src/lib.rs\n")
        genesis = proj / "genesis.md"
        fr = proj / "functional_requirements.md"

        calls: list[tuple[Path, Path]] = []
        clean_state(root, proj, manifest, genesis, fr,
                    delete_source_files=lambda pd, mp: calls.append((pd, mp)))

        assert calls == [(proj, manifest)]
        assert not (proj / PROJECT_PROFILE_NAME).exists()
        assert not (proj / FAILURE_LOG_NAME).exists()
        assert not (proj / ".generated_files_mod.txt").exists()
        assert not (proj / CLUSTER_AUGMENTED_DIR).exists()
        assert not (proj / MODULE_AUGMENTED_DIR).exists()
        assert not (proj / LOCKED_MODULES_DIR).exists()
        assert not (proj / "src" / "lib.gen_tests_1.rs").exists()
        # gen_tests decl stripped from the surviving source file.
        assert "gen_tests_1" not in (proj / "src" / "lib.rs").read_text()


def test_clean_state_skips_delete_when_no_manifest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "ws"
        proj.mkdir()
        calls: list[object] = []
        clean_state(root, proj, proj / "absent.txt", proj / "g.md", proj / "fr.md",
                    delete_source_files=lambda *a: calls.append(a))
        assert calls == []
