"""gen_tests reattach (P5-S3): single-tree fallback + per-cluster filled paths
+ the post-reattach compile-validate / quarantine branches."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline_core.adapters.discovery import match_gen_tests_sibling
from pipeline_core.application import gen_tests_reattach
from pipeline_core.application.gen_tests_reattach import ReattachDeps, reattach_gen_tests
from pipeline_core.domain.models import BuildCluster


def _cluster(name: str, *, is_default: bool = True, active: list[str] | None = None) -> BuildCluster:
    return BuildCluster(
        name=name, env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=active or [], prompt_nightly_rule="",
        is_default=is_default,
    )


def _siblings(sf: Path) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for f in sf.parent.iterdir():
        m = match_gen_tests_sibling(f.name)
        if m and m[0] == sf.stem:
            out.append((m[1], f))
    return sorted(out)


def _deps(origin: dict[str, str] | None = None) -> ReattachDeps:
    def build_decl(name: str, n: object, allow: str, gate: str = "") -> str:
        head = f"#[cfg({gate})]\n" if gate else ""
        return f'\n{head}#[path = "{name}"]\nmod gen_tests_{n};\n'

    return ReattachDeps(
        log=lambda _m: None,
        emit=lambda *_a, **_k: None,
        derive_gentest_prelude=lambda _pd: ("#[allow(unused)]", ""),
        is_gen_tests_file=lambda name: match_gen_tests_sibling(name) is not None,
        gen_tests_siblings=_siblings,
        load_gen_tests_origin=lambda _pd: dict(origin or {}),
        gen_tests_path=lambda src, n: src.with_name(f"{src.stem}.gen_tests_{n}.rs"),
        build_gen_tests_decl=build_decl,
        cluster_cfg_attr=lambda c: (c.active_cfg[0] if c and c.active_cfg else ""),
        has_gen_tests_decl=lambda txt, n: f"mod gen_tests_{n};" in txt,
        cargo_env=lambda: {},
    )


def _ok_cargo(monkeypatch: pytest.MonkeyPatch, rc: int = 0, out: str = "") -> None:
    monkeypatch.setattr(
        gen_tests_reattach.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], rc, out, ""))


# --- single-tree fallback ---------------------------------------------------

def test_single_tree_reattach_writes_sibling_and_decl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    _ok_cargo(monkeypatch)

    reattach_gen_tests(
        tmp_path, "demo", clusters=[],
        saved_gen_blocks={"src/lib.rs": [(1, "#[test] fn t() {}")]},
        existing_test_files=set(), deps=_deps(),
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


# --- per-cluster filled paths ----------------------------------------------

def _seed_filled(tmp_path: Path, cluster: str, body: str, *, n: int = 1) -> None:
    cdir = tmp_path / ".skeleton-filled" / cluster / "src"
    cdir.mkdir(parents=True)
    (cdir / "lib.rs").write_text("pub fn f() {}", encoding="utf-8")
    (cdir / f"lib.gen_tests_{n}.rs").write_text(body, encoding="utf-8")


def test_per_cluster_identical_emits_ungated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    _seed_filled(tmp_path, "default", "BODY")
    _ok_cargo(monkeypatch)

    reattach_gen_tests(
        tmp_path, "demo", clusters=[_cluster("default")],
        saved_gen_blocks={}, existing_test_files=set(), deps=_deps(),
    )
    assert (tmp_path / "src" / "lib.gen_tests_1.rs").read_text() == "BODY"
    decl = (tmp_path / "src" / "lib.rs").read_text()
    assert "mod gen_tests_1;" in decl and "#[cfg(" not in decl  # ungated


def test_per_cluster_origin_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    _seed_filled(tmp_path, "nightly", "NB")
    _ok_cargo(monkeypatch)

    reattach_gen_tests(
        tmp_path, "demo",
        clusters=[_cluster("nightly", is_default=False, active=["feat"])],
        saved_gen_blocks={}, existing_test_files=set(),
        deps=_deps(origin={"lib.gen_tests_1": "feat"}),
    )
    assert (tmp_path / "src" / "lib.gen_tests_1.rs").read_text() == "NB"
    assert "#[cfg(feat)]" in (tmp_path / "src" / "lib.rs").read_text()  # gated


def test_per_cluster_divergent_emits_per_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    _seed_filled(tmp_path, "default", "BODY_A")
    _seed_filled(tmp_path, "nightly", "BODY_B")
    _ok_cargo(monkeypatch)

    reattach_gen_tests(
        tmp_path, "demo",
        clusters=[_cluster("default"), _cluster("nightly", is_default=False, active=["feat"])],
        saved_gen_blocks={}, existing_test_files=set(), deps=_deps(),
    )
    assert (tmp_path / "src" / "lib.gen_tests_1_default.rs").read_text() == "BODY_A"
    assert (tmp_path / "src" / "lib.gen_tests_1_nightly.rs").read_text() == "BODY_B"


# --- post-reattach compile validation --------------------------------------

def test_reattach_compile_fail_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    # cargo check fails, mentioning the sibling → compile-fail branch (no quarantine).
    _ok_cargo(monkeypatch, rc=1, out="error in lib.gen_tests_1.rs")
    monkeypatch.delenv("GEN_TESTS_REATTACH_QUARANTINE", raising=False)

    reattach_gen_tests(
        tmp_path, "demo", clusters=[],
        saved_gen_blocks={"src/lib.rs": [(1, "broken")]},
        existing_test_files=set(), deps=_deps(),
    )
    assert (src / "lib.gen_tests_1.rs").exists()  # left in place for Phase C


def test_reattach_legacy_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    _ok_cargo(monkeypatch, rc=1, out="error in lib.gen_tests_1.rs")
    monkeypatch.setenv("GEN_TESTS_REATTACH_QUARANTINE", "1")

    reattach_gen_tests(
        tmp_path, "demo", clusters=[],
        saved_gen_blocks={"src/lib.rs": [(1, "broken")]},
        existing_test_files=set(), deps=_deps(),
    )
    assert not (src / "lib.gen_tests_1.rs").exists()  # renamed to .broken
    assert (src / "lib.gen_tests_1.rs.broken").exists()
