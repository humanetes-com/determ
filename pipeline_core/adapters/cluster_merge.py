"""Cross-cluster skeleton merge.

Composes the per-cluster filled source dirs (`.skeleton-filled/{cluster}/src/`)
into the project's `src/`, gating divergent items behind their cluster's cfg.
The merge itself is AST-based: the syn extractor's `--merge-clusters` subcommand
parses each cluster's version of a file and emits identical items once, divergent
items as cfg-gated copies at their natural position (recursively, respecting
nested trait/impl bodies — which the old regex splicer could not).

Lifted verbatim out of the pipeline.py god-file. The pure cfg-gate construction
lives in :func:`pipeline_core.domain.cfg.cluster_gate_predicate`; this adapter is
the I/O boundary (extractor subprocess + file walking), composition-injected with
the extractor binary path and a log sink.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.cfg import cluster_gate_predicate
from ..domain.rust_scan import count_cfg_attrs


@dataclass(frozen=True, slots=True)
class MergedFile:
    """What the extractor did to one file. `ast_merged` False means the
    passthrough fallback ran — a verbatim copy of the base cluster with no
    divergence handling at all, which used to be counted in `files_written`
    exactly like a real merge and so left a fully degraded run
    indistinguishable from a clean one."""

    ast_merged: bool
    items_in_max: int = 0
    items_out: int = 0
    preferred: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClusterMerger:
    """AST-based cross-cluster merge with its I/O collaborators injected."""

    extractor_bin: Path
    log: Callable[[str], None]

    def _ast_merge_files(self, out_path: Path,
                         cluster_files: list[tuple[str, Path, str]],
                         prefer_cluster: str | None) -> MergedFile | None:
        """Invoke the syn merger on a single file. `cluster_files` is a list of
        (cluster_name, source_path, cfg_predicate). Writes to `out_path` and
        returns what the merge did, or None when it could not run."""
        if not self.extractor_bin.exists():
            return None
        cmd: list[str] = [
            str(self.extractor_bin),
            "--merge-clusters",
            "--merge-output", str(out_path),
        ]
        if prefer_cluster:
            cmd += ["--prefer-cluster", prefer_cluster]
        for name, src_path, cfg_pred in cluster_files:
            cmd += ["--cluster", f"{name}:{src_path}:{cfg_pred}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as e:
            self.log(f"  [skeleton-merge] extractor invocation failed: {e}")
            return None
        if r.returncode != 0:
            self.log(f"  [skeleton-merge] extractor returned rc={r.returncode}: "
                     f"{(r.stderr or '').strip()[:240]}")
            return None
        try:
            counts = json.loads((r.stdout or "").strip() or "{}")
        except ValueError:
            counts = {}
        return MergedFile(
            ast_merged=True,
            items_in_max=int(counts.get("items_in_max", 0)),
            items_out=int(counts.get("items_out", 0)),
            preferred=tuple(counts.get("preferred", ())),
        )

    def merge_cluster_outputs(self, project_dir: Path, cluster_specs: dict[str, Any],
                              successes: list[dict[str, Any]],
                              prefer_cluster: str | None = None) -> dict[str, Any]:
        """Compose per-cluster filled source dirs into `project_dir/src/` with
        cfg-gated divergence.

        `prefer_cluster` names the cluster whose body wins when the clusters
        diverge on an item every one of them compiles; see the extractor's
        `--prefer-cluster`. The evidence for that choice is the per-cluster
        sweep record and so belongs to the caller, not here.

        The returned stats are what a later reader has to reconstruct the run
        from. They used to be `files_written` and an approximate gate count,
        and `files_written` counted a real AST merge and the passthrough
        fallback identically — so a run where the extractor binary was missing
        and every file degraded to a verbatim copy reported the same figure as
        a clean merge. `files_ast_merged` / `files_passthrough` separate them,
        and `items_dropped` is the invariant: an item every cluster carries is
        counted once per cluster going in and once coming out, so a positive
        number means the merge lost something."""
        cluster_dirs: dict[str, Path] = {}
        for s in successes:
            cname = s["cluster"]
            d = project_dir / ".skeleton-filled" / cname
            if d.exists():
                cluster_dirs[cname] = d
        if not cluster_dirs:
            return {"files_written": 0, "cfg_gated_fns": 0, "files_ast_merged": 0,
                    "files_passthrough": 0, "items_in_max": 0, "items_out": 0,
                    "items_dropped": 0, "preferred": [], "prefer_cluster": ""}

        # Collect every file path across all cluster outputs.
        all_paths: set[str] = set()
        for d in cluster_dirs.values():
            for p in (d / "src").rglob("*.rs"):
                all_paths.add(str(p.relative_to(d)))

        files_written = 0
        cfg_gated_fns = 0
        # Divergence counter matches the atoms the clusters actually gate on
        # (never a hardcoded crate-specific list).
        gate_atoms: tuple[str, ...] = tuple(sorted({
            atom
            for c in cluster_specs.values()
            for atom in ((getattr(c, "active_cfg", []) or [])
                         + (getattr(c, "excluded_cfg", []) or []))
        }))
        extractor_available = self.extractor_bin.exists()
        if not extractor_available:
            self.log("  [skeleton-merge] WARN: extractor binary missing; falling "
                     "back to base-cluster passthrough (no divergence handling).")

        files_ast_merged = 0
        files_passthrough = 0
        items_in_max = 0
        items_out = 0
        preferred: list[str] = []
        for rel in sorted(all_paths):
            if ".gen_tests_" in rel:
                continue
            # Gather (cluster_name, path, cfg_predicate) for every cluster that
            # has this file, in lexicographic order so the merge output is
            # deterministic. The base — the cluster the passthrough fallback
            # copies — is therefore `sorted()[0]`, which is whichever cluster
            # name sorts first and NOT necessarily a stable one: on `anyhow`
            # the names are `nightly` and `stable`, so the base is `nightly`.
            cluster_files: list[tuple[str, Path, str]] = []
            for cname in sorted(cluster_dirs.keys()):
                p = cluster_dirs[cname] / rel
                if not p.exists():
                    continue
                cluster_obj = cluster_specs.get(cname)
                cfg_pred = "" if cluster_obj is None else cluster_gate_predicate(
                    getattr(cluster_obj, "active_cfg", []) or [],
                    getattr(cluster_obj, "excluded_cfg", []) or [],
                )
                cluster_files.append((cname, p, cfg_pred))
            if not cluster_files:
                continue

            out_path = project_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)

            merged: MergedFile | None = None
            if extractor_available:
                merged = self._ast_merge_files(out_path, cluster_files, prefer_cluster)
            if merged is None:
                # Fallback: copy the base cluster's file verbatim. Loses
                # divergence handling but never corrupts output. Only hit when
                # the extractor binary is missing or fails on a specific file.
                base_path = cluster_files[0][1]
                try:
                    out_path.write_text(base_path.read_text(encoding="utf-8"),
                                         encoding="utf-8")
                except Exception as e:
                    self.log(f"  [skeleton-merge] copy fallback failed for {rel}: {e}")
                    continue
                files_passthrough += 1
            else:
                files_ast_merged += 1
                items_in_max += merged.items_in_max
                items_out += merged.items_out
                preferred.extend(f"{rel}::{p}" for p in merged.preferred)
                if merged.items_out < merged.items_in_max:
                    self.log(f"  [skeleton-merge] WARN: {rel} lost "
                             f"{merged.items_in_max - merged.items_out} item(s) "
                             f"({merged.items_in_max} in the richest input, "
                             f"{merged.items_out} out).")
            files_written += 1
            # Approximate cfg_gated_fns from #[cfg(...)] attributes in the
            # merged output matching our cluster gates (log line only).
            if gate_atoms:
                try:
                    text_out = out_path.read_text(encoding="utf-8")
                    cfg_gated_fns += count_cfg_attrs(text_out, gate_atoms)
                except Exception:
                    pass

        return {
            "files_written": files_written,
            "cfg_gated_fns": cfg_gated_fns,
            "files_ast_merged": files_ast_merged,
            "files_passthrough": files_passthrough,
            "items_in_max": items_in_max,
            "items_out": items_out,
            "items_dropped": max(0, items_in_max - items_out),
            "preferred": preferred,
            "prefer_cluster": prefer_cluster or "",
        }
