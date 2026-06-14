"""gen_tests reattach (P5-S3): single-tree fallback path stitches preserved
sibling bodies + decls back onto regenerated source."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline_core.application import gen_tests_reattach
from pipeline_core.application.gen_tests_reattach import ReattachDeps, reattach_gen_tests


def _deps() -> ReattachDeps:
    def build_decl(name: str, n: object, allow: str, gate: str = "") -> str:
        return f'\n#[cfg(test)]\n#[path = "{name}"]\nmod gen_tests_{n};\n'

    return ReattachDeps(
        log=lambda _m: None,
        emit=lambda *_a, **_k: None,
        derive_gentest_prelude=lambda _pd: ("#[allow(unused)]", ""),
        is_gen_tests_file=lambda name: ".gen_tests_" in name,
        gen_tests_siblings=lambda _sf: [],
        load_gen_tests_origin=lambda _pd: {},
        gen_tests_path=lambda src, n: src.with_name(f"{src.stem}.gen_tests_{n}.rs"),
        build_gen_tests_decl=build_decl,
        cluster_cfg_attr=lambda _c: "",
        has_gen_tests_decl=lambda txt, n: f"mod gen_tests_{n};" in txt,
        cargo_env=lambda: {},
    )


def test_single_tree_reattach_writes_sibling_and_decl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")

    # Stub the post-reattach `cargo check --tests` validation (returncode 0 →
    # no quarantine path). Avoids invoking a real toolchain in a unit test.
    monkeypatch.setattr(
        gen_tests_reattach.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))

    # No .skeleton-filled → single-tree (saved_gen_blocks) path.
    reattach_gen_tests(
        tmp_path, "demo",
        clusters=[],
        saved_gen_blocks={"src/lib.rs": [(1, "#[test] fn t() {}")]},
        existing_test_files=set(),
        deps=_deps(),
    )

    assert (src / "lib.gen_tests_1.rs").read_text() == "#[test] fn t() {}"
    body = (src / "lib.rs").read_text()
    assert "mod gen_tests_1;" in body
    assert 'path = "lib.gen_tests_1.rs"' in body


def test_noop_when_nothing_saved(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text("x", encoding="utf-8")
    reattach_gen_tests(
        tmp_path, "demo", clusters=[], saved_gen_blocks={},
        existing_test_files=set(), deps=_deps(),
    )
    assert (tmp_path / "lib.rs").read_text() == "x"
