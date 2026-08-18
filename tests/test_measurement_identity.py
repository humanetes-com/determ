"""Pins for `code_identity`'s scope: the resume-invalidation identity covers
exactly what the pipeline process composes.

`pipeline_core/monitor/` is the standalone observer's library, and folding it
into the identity made an observer-only fix discard the TDD floor's pass
cursor and futility evidence (run `3097970-03bcf403`, 2026-08-18 — the floor
resumed `best_pass unset` and the crawl re-spent a ~68-minute relaunch cycle).
The exclusion is only sound while nothing the pipeline composes imports the
observer: the import-linter contract "The observer library is not composed
into the pipeline" holds that for `pipeline_core`'s layers, and the AST pin
below holds it for `pipeline.py` itself (grimp cannot take a single-file
module as a root, so the contract cannot)."""
from __future__ import annotations

import ast
from pathlib import Path

from pipeline_core.domain.measurement import code_identity

_REPO = Path(__file__).resolve().parents[1]


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pipeline_core" / "domain").mkdir(parents=True)
    (root / "pipeline_core" / "monitor").mkdir()
    (root / "pipeline.py").write_text("entry = 1\n", encoding="utf-8")
    (root / "pipeline_core" / "domain" / "loop.py").write_text(
        "def decide() -> int:\n    return 1\n", encoding="utf-8")
    (root / "pipeline_core" / "monitor" / "run_data.py").write_text(
        "def badge() -> str:\n    return 'running'\n", encoding="utf-8")
    return root


def test_a_monitor_only_edit_does_not_move_the_identity(tmp_path) -> None:
    """The observer's files are data to this identity: editing one must not
    invalidate a resume, because it cannot change a loop decision."""
    root = _tree(tmp_path)
    before = code_identity(root)
    mon = root / "pipeline_core" / "monitor" / "run_data.py"
    mon.write_text(mon.read_text(encoding="utf-8") + "\nchanged = True\n",
                   encoding="utf-8")
    assert code_identity(root) == before


def test_a_composed_module_edit_moves_the_identity(tmp_path) -> None:
    """Negative control: the exclusion is scoped to the observer alone — a
    domain edit (something the loop composes) must still invalidate."""
    root = _tree(tmp_path)
    before = code_identity(root)
    loop = root / "pipeline_core" / "domain" / "loop.py"
    loop.write_text(loop.read_text(encoding="utf-8") + "\nchanged = True\n",
                    encoding="utf-8")
    assert code_identity(root) != before


def test_an_entry_point_edit_moves_the_identity(tmp_path) -> None:
    root = _tree(tmp_path)
    before = code_identity(root)
    (root / "pipeline.py").write_text("entry = 2\n", encoding="utf-8")
    assert code_identity(root) != before


def test_pipeline_py_composes_nothing_from_the_observer_library() -> None:
    """The half of the exclusion's premise import-linter cannot enforce:
    `pipeline.py` imports nothing from `pipeline_core.monitor`."""
    tree = ast.parse((_REPO / "pipeline.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names
                          if a.name.startswith("pipeline_core.monitor")]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("pipeline_core.monitor"):
                offenders.append(mod)
            elif mod == "pipeline_core":
                offenders += [f"pipeline_core.{a.name}" for a in node.names
                              if a.name == "monitor"]
    assert not offenders, (
        f"pipeline.py imports the observer library ({offenders}); the "
        "code_identity exclusion of pipeline_core/monitor is no longer "
        "sound — either remove the import or remove the exclusion")
