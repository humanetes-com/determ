"""Unit tests for `pipeline_core.adapters.cluster_merge.ClusterMerger` (P6).

Covers the offline branches (no extractor I/O): no cluster outputs → zero result,
and the verbatim-copy fallback when the extractor binary is absent.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.cluster_merge import ClusterMerger


def _merger(tmp_path: Path) -> ClusterMerger:
    # Point at a non-existent binary → forces the passthrough fallback path.
    return ClusterMerger(extractor_bin=tmp_path / "nope", log=lambda *_: None)


def test_no_cluster_outputs_returns_zero(tmp_path):
    m = _merger(tmp_path)
    out = m.merge_cluster_outputs(tmp_path, {}, [])
    assert out["files_written"] == 0
    assert out["cfg_gated_fns"] == 0
    assert out["files_ast_merged"] == 0
    assert out["files_passthrough"] == 0


def test_passthrough_is_counted_separately_from_a_real_merge(tmp_path):
    # `files_written` counted an AST merge and the verbatim-copy fallback
    # identically, so a run where the extractor binary was missing and every
    # file degraded reported the same figure as a clean merge. Run
    # `525803-9a51be5a` wrote 2 510 records and not one named the merge at all.
    cdir = tmp_path / ".skeleton-filled" / "base" / "src"
    cdir.mkdir(parents=True)
    (cdir / "lib.rs").write_text("fn x() {}\n", encoding="utf-8")
    out = _merger(tmp_path).merge_cluster_outputs(
        tmp_path, {"base": None}, [{"cluster": "base"}])
    assert out["files_written"] == 1
    assert out["files_passthrough"] == 1
    assert out["files_ast_merged"] == 0
    assert out["items_dropped"] == 0


def test_copy_fallback_when_extractor_missing(tmp_path):
    # One cluster with a source file; extractor missing → verbatim copy.
    cdir = tmp_path / ".skeleton-filled" / "base" / "src"
    cdir.mkdir(parents=True)
    (cdir / "lib.rs").write_text("fn x() {}\n", encoding="utf-8")
    m = _merger(tmp_path)
    out = m.merge_cluster_outputs(tmp_path, {"base": None}, [{"cluster": "base"}])
    assert out["files_written"] == 1
    assert (tmp_path / "src" / "lib.rs").read_text() == "fn x() {}\n"
