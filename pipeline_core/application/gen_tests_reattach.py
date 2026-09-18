"""Reattach saved gen_tests siblings to TASK 2's regenerated source.

Extracted verbatim from `main()`'s reconstruction loop. After TASK 2 rewrites a
module's source files, the unit tests preserved through strip must be
re-stitched back on: each saved `(pass_n, body)` becomes a sibling file plus a
`#[cfg(test)] … mod gen_tests_N;` path-decl on the regenerated source. Per-cluster
filled skeletons keep a test's content and its cfg gate together; the
single-tree path is the non-cluster fallback. Compile failures of reattached
siblings are reconstruction signal (left in place for Phase C), not gen_test bugs.

Collaborators stay in pipeline.py and are injected via `ReattachDeps` (the
per-block-Deps pattern — NOT a loop-wide god-object).
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.generation import strip_gen_tests_decl
from pipeline_core.domain.models import BuildCluster


@dataclass(frozen=True)
class ReattachDeps:
    log: Callable[[str], None]
    emit: Callable[..., None]
    derive_gentest_prelude: Callable[[Path], tuple[str, str]]
    is_gen_tests_file: Callable[[str], bool]
    gen_tests_siblings: Callable[[Path], list[tuple[int, Path]]]
    load_gen_tests_origin: Callable[[Path], dict[str, str]]
    gen_tests_path: Callable[[Path, int], Path]
    build_gen_tests_decl: Callable[..., str]
    cluster_cfg_attr: Callable[[BuildCluster | None], str]
    has_gen_tests_decl: Callable[[str, int], bool]
    cargo_env: Callable[[], dict[str, str]]


def reattach_gen_tests(
    project_dir: Path,
    module_name: str,
    *,
    clusters: list[BuildCluster],
    saved_gen_blocks: dict[str, list[tuple[int, str]]],
    existing_test_files: set[str],
    deps: ReattachDeps,
) -> None:
    """Reattach preserved gen_tests siblings + decls onto regenerated source,
    then validate them against the tree (compile failures left in place unless
    GEN_TESTS_REATTACH_QUARANTINE=1). Mutates the workspace in place."""
    d = deps
    # Reattach saved gen_tests siblings to generated source files.
    # Idempotent: for each saved (pass_n, body) we ensure
    #   (a) the sibling file exists with the saved body, and
    #   (b) the source file contains a path-mod decl for it.
    # The previous guard ("skip if any sibling already on disk")
    # was a no-op once strip started preserving sibling files,
    # leaving them orphaned by the regenerated source.
    _reattached: list[tuple[Path, Path, int]] = []
    # Source gen_tests PER-CLUSTER so a test's content and
    # its cfg gate never separate. A gen_test generated under one
    # cluster (e.g. nightly's `request_ref`) must be gated to THAT
    # cluster and excluded from others. Emit ONE ungated sibling only
    # when the identical test is present in EVERY cluster; otherwise
    # emit one gated sibling per present cluster with a cluster-unique
    # name (so neither the file content nor the decl collapses in the
    # merge, and a single-cluster test stays fenced to its cluster).
    _filled_root = project_dir / ".skeleton-filled"
    _per_cluster_filled = [
        c for c in clusters
        if (_filled_root / c.name).exists()
    ]
    if _per_cluster_filled:
        _reattach_allow_line, _ = d.derive_gentest_prelude(project_dir)
        # (rel, n) -> {cluster_name: body}
        _gt: dict[tuple[str, int], dict[str, str]] = {}
        _cl_by_name = {c.name: c for c in _per_cluster_filled}
        for _c in _per_cluster_filled:
            _cdir = _filled_root / _c.name
            for _sf in sorted(_cdir.rglob("*.rs")):
                if d.is_gen_tests_file(_sf.name):  # structured parser, was regex
                    continue
                _rel = str(_sf.relative_to(_cdir))
                if _rel in existing_test_files:
                    continue
                for _n, _p in d.gen_tests_siblings(_sf):
                    try:
                        _gt.setdefault((_rel, _n), {})[_c.name] = \
                            _p.read_text(encoding="utf-8")
                    except OSError:
                        continue
        _appends: dict[str, list[str]] = {}
        _all_cluster_names = {c.name for c in clusters}
        _gt_origin = d.load_gen_tests_origin(project_dir)
        for (_rel, _n), _bodies in sorted(_gt.items()):
            _tgt = project_dir / _rel
            if not _tgt.exists():
                continue
            _origin_gate = _gt_origin.get(f"{_tgt.stem}.gen_tests_{_n}")
            if _origin_gate is not None:
                # Origin-cfg path (deterministic): one sibling gated by
                # the cluster that GENERATED the test ('' = default/stable
                # → ungated). A nightly-only test carries
                # `#[cfg(error_generic_member_access)]` → excluded from
                # stable/no_core, compiled in nightly. Bodies are copied
                # verbatim across clusters, so any is representative.
                _body = next(iter(_bodies.values()))
                _sib = d.gen_tests_path(_tgt, _n)
                _sib.write_text(_body, encoding="utf-8")
                _appends.setdefault(_rel, []).append(
                    d.build_gen_tests_decl(
                        _sib.name, _n, _reattach_allow_line, _origin_gate))
                _reattached.append((_tgt, _sib, _n))
            elif (len(set(_bodies.values())) == 1
                    and set(_bodies) == _all_cluster_names):
                # Fallback (no origin manifest): present in EVERY cluster
                # AND identical → safe ungated. A body found in only a
                # subset (e.g. nightly-only `request_ref`) must NOT land
                # here — it would compile under the other clusters' test
                # build and break them.
                _body = next(iter(_bodies.values()))
                _sib = d.gen_tests_path(_tgt, _n)
                _sib.write_text(_body, encoding="utf-8")
                _appends.setdefault(_rel, []).append(
                    d.build_gen_tests_decl(_sib.name, _n, _reattach_allow_line))
                _reattached.append((_tgt, _sib, _n))
            else:
                # Fallback: divergent → per-cluster gated siblings, unique names
                for _cname, _body in sorted(_bodies.items()):
                    _gate = d.cluster_cfg_attr(_cl_by_name.get(_cname))
                    _sib = _tgt.with_name(
                        f"{_tgt.stem}.gen_tests_{_n}_{_cname}.rs")
                    _sib.write_text(_body, encoding="utf-8")
                    _appends.setdefault(_rel, []).append(
                        d.build_gen_tests_decl(
                            _sib.name, f"{_n}_{_cname}",
                            _reattach_allow_line, _gate))
                    _reattached.append((_tgt, _sib, _n))
        for _rel, _decls in _appends.items():
            _tgt = project_dir / _rel
            _txt = _tgt.read_text(encoding="utf-8").rstrip()
            _added = 0
            for _d in _decls:
                _path_line = _d.split("#[path = ")[-1].split("]")[0]
                if _path_line not in _txt:
                    _txt += _d
                    _added += 1
            if _added:
                _tgt.write_text(_txt, encoding="utf-8")
                d.log(f"  Reattached {_added} per-cluster gen_tests "
                      f"decl(s) to {_rel}.")
    elif saved_gen_blocks:
        # Non-cluster fallback (single tree): original behaviour.
        _reattach_allow_line, _ = d.derive_gentest_prelude(project_dir)
        for rel, blocks in saved_gen_blocks.items():
            src_path = project_dir / rel
            if not src_path.exists():
                continue
            content = src_path.read_text(encoding="utf-8")
            new_text = content.rstrip()
            added_decls = 0
            for pass_n, body in blocks:
                sibling = d.gen_tests_path(src_path, pass_n)
                if not sibling.exists() or sibling.read_text(encoding="utf-8") != body:
                    sibling.write_text(body, encoding="utf-8")
                if not d.has_gen_tests_decl(new_text, pass_n):
                    new_text += d.build_gen_tests_decl(
                        sibling.name, pass_n, _reattach_allow_line
                    )
                    added_decls += 1
                _reattached.append((src_path, sibling, pass_n))
            if added_decls > 0:
                src_path.write_text(new_text, encoding="utf-8")
                d.log(f"  Reattached {added_decls} gen_tests decl(s) to {rel}.")
    if _reattached:

        # Validate reattached gen_tests against the regenerated
        # source. Compile failures are signal that the
        # reconstruction is wrong, NOT that the gen_test is
        # broken (gen_tests were validated at task0c time
        # against the original). Log the names but leave them
        # in place — Phase C / TDD picks up compile errors via
        # the compile-frontier extractor and drives fills.
        # Opt back into permanent quarantine with
        # GEN_TESTS_REATTACH_QUARANTINE=1.
        if _reattached:
            _vchk: subprocess.CompletedProcess[str] | None
            try:
                _vchk = subprocess.run(
                    ["cargo", "check", "--tests"],
                    cwd=project_dir, capture_output=True,
                    text=True, timeout=300, env=d.cargo_env(),
                )
            except Exception as _e:
                d.log(f"  [reattach] validate cargo check failed: {_e}")
                _vchk = None
            _legacy_quarantine = os.getenv(
                "GEN_TESTS_REATTACH_QUARANTINE", "0",
            ) == "1"
            if _vchk is not None and _vchk.returncode != 0:
                _out = (_vchk.stdout or "") + (_vchk.stderr or "")
                _failing = [
                    (sp, sb, pn) for sp, sb, pn in _reattached
                    if sb.name in _out
                ]
                if _failing and not _legacy_quarantine:
                    _names = ", ".join(sb.name for _, sb, _ in _failing)
                    d.log(f"  [reattach] {len(_failing)} gen_tests sibling(s) "
                          f"fail compile against regenerated source: {_names}. "
                          "Leaving in place so Phase C compile-frontier can act.")
                    d.emit("gen_tests.reattach_compile_fail", data={
                        "module": module_name,
                        "count": len(_failing),
                        "files": [sb.name for _, sb, _ in _failing],
                    })
                elif _failing and _legacy_quarantine:
                    quarantined = 0
                    for src_path, sibling, pass_n in _failing:
                        try:
                            ptext = src_path.read_text(encoding="utf-8")
                            # Structured decl-strip (regex-free).
                            ptext_new = strip_gen_tests_decl(ptext, sibling.name, pass_n)
                            if ptext_new != ptext:
                                src_path.write_text(ptext_new, encoding="utf-8")
                        except Exception:
                            pass
                        try:
                            sibling.rename(sibling.with_suffix(".rs.broken"))
                        except Exception:
                            with suppress(Exception):
                                sibling.unlink()
                        quarantined += 1
                    if quarantined:
                        d.log(f"  [reattach] quarantined {quarantined} "
                              "gen_tests sibling(s) (legacy mode).")
                        d.emit("gen_tests.reattach_quarantine", data={
                            "module": module_name,
                            "quarantined": quarantined,
                        })
