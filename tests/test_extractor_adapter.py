"""Unit tests for the CliExtractor adapter (the Python↔Rust syn seam).

The adapter shells the skeleton-extractor binary; these tests fake `subprocess.run` so the
ARGV construction (subcommand + `--input` + cfg flags) and the JSON→typed-record parsing
are exercised without the binary. The seam is sacred (CODE_ARCHITECTURE.md §4), so the
wire-dict → record mapping and the uniform None-on-failure contract are pinned here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline_core.adapters import extractor as ext
from pipeline_core.adapters.extractor import CliExtractor, _cfg_args
from pipeline_core.domain.models import ConstRecord, DispatchFamily, FnRecord, ModRecord, TypeRecord


def _bin(tmp_path: Path) -> Path:
    """A real on-disk path so `self._bin.exists()` is True (content irrelevant — run is faked)."""
    p = tmp_path / "skeleton-extractor"
    p.write_text("#!/bin/sh\n")
    return p


class _FakeRun:
    """Records the argv of each call and returns a canned CompletedProcess-like object."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "",
                 raises: BaseException | None = None) -> None:
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


# --------------------------------------------------------------------------- _cfg_args

def test_cfg_args_interleaves_true_then_false() -> None:
    assert _cfg_args(("std", "alloc"), ("nightly",)) == [
        "--cfg-true", "std", "--cfg-true", "alloc", "--cfg-false", "nightly",
    ]
    assert _cfg_args((), ()) == []


# --------------------------------------------------------------------------- enumerate_* happy path + argv

def test_enumerate_fns_parses_records_and_builds_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(stdout='[{"fn_name": "f", "file": "src/a.rs", "is_pub": true}]')
    monkeypatch.setattr(ext.subprocess, "run", fake)
    out = CliExtractor(_bin(tmp_path)).enumerate_fns(
        tmp_path, cfg_true=("std",), cfg_false=("nightly",))
    assert out == [FnRecord.from_dict({"fn_name": "f", "file": "src/a.rs", "is_pub": True})]
    assert isinstance(out[0], FnRecord) and out[0].fn_name == "f" and out[0].is_pub
    # argv: [bin, subcmd, --input, dir, --cfg-true std, --cfg-false nightly]
    argv = fake.calls[0]
    assert argv[1] == "--enumerate-fns"
    assert argv[2:4] == ["--input", str(tmp_path)]
    assert "--cfg-true" in argv and "std" in argv and "--cfg-false" in argv and "nightly" in argv


@pytest.mark.parametrize(
    ("method", "subcmd", "row", "rec_cls"),
    [
        ("enumerate_types", "--enumerate-types", {"name": "T", "kind": "struct"}, TypeRecord),
        ("enumerate_mods", "--enumerate-mods", {"name": "m", "file": "m.rs"}, ModRecord),
        ("enumerate_consts", "--enumerate-consts", {"name": "C", "ty": "u8"}, ConstRecord),
        ("enumerate_dispatch_families", "--enumerate-dispatch-families", {"method": "kind"}, DispatchFamily),
    ],
)
def test_enumerate_record_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  method: str, subcmd: str, row: dict[str, Any],
                                  rec_cls: type) -> None:
    import json
    fake = _FakeRun(stdout=json.dumps([row]))
    monkeypatch.setattr(ext.subprocess, "run", fake)
    out = getattr(CliExtractor(_bin(tmp_path)), method)(tmp_path)
    assert out is not None and len(out) == 1 and isinstance(out[0], rec_cls)
    assert fake.calls[0][1] == subcmd


def test_enumerate_feature_gate_cfgs_returns_strings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(stdout='["std", "alloc"]')
    monkeypatch.setattr(ext.subprocess, "run", fake)
    assert CliExtractor(_bin(tmp_path)).enumerate_feature_gate_cfgs(tmp_path) == ["std", "alloc"]


# --------------------------------------------------------------------------- failure contract → None

def test_enumerate_missing_binary_returns_none(tmp_path: Path) -> None:
    assert CliExtractor(tmp_path / "absent").enumerate_fns(tmp_path) is None


def test_enumerate_nonzero_rc_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(returncode=1, stdout="[]"))
    assert CliExtractor(_bin(tmp_path)).enumerate_fns(tmp_path) is None


def test_enumerate_bad_json_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(stdout="not json{"))
    assert CliExtractor(_bin(tmp_path)).enumerate_types(tmp_path) is None


def test_enumerate_non_list_json_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(stdout='{"not": "a list"}'))
    assert CliExtractor(_bin(tmp_path)).enumerate_fns(tmp_path) is None


def test_enumerate_subprocess_raises_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(raises=OSError("boom")))
    assert CliExtractor(_bin(tmp_path)).enumerate_fns(tmp_path) is None


# --------------------------------------------------------------------------- skeletonise

def test_skeletonise_ok_parses_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(stdout='[{"file": "a.rs", "fns": 3}]', stderr="warn")
    monkeypatch.setattr(ext.subprocess, "run", fake)
    out_dir = tmp_path / "out"
    res = CliExtractor(_bin(tmp_path)).skeletonise(tmp_path, out_dir)
    assert (res.ok, res.reports, res.stderr) == (True, [{"file": "a.rs", "fns": 3}], "warn")
    assert out_dir.is_dir()  # output dir created
    # argv carries --input/--output/--report
    argv = fake.calls[0]
    assert "--report" in argv and "--output" in argv and str(out_dir) in argv


def test_skeletonise_empty_stdout_yields_empty_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(stdout="   "))
    res = CliExtractor(_bin(tmp_path)).skeletonise(tmp_path, tmp_path / "o")
    assert res.ok is True and res.reports == []


def test_skeletonise_missing_binary(tmp_path: Path) -> None:
    res = CliExtractor(tmp_path / "absent").skeletonise(tmp_path, tmp_path / "o")
    assert res.ok is False and "binary missing" in res.stderr


def test_skeletonise_nonzero_rc_reports_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(returncode=2, stderr="  parse failed  "))
    res = CliExtractor(_bin(tmp_path)).skeletonise(tmp_path, tmp_path / "o")
    assert res.ok is False and res.stderr == "parse failed"


def test_skeletonise_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(raises=subprocess.TimeoutExpired("c", 120)))
    res = CliExtractor(_bin(tmp_path)).skeletonise(tmp_path, tmp_path / "o")
    assert res.ok is False and "timed out" in res.stderr


def test_skeletonise_bad_json_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(stdout="{bad json"))
    res = CliExtractor(_bin(tmp_path)).skeletonise(tmp_path, tmp_path / "o")
    assert res.ok is False and "could not parse JSON report" in res.stderr


def test_skeletonise_generic_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(raises=RuntimeError("nope")))
    res = CliExtractor(_bin(tmp_path)).skeletonise(tmp_path, tmp_path / "o")
    assert res.ok is False and "invocation failed" in res.stderr


# --------------------------------------------------------------------------- merge_clusters

def test_merge_clusters_builds_cluster_args_and_returns_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(returncode=0)
    monkeypatch.setattr(ext.subprocess, "run", fake)
    ok = CliExtractor(_bin(tmp_path)).merge_clusters(
        tmp_path / "merged.rs",
        [("stable", Path("src/a.rs"), "feature=\"std\""), ("nightly", Path("src/b.rs"), "nightly")],
    )
    assert ok is True
    argv = fake.calls[0]
    assert "--merge-clusters" in argv and "--merge-output" in argv
    assert "stable:src/a.rs:feature=\"std\"" in argv
    assert "nightly:src/b.rs:nightly" in argv


def test_merge_clusters_missing_binary_false(tmp_path: Path) -> None:
    assert CliExtractor(tmp_path / "absent").merge_clusters(tmp_path / "m.rs", []) is False


def test_merge_clusters_nonzero_rc_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(returncode=1))
    assert CliExtractor(_bin(tmp_path)).merge_clusters(tmp_path / "m.rs", []) is False


def test_merge_clusters_exception_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext.subprocess, "run", _FakeRun(raises=OSError("x")))
    assert CliExtractor(_bin(tmp_path)).merge_clusters(tmp_path / "m.rs", []) is False


# --------------------------------------------------------------------------- real-binary merge pin

_REAL_BIN = Path(__file__).resolve().parent.parent / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"


@pytest.mark.skipif(not _REAL_BIN.exists(), reason="skeleton-extractor binary not built (bash setup.sh)")
def test_real_merge_keeps_cfg_variant_fns_distinct(tmp_path: Path) -> None:
    """End-to-end pin through the real binary (hardcrate cap-trap regression): two
    cfg-variant `cap_limit` fns must merge into two distinct gated defs with the
    filled body each, never an `any(x, not(x))` collapse with a leftover stub."""
    gate_extra = 'any(extra_cap, feature = "extra_cap")'
    gate_stable = f"not({gate_extra})"
    stable = tmp_path / "stable.rs"
    stable.write_text(
        f"#[cfg({gate_extra})]\npub fn cap_limit() -> usize {{ ::core::unimplemented!() }}\n"
        f"#[cfg({gate_stable})]\npub fn cap_limit() -> usize {{ 64 }}\n")
    extra = tmp_path / "extra.rs"
    extra.write_text(
        f"#[cfg({gate_extra})]\npub fn cap_limit() -> usize {{ 255 }}\n"
        f"#[cfg({gate_stable})]\npub fn cap_limit() -> usize {{ ::core::unimplemented!() }}\n")
    out_path = tmp_path / "merged.rs"
    ok = CliExtractor(_REAL_BIN).merge_clusters(
        out_path, [("stable", stable, gate_stable), ("extra_cap", extra, gate_extra)])
    assert ok is True
    out = out_path.read_text()
    assert out.count("fn cap_limit") == 2, out
    assert "255" in out and "64" in out, out
    assert "unimplemented" not in out, out
    assert "any(any(" not in out.replace(" ", ""), out
