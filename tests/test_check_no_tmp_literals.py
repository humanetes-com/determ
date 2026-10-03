"""The `/tmp`-literal gate: a `/tmp` path anywhere in the pipeline's code fails
the build unless it is in `domain/runtime_paths.py`.

Plan sentence: per-run artefacts and control channels are named in one module,
so the test suite can redirect all of them and no new one can be added beside
it. The gate fails on the three shapes the pipeline used (a `Path("/tmp")`
join, an f-string, a glob) and passes the owner module and paths that only
look alike. Negative control, computed on HEAD `f3fe484` when the gate was
added: the same scan reported 30 sites, among them the coverage adapter's six
and `clean_state`'s five globs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _gate():
    path = Path(__file__).resolve().parents[1] / "tools" / "check_no_tmp_literals.py"
    spec = importlib.util.spec_from_file_location("check_no_tmp_literals", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _gate()

_SHAPES = '''
import glob
from pathlib import Path

def outputs(module: str, attempt: int) -> list[object]:
    return [Path("/tmp") / f"llvmcov_{module}.lcov",
            Path(f"/tmp/stream_loop_{attempt}.log"),
            glob.glob("/tmp/grcov_*.json")]
'''

_LOOKALIKES = '''
import os

def paths(script_dir: str) -> list[str]:
    # A comment naming /tmp is prose, not a path.
    return [os.path.join(script_dir, "tmp", "anyhow"), "tmp/x", "/tmpfs/y"]
'''


def test_every_shape_of_a_tmp_path_is_a_breach() -> None:
    hits = gate.scan_source(_SHAPES, "pipeline_core/adapters/x.py")
    assert [h.split(":")[1] for h in hits] == ["6", "7", "8"]


def test_the_owner_module_and_lookalikes_pass() -> None:
    assert gate.scan_source(_SHAPES, gate.OWNER) == []
    assert gate.scan_source(_LOOKALIKES, "monitor.py") == []


def test_the_tree_is_clean() -> None:
    assert gate.main() == 0
