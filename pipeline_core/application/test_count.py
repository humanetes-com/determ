"""Cluster test-count orchestration (debt #29 extraction).

`count_active_tests_for_cluster` is the FR-024 strict-verdict helper: how many
`#[test]` fns SHOULD run if cargo compiles every test source active under the
cluster's cfg. It walks `tests/`, the `gen_tests_*` siblings, and `src/`,
cfg-filtering each file so cargo-cannot-compile tests don't inflate the expected
count (an UNDERcount would manufacture a fraudulent TASK-3 SUCCESS — FR-022).

The impure collaborators (syn extractor for fn records + mod records, the
per-file cfg-predicate lookup) are injected via :class:`TestCountDeps`; the pure
counting lives in :mod:`pipeline_core.domain.test_count`. Behaviour is
byte-identical to the former `pipeline._count_active_tests_for_cluster` /
`_file_active_for_cluster`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.cargo_toml import cargo_default_features
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.doc_tests import count_doc_tests
from pipeline_core.domain.generation import gen_tests_decl_blocks
from pipeline_core.domain.rust_scan import mod_decls
from pipeline_core.domain.test_count import (
    count_test_fns_cfg_aware,
    count_test_fns_in_records,
    file_top_cfg_excluded,
    test_fn_names_in_records,
)


@dataclass(frozen=True, slots=True)
class TestCountDeps:
    """Impure collaborators the cluster test-count orchestration needs."""

    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None]
    mod_records: Callable[[Path], list[Any] | None]
    file_cfg_predicate: Callable[[Path, str], str | None]
    is_gen_tests_file: Callable[[str], bool]


def file_active_for_cluster(
    src_dir: Path,
    file_path: Path,
    cfg_true: set[str],
    cfg_false: set[str],
    deps: TestCountDeps,
    _depth: int = 0,
    default_features: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """Whether `file_path` is COMPILED under the cluster cfg: evaluate every cfg
    predicate along its mod-declaration chain up to the crate root, via the syn
    extractor's ModRecords. A decidably-False chain never compiles.

    Undecidable predicates and unmatched files count as ACTIVE — may
    under-exclude but never over-exclude (the strict verdict must not silently
    shrink the oracle, FR-018)."""
    stem = file_path.stem
    if stem in ("lib", "main") or _depth > 32:
        return True
    cs = CfgSet.of(cfg_true, cfg_false, default_features)
    recs = deps.mod_records(src_dir)
    if recs is None:
        # Extractor unavailable: single-level gate (AST-less environments).
        pred = deps.file_cfg_predicate(src_dir, file_path.name)
        return pred is None or not cs.excludes_for_cargo(pred)
    matches = [r for r in recs if r.name == stem or r.file == file_path.name]
    if not matches:
        return True
    # Dual-form decls (`#[cfg(X)] mod m;` / `#[cfg(not(X))] mod m;`): the file
    # is active if ANY declaration chain is active.
    for rec in matches:
        if any(cs.excludes_for_cargo(p) for p in rec.cfg_predicates):
            continue
        if file_active_for_cluster(src_dir, Path(rec.declared_in),
                                   cfg_true, cfg_false, deps,
                                   _depth + 1, default_features):
            return True
    return False


def count_active_tests_for_cluster(
    project_dir: Path,
    cluster: Any,
    deps: TestCountDeps,
) -> int:
    """FR-024 strict-verdict helper — see module docstring. Counts three
    sources (tests/, gen_tests siblings, src/), all cfg-filtered against the
    cluster's (cfg_true, cfg_false) sets. Returns the cfg-aware total."""
    return active_test_inventory_for_cluster(project_dir, cluster, deps)[0]


def active_test_inventory_for_cluster(
    project_dir: Path,
    cluster: Any,
    deps: TestCountDeps,
    *,
    tests_dir_stems: frozenset[str] | None = None,
    include_lib_sources: bool = False,
) -> tuple[int, frozenset[str]]:
    """(expected_count, expected_names) over the same walk as
    `count_active_tests_for_cluster`. Names come from the typed-record path
    only — a file counted via the line-walker fallback contributes to the
    count but not the names, so the name set may be a SUBSET of the count
    (safe: reconciliation treats missing names as a count deficit, never
    invents vanished tests).

    `tests_dir_stems`: when given, the `tests/` walk covers ONLY files with
    those stems — the integration binaries a `cargo test --test stem…` sweep
    names. None keeps the full FR-024 `tests/` walk.

    `include_lib_sources`: also count the crate unit-test binary (gen_tests
    siblings + src-side `#[cfg(test)]`) — the denominator of a sweep that adds
    `--lib`. It composes with `tests_dir_stems`: a `--lib` sweep names its
    `tests/` stems AND runs the lib binary, so its oracle is the stem-scoped
    `tests/` walk PLUS the lib sources. (With `tests_dir_stems=None` the lib
    sources are already included — the full FR-024 walk — so the flag only
    matters when `tests/` is stem-scoped.)"""
    if cluster is None:
        cfg_true: set[str] = set()
        cfg_false: set[str] = set()
    else:
        cfg_true = set(getattr(cluster, "cfg_true", []) or [])
        cfg_false = set(getattr(cluster, "cfg_false", []) or [])

    # Crate default features — a `feature = "X"` that IS a default is ON even
    # when the cluster sets don't list it, so it must NOT be force-excluded
    # (FR-018).
    cargo_toml = project_dir / "Cargo.toml"
    default_features: frozenset[str] = frozenset()
    if cargo_toml.exists():
        try:
            default_features = frozenset(cargo_default_features(
                cargo_toml.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            default_features = frozenset()

    # Typed-record path (FR-024 fraud-safety): count test fns off the syn
    # extractor's FnRecords (is_test + accumulated cfg_predicates) rather than a
    # line-level marker scan. Enumerated ONCE over the whole crate, indexed by
    # crate-relative path. A file absent from the index (extractor unavailable,
    # parse failure, or no fns) falls back to the marker scan — conservative:
    # never undercount (FR-022), and cargo can't run a file that doesn't parse
    # anyway, so an over-count merely keeps the cluster 'incomplete'.
    fn_recs = deps.enumerate_fns(project_dir)
    by_file: dict[str, list[dict[str, Any]]] | None = None
    if fn_recs is not None:
        by_file = {}
        for r in fn_recs:
            by_file.setdefault(r.get("file", ""), []).append(r)

    def _count_file(rel: str, text: str) -> tuple[int, frozenset[str]]:
        # File-level inner `#![cfg(P)]` gates the whole compilation unit and is
        # not carried on fn records — apply it here (both paths agree below).
        if file_top_cfg_excluded(text, cfg_true, cfg_false):
            return 0, frozenset()
        if by_file is not None and rel in by_file:
            return (count_test_fns_in_records(
                        by_file[rel], cfg_true, cfg_false, default_features),
                    test_fn_names_in_records(
                        by_file[rel], cfg_true, cfg_false, default_features))
        return (count_test_fns_cfg_aware(
                    text, cfg_true, cfg_false, default_features),
                frozenset())

    total = 0
    names: set[str] = set()

    def _add(rel: str, text: str) -> None:
        nonlocal total
        n, nm = _count_file(rel, text)
        total += n
        names.update(nm)
    # 1. tests/**/*.rs — cfg-filtered per-test.
    tests_dir = project_dir / "tests"
    if tests_dir.is_dir():
        # A subdirectory module (tests/common/mod.rs) has no binary of its
        # own — it compiles INTO every root-level binary that declares its
        # top directory (`mod common;`). With a stem filter active, such
        # files count iff a SELECTED root binary includes them; skipping
        # them wholesale undercounted their #[test] fns (missed vanishes).
        included_dirs: set[str] = set()
        if tests_dir_stems is not None:
            for root_f in tests_dir.glob("*.rs"):
                if root_f.stem not in tests_dir_stems:
                    continue
                try:
                    rtext = root_f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                included_dirs.update(mod_decls(rtext))
        for tp in tests_dir.rglob("*.rs"):
            rel_parts = tp.relative_to(tests_dir).parts
            if tests_dir_stems is not None:
                in_root = len(rel_parts) == 1
                if in_root and tp.stem not in tests_dir_stems:
                    continue
                if not in_root and rel_parts[0] not in included_dirs:
                    continue
            try:
                text = tp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            _add(tp.relative_to(project_dir).as_posix(), text)

    if tests_dir_stems is not None and not include_lib_sources:
        return total, frozenset(names)

    src_dir = project_dir / "src"
    if not src_dir.is_dir():
        return total, frozenset(names)

    cs = CfgSet.of(cfg_true, cfg_false)
    # 2. gen_tests siblings — gated by their parent #[path] decl (R7: decl scan).
    declared_sibs: set[str] = set()
    for parent in src_dir.glob("*.rs"):
        if deps.is_gen_tests_file(parent.name):
            continue
        # A parent file that never compiles under this cluster (mod-decl chain
        # cfg'd out) takes its gen_tests siblings with it.
        if not file_active_for_cluster(src_dir, parent, cfg_true, cfg_false,
                                       deps, 0, default_features):
            continue
        try:
            ptext = parent.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for decl in gen_tests_decl_blocks(ptext):
            gate = decl.gate.strip()
            sib_name = decl.sibling_name or ""
            declared_sibs.add(sib_name)   # declared — even if gated out below
            if gate and cs.evaluate(gate) is False:
                continue
            sib = src_dir / sib_name
            if not sib.exists():
                continue
            try:
                stext = sib.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            _add(sib.relative_to(project_dir).as_posix(), stext)

    # 2b. UNDECLARED siblings on disk still count toward expected. A sibling
    # whose parent lost its `mod gen_tests_N;` decl is a HIDDEN test file —
    # its tests silently stop compiling. Counting it keeps the strict verdict
    # 'incomplete' instead of letting the expected count shrink in the same
    # stroke that hid the tests (never undercount the oracle). Siblings
    # DECLARED somewhere are exempt — a cluster-gated decl (evaluated False
    # above) is deliberate exclusion, not hiding.
    for sib in src_dir.glob("*.rs"):
        if not deps.is_gen_tests_file(sib.name) or sib.name in declared_sibs:
            continue
        parent_stem = sib.name.split(".gen_tests_", 1)[0]
        parent = src_dir / f"{parent_stem}.rs"
        # Parent cfg'd out of this cluster → the sibling could never compile
        # here even when declared; a MISSING parent stays counted (that is a
        # corruption to surface, not a reason to shrink the count).
        if parent.exists() and not file_active_for_cluster(
                src_dir, parent, cfg_true, cfg_false, deps, 0, default_features):
            continue
        try:
            stext = sib.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        _add(sib.relative_to(project_dir).as_posix(), stext)

    # 3. src/**/*.rs (excluding gen_tests siblings) — inline test mods, and the
    #    DOC tests, which have no `#[test]` fn and therefore no record. cargo
    #    runs them as their own binary and counts them in the same total the
    #    strict verdict compares against: on the reconstructed anyhow tree they
    #    are 25 of the 149 `stable` announces, and their absence here is the
    #    whole of the "expected undershoots by exactly 25 per cluster" gap.
    #    Counted, never NAMED — rustdoc identifies them as `src/lib.rs - Chain
    #    (line 284)`, which is not a `#[test]` name and reconciles against
    #    nothing. `names` is documented as a possible subset of the count.
    for srf in src_dir.rglob("*.rs"):
        if deps.is_gen_tests_file(srf.name):
            continue
        if not file_active_for_cluster(src_dir, srf, cfg_true, cfg_false,
                                       deps, 0, default_features):
            continue
        try:
            stext = srf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        _add(srf.relative_to(project_dir).as_posix(), stext)
        total += count_doc_tests(stext)
    return total, frozenset(names)
