"""Storage and scratch execution for the held-out captures
(`domain/held_out.py`).

The store is `.skeleton/_heldout_<cluster>.json`: beside the cluster suites,
committed with them at t0d and listed by the oracle seal under its own path
(`domain.behaviour.HELD_OUT_FILE_PREFIX`). The name is deliberately NOT
`_behaviours_*.json`: the fix loop builds its prompt-visible behaviour specs
from every cluster suite on disk (`domain.behaviour.suite_cluster_of`), so a
held-out file under that prefix would be read as one more cluster's suite
and its programs and Thens would sit in the fix prompts. `load_held_out` has one caller, the
agreement measurement.

The measurement runs in a scratch copy of the tree: the probe runner writes
an example file into the crate it runs, and the reconstructed tree is the
measurement the pipeline exists to take.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

from pipeline_core.domain.behaviour import BehaviourSuite, held_out_file_name
from pipeline_core.domain.held_out import AgreementRow
from pipeline_core.log import emit, log


def _left_behind(name: str) -> bool:
    """A top-level entry the scratch copy does not take: build output
    (`target`, `target-asan`, …) and dot-entries — the repository, the
    pipeline's stores and trees — except cargo's own `.cargo` config."""
    return name.startswith("target") or (name.startswith(".") and name != ".cargo")


def held_out_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / ".skeleton" / held_out_file_name(cfg_cluster)


def save_held_out(project_dir: Path, suite: BehaviourSuite) -> Path:
    """Write the held-out captures of `suite.cfg_cluster`; return the path."""
    p = held_out_path(project_dir, suite.cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(suite.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return p


def load_held_out(project_dir: Path, cfg_cluster: str) -> BehaviourSuite:
    """A cluster's held-out captures; empty when none were ever held out.
    Unreadable is reported and read as empty: the reading is additive."""
    p = held_out_path(project_dir, cfg_cluster)
    try:
        return BehaviourSuite.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return BehaviourSuite(cfg_cluster=cfg_cluster)
    except Exception as exc:
        log(f"  [behaviour] ‼ unreadable held-out store {p}: {type(exc).__name__}: {exc}")
        emit("behaviour.held_out.unreadable", data={"path": str(p), "cfg_cluster": cfg_cluster})
        return BehaviourSuite(cfg_cluster=cfg_cluster)


@contextmanager
def scratch_tree(tree_dir: Path) -> Iterator[Path]:
    """A throwaway copy of the crate at `tree_dir`, symlinks followed (the
    tree's `tests` is a symlink into the workspace oracle; a copied link
    would let a write through), build output and the pipeline's own
    dot-directories left out. Removed on exit."""
    root = Path(tempfile.mkdtemp(prefix="held_out_"))
    dst = root / "crate"
    try:
        shutil.copytree(
            tree_dir, dst, symlinks=False,
            ignore=lambda d, names: [n for n in names
                                     if Path(d) == Path(tree_dir) and _left_behind(n)])
        yield dst
    finally:
        with suppress(OSError):
            shutil.rmtree(root)


def record_agreement_rows(repo_root: Path, project: str, cluster: str, where: str,
                          rows: Sequence[AgreementRow]) -> Path:
    """The per-capture outcomes of one reading, for a person:
    `logs/behaviour_agreement/<project>/<cluster>_<where>.json`, rewritten
    whole. Nothing in the pipeline reads it back."""
    d = Path(repo_root) / "logs" / "behaviour_agreement" / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cluster}_{where}.json"
    p.write_text(json.dumps([r.to_dict() for r in rows], indent=2), encoding="utf-8")
    return p
