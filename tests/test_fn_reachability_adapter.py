"""The adapter that takes rustc's MIR and public index for one cluster world.

`run_bounded` is replaced by a recorder, so these tests pin what is ASKED of
cargo (toolchain, `-Z` flags, the scoped `RUSTC_BOOTSTRAP`, the private target
directory) and how each way of failing is reported. The real commands run in
`tests/integration/test_fn_reachability_real_rustc.py`.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pipeline_core.adapters.fn_reachability as adapter
from pipeline_core.adapters.process import BoundedRun
from pipeline_core.domain.enums import ProcessFailure


class _Cargo:
    def __init__(self, tmp: Path, *, mir_rc: int = 0, doc_rc: int = 0,
                 write_json: bool = True, failure: ProcessFailure | None = None) -> None:
        self.tmp = tmp
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.mir_rc, self.doc_rc, self.write_json, self.failure = mir_rc, doc_rc, write_json, failure

    def __call__(self, cmd: list[str], *, cwd: Path, env: dict[str, str],
                 timeout: float) -> BoundedRun:
        self.calls.append((cmd, dict(env)))
        if self.failure is not None:
            return BoundedRun(-1, "", "", self.failure, None)
        if "rustc" in cmd:
            return BoundedRun(self.mir_rc, "fn f() -> () {\n}\n", "error: boom\n", None, None)
        if self.write_json:
            doc = Path(env["CARGO_TARGET_DIR"]) / "doc"
            doc.mkdir(parents=True, exist_ok=True)
            (doc / "my_crate.json").write_text('{"index": {}}', encoding="utf-8")
        return BoundedRun(self.doc_rc, "", "error: doc boom\n", None, None)


def _project(tmp_path: Path, name: str = "my-crate") -> Path:
    (tmp_path / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n', encoding="utf-8")
    return tmp_path


def _measure(tmp_path: Path, cargo: _Cargo, monkeypatch: pytest.MonkeyPatch,
             toolchain: str | None = None) -> Any:
    monkeypatch.setattr(adapter, "run_bounded", cargo)
    cluster = SimpleNamespace(name="stable", toolchain=toolchain)
    return adapter.measure_world_reachability(
        _project(tmp_path), cluster, cluster_env=lambda c: {"RUSTFLAGS": "--cfg x"})


def test_it_asks_cargo_for_the_mir_and_the_public_index_in_a_private_target_dir(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cargo = _Cargo(tmp_path)
    got = _measure(tmp_path, cargo, monkeypatch)
    assert got.measured and got.mir.startswith("fn f()") and got.rustdoc == '{"index": {}}'
    (mir_cmd, mir_env), (doc_cmd, _) = cargo.calls
    assert mir_cmd == ["cargo", "rustc", "--lib", "--", "-Zunpretty=mir"]
    assert doc_cmd == ["cargo", "rustdoc", "--lib", "--", "-Zunstable-options",
                       "--output-format", "json", "--document-hidden-items"]
    assert "--document-private-items" not in doc_cmd
    # The `-Z` flags are enabled for THIS crate only, never for build.rs probes.
    assert mir_env["RUSTC_BOOTSTRAP"] == "my_crate"
    assert mir_env["CARGO_TARGET_DIR"] == str(tmp_path / "target" / "fn-reach" / "stable")
    assert mir_env["RUSTFLAGS"] == "--cfg x"


def test_a_cluster_toolchain_is_passed_to_cargo(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cargo = _Cargo(tmp_path)
    _measure(tmp_path, cargo, monkeypatch, toolchain="nightly")
    assert all(cmd[:2] == ["cargo", "+nightly"] for cmd, _ in cargo.calls)


def test_each_way_of_failing_is_a_reason_and_no_reading(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for sub in "abcd":
        (tmp_path / sub).mkdir()
    mir = _measure(tmp_path / "a", _Cargo(tmp_path, mir_rc=101), monkeypatch)
    assert not mir.measured and "cargo rustc rc=101: error: boom" in mir.reason
    doc = _measure(tmp_path / "b", _Cargo(tmp_path, doc_rc=101), monkeypatch)
    assert not doc.measured and "cargo rustdoc rc=101" in doc.reason
    timeout = _measure(tmp_path / "c", _Cargo(tmp_path, failure=ProcessFailure.TIMEOUT), monkeypatch)
    assert not timeout.measured and "did not finish" in timeout.reason
    nojson = _measure(tmp_path / "d", _Cargo(tmp_path, write_json=False), monkeypatch)
    assert not nojson.measured and "rustdoc json not written" in nojson.reason


def test_a_manifest_without_a_package_name_is_no_reading(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "run_bounded", _Cargo(tmp_path))
    (tmp_path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    got = adapter.measure_world_reachability(
        tmp_path, SimpleNamespace(name="stable"), cluster_env=lambda c: {})
    assert not got.measured and got.reason == "no package name in Cargo.toml"
