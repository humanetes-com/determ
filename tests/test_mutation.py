"""Unit tests for `pipeline_core.adapters.mutation.MutationTester` (P6).

Covers the offline guards without invoking cargo-mutants: survivors→None when
the binary is absent, and harden_file→"" when there is no baseline.
"""
from __future__ import annotations

import pipeline_core.adapters.mutation as m
from pipeline_core.adapters.mutation import MutationTester


def _tester() -> MutationTester:
    return MutationTester(
        cargo_env=lambda: {}, ask=lambda *a, **k: "", log=lambda *_: None,
        task0c_model="m", single_call_char_limit=10_000,
    )


def test_survivors_none_when_cargo_mutants_absent(tmp_path, monkeypatch):
    # Force both presence checks to miss → returns None without running cargo.
    monkeypatch.setattr(m.shutil, "which", lambda _: None)
    monkeypatch.setattr(m.Path, "home", staticmethod(lambda: tmp_path))
    assert _tester().survivors(tmp_path) is None


def test_harden_returns_empty_without_baseline(tmp_path, monkeypatch):
    # No survivors baseline (cargo-mutants absent) → harden is a no-op "".
    monkeypatch.setattr(m.shutil, "which", lambda _: None)
    monkeypatch.setattr(m.Path, "home", staticmethod(lambda: tmp_path))
    assert _tester().harden_file(tmp_path, "mod", None, "src/lib.rs") == ""
