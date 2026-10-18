"""The crate-symbol gate reads executed strings only, and fails on a crate name.

On 2026-10-17 the pipeline held 16 executed literals naming symbols of the
crate under reconstruction; the gate over the last commit's tree lists them,
and over the working tree after the catalog's removal it is clean. These
tests pin the gate's reading on an invented module: a crate name in an
executed string is a breach, the same name in a docstring, an attribute
docstring or a comment is not.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "check_crate_symbols.py"


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_crate_symbols", _TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODULE = '''"""Module docstring naming widget_frobnicate is an explanation."""

LIMIT = 3
"""Attribute docstring naming widget_frobnicate is an explanation."""

# A comment naming widget_frobnicate is an explanation.


def instruction() -> str:
    """Docstring naming WidgetCore explains."""
    return "call widget_frobnicate on the WidgetCore before returning"


def generic() -> str:
    return "call the constructor before returning"
'''


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    g = _gate()
    pkg = tmp_path / "pipeline_core"
    pkg.mkdir()
    (pkg / "prompt.py").write_text(_MODULE, encoding="utf-8")
    (tmp_path / "tools" / "crate_symbols").mkdir(parents=True)
    (tmp_path / "tools" / "crate_symbols" / "widget.txt").write_text(
        "# header\nwidget_frobnicate\nWidgetCore\n", encoding="utf-8")
    monkeypatch.setattr(g, "ROOT", tmp_path)
    monkeypatch.setattr(g, "SYMBOL_DIR", tmp_path / "tools" / "crate_symbols")
    monkeypatch.setattr(g, "SCANNED", (pkg,))
    monkeypatch.setattr(g, "ALLOWLIST", {})
    return g


def test_a_crate_name_in_an_executed_string_is_a_breach(gate: ModuleType) -> None:
    hits = gate.scan(gate._symbols())
    assert sorted((rel, name) for rel, _line, name in hits) == [
        ("pipeline_core/prompt.py", "WidgetCore"),
        ("pipeline_core/prompt.py", "widget_frobnicate"),
    ], "only the returned instruction executes; every explanation is skipped"
    assert gate.main() == 1


def test_an_allowlisted_literal_passes_and_a_stale_line_fails(
        gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "ALLOWLIST", {
        ("pipeline_core/prompt.py", "WidgetCore"): "reason",
        ("pipeline_core/prompt.py", "widget_frobnicate"): "reason",
    })
    assert gate.main() == 0
    monkeypatch.setattr(gate, "ALLOWLIST", {
        **gate.ALLOWLIST, ("pipeline_core/prompt.py", "gone"): "reason"})
    assert gate.main() == 1


def test_no_symbol_list_is_a_failure_not_a_pass(
        gate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gate, "SYMBOL_DIR", tmp_path / "nowhere")
    assert gate.main() == 1


def test_the_working_tree_is_clean() -> None:
    assert _gate().main() == 0
