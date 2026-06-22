"""Cross-cluster skeleton merge (P6 engine-relocation series).

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

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.cfg import cluster_gate_predicate
from ..domain.rust_scan import count_cfg_attrs


@dataclass(frozen=True, slots=True)
class ClusterMerger:
    """AST-based cross-cluster merge with its I/O collaborators injected."""

    extractor_bin: Path
    log: Callable[[str], None]

    def _ast_merge_files(self, out_path: Path,
                         cluster_files: list[tuple[str, Path, str]]) -> bool:
        """Invoke the syn merger on a single file. `cluster_files` is a list of
        (cluster_name, source_path, cfg_predicate). Writes to `out_path`."""
        if not self.extractor_bin.exists():
            return False
        cmd: list[str] = [
            str(self.extractor_bin),
            "--merge-clusters",
            "--merge-output", str(out_path),
        ]
        for name, src_path, cfg_pred in cluster_files:
            cmd += ["--cluster", f"{name}:{src_path}:{cfg_pred}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as e:
            self.log(f"  [skeleton-merge] extractor invocation failed: {e}")
            return False
        if r.returncode != 0:
            self.log(f"  [skeleton-merge] extractor returned rc={r.returncode}: "
                     f"{(r.stderr or '').strip()[:240]}")
            return False
        return True

    def merge_cluster_outputs(self, project_dir: Path, cluster_specs: dict[str, Any],
                              successes: list[dict[str, Any]]) -> dict[str, int]:
        """Compose per-cluster filled source dirs into `project_dir/src/` with
        cfg-gated divergence. Returns {'files_written', 'cfg_gated_fns'} (the
        latter approximate — derived from a post-merge attribute count)."""
        cluster_dirs: dict[str, Path] = {}
        for s in successes:
            cname = s["cluster"]
            d = project_dir / ".skeleton-filled" / cname
            if d.exists():
                cluster_dirs[cname] = d
        if not cluster_dirs:
            return {"files_written": 0, "cfg_gated_fns": 0}

        # Collect every file path across all cluster outputs.
        all_paths: set[str] = set()
        for d in cluster_dirs.values():
            for p in (d / "src").rglob("*.rs"):
                all_paths.add(str(p.relative_to(d)))

        files_written = 0
        cfg_gated_fns = 0
        extractor_available = self.extractor_bin.exists()
        if not extractor_available:
            self.log("  [skeleton-merge] WARN: extractor binary missing; falling "
                     "back to base-cluster passthrough (no divergence handling).")

        for rel in sorted(all_paths):
            if ".gen_tests_" in rel:
                continue
            # Gather (cluster_name, path, cfg_predicate) for every cluster that
            # has this file. Lexicographic order means a stable cluster is base
            # when present, keeping merge output deterministic.
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

            merged_ok = False
            if extractor_available:
                merged_ok = self._ast_merge_files(out_path, cluster_files)
            if not merged_ok:
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
            files_written += 1
            # Approximate cfg_gated_fns from #[cfg(...)] attributes in the
            # merged output matching our cluster gates (log line only).
            try:
                text_out = out_path.read_text(encoding="utf-8")
                cfg_gated_fns += count_cfg_attrs(
                    text_out,
                    ("error_generic_member_access", "anyhow_no_core_error"),
                )
            except Exception:
                pass

        return {"files_written": files_written, "cfg_gated_fns": cfg_gated_fns}
