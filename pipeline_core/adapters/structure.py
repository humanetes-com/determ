"""CliStructureReader (P1) — the real StructureReader over the Extractor seam.

Holds the `.to_dict()`-wrapping that used to live in pipeline.py's `_enumerate_*_via_extractor`
shims (anti-pattern A: logic in the god-file behind a monkeypatch seam). The composition root
constructs it once from the `Extractor` adapter and injects it; tests inject a fake.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.cargo_toml import cargo_default_features
from pipeline_core.domain.models import (
    BuildCluster,
    ConstRecord,
    DispatchFamily,
    ModRecord,
    TypeRecord,
    UseRecord,
)
from pipeline_core.domain.rust_idents import fn_reference_counts
from pipeline_core.domain.rust_idioms import no_std_invariant_note
from pipeline_core.domain.rust_macros import (
    collect_crate_macros as _collect_crate_macros_pure,
)
from pipeline_core.ports import Extractor

from .std_types import (
    ExternalTypeSource,
    StdTypeSource,
    crate_root_of,
    external_type_records,
    external_type_records_enabled,
)


def collect_crate_macros(src_dir: Path, max_chars: int) -> str:
    """Read the crate's .rs files and render its macro_rules! section (pure
    extraction in domain.rust_macros.collect_crate_macros; this shim does the I/O)."""
    files: list[tuple[str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            files.append((p.name, p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return _collect_crate_macros_pure(files, max_chars)


def detect_crate_no_std_invariant(src_dir: Path) -> str:
    """Read the crate root's head and return the no_std invariant note (pure
    decision in domain.rust_idioms.no_std_invariant_note; this shim does the I/O)."""
    lib_rs = src_dir / "src" / "lib.rs" if (src_dir / "src").exists() else src_dir / "lib.rs"
    if not lib_rs.exists():
        return ""
    try:
        head = lib_rs.read_text(encoding="utf-8")[:4000]
    except Exception:
        return ""
    return no_std_invariant_note(head)


def fn_name_counts(src_dir: Path) -> dict[str, int]:
    """`{fn_name: ref_count}` across every .rs under `src_dir` (word-boundary
    reference count, regex-free). Pass a dir holding real source (the live
    project_dir/src BEFORE strip) — the result is serialised so Phase C never
    re-reads source after strip."""
    if not src_dir.exists():
        return {}
    parts: list[str] = []
    for p in src_dir.rglob("*.rs"):
        try:
            parts.append(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    text = "\n".join(parts)
    return fn_reference_counts(text) if text else {}


def source_hash(
    project_dir: Path, *,
    is_gen_tests_sibling_name: Callable[[str], bool],
    strip_gen_tests_decls: Callable[[str], str],
) -> str:
    """16-hex hash of Cargo.toml + src/*.rs (gen_tests artifacts stripped) +
    test-file heads. Shared across phases — if source changes, all phase hashes
    go stale. The gen_tests helpers are injected (pipeline-resident)."""
    parts: list[str] = []
    cargo = project_dir / "Cargo.toml"
    if cargo.exists():
        parts.append(cargo.read_text(errors="replace"))
    src = project_dir / "src"
    for p in sorted(src.glob("*.rs")) if src.exists() else []:
        if is_gen_tests_sibling_name(p.name):
            continue
        with suppress(Exception):
            parts.append(strip_gen_tests_decls(p.read_text(errors="replace")))
    for p in sorted(project_dir.rglob("*.rs")):
        rel = str(p.relative_to(project_dir))
        if "tests/" in rel or rel.startswith("tests"):
            with suppress(Exception):
                parts.append(p.read_text(errors="replace")[:500])
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:16]


def load_baked_clusters(project_dir: Path) -> list[BuildCluster]:
    """Rehydrate clusters from `.skeleton/_clusters.json` (written pre-strip by
    the bake step). Returns [] if absent. Replaces every post-strip
    `task0_cfg_scan` call — those need the source tree, which is gone."""
    path = project_dir / ".skeleton" / "_clusters.json"
    if not path.exists():
        return []
    # The crate's own default-on cargo features, read from the manifest rather
    # than the manifest-time cluster record: they are a property of the CRATE,
    # not of the cluster plan, and reading them here means the corrected cfg
    # world reaches an already-baked tree without a re-bake (the bake is
    # frozen once `_clusters.json` is written). Absent/unparsable manifest is
    # the empty set — the pre-fix behaviour.
    defaults: list[str] = []
    manifest = project_dir / "Cargo.toml"
    if manifest.exists():
        try:
            defaults = cargo_default_features(
                manifest.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            defaults = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[BuildCluster] = []
    for c in data.get("clusters", []):
        bc = BuildCluster(
            name=c.get("name", ""),
            env_extra=dict(c.get("env_extra", {}) or {}),
            tarpaulin_exclude_files=list(c.get("tarpaulin_exclude_files", []) or []),
            excluded_cfg=list(c.get("excluded_cfg", []) or []),
            active_cfg=list(c.get("active_cfg", []) or []),
            prompt_nightly_rule=c.get("prompt_nightly_rule", "") or "",
            is_default=bool(c.get("is_default", False)),
            default_features=list(defaults),
        )
        # cfg_true / cfg_false / toolchain are read-only @properties derived from
        # active_cfg / excluded_cfg / requires_nightly — written into the manifest
        # for readability but must not be assigned back onto the cluster.
        if c.get("requires_nightly"):
            bc.requires_nightly = True
        out.append(bc)
    return out


@dataclass(frozen=True, slots=True)
class CliStructureReader:
    """Structure reads via the syn extractor. Satisfies ports.structure.StructureReader.

    `enumerate_types` is the one composite read: the crate's own records
    (the `Extractor` seam, one subcommand per method, untouched) followed by
    records for the standard-library types the crate's `use` lines bind,
    fetched from `rust-src` through `std_types` (default: a `StdTypeSource`
    over the same extractor). This is the seam every fill route reaches —
    the composition root's `enumerate_types` closure is `structure
    .enumerate_types` — so a prompt's TYPES_IN_SCOPE can render
    `std::backtrace::Backtrace` without a builder change, while the
    pre-strip API-surface capture, which calls the extractor directly, keeps
    seeing the crate alone."""

    extractor: Extractor
    std_types: ExternalTypeSource | None = None

    def __post_init__(self) -> None:
        if self.std_types is None:
            object.__setattr__(self, "std_types", StdTypeSource(self.extractor))

    def enumerate_fns(self, src_dir: Path) -> list[dict[str, Any]] | None:
        """Fn records as wire dicts (the per-fn/per-impl engine contract); None if the
        extractor binary is unavailable. The seam returns typed FnRecords (slice 6); we
        serialise back via ``to_dict`` so the engine stays byte-identical. Each dict:
        file, fn_name, sig_text, body_text, sig_byte_start, body_byte_start,
        body_byte_end, cfg_predicates (list[str]), is_pub (bool), container
        ("" free fn | "impl ..." | "trait ..."), has_unimplemented_body (bool)."""
        recs = self.extractor.enumerate_fns(src_dir)
        return None if recs is None else [r.to_dict() for r in recs]

    def enumerate_types(self, src_dir: Path) -> list[TypeRecord] | None:
        recs = self.extractor.enumerate_types(src_dir)
        if recs is None or self.std_types is None or not external_type_records_enabled():
            return recs
        if crate_root_of(src_dir) is None:
            return recs  # not a cargo package: nothing binds `std` here
        # `None` = a stale binary that cannot report `use` bindings; the crate's
        # own records still answer, so return them rather than nothing.
        uses = self.extractor.enumerate_uses(src_dir)
        if uses is None:
            return recs
        return [*recs, *external_type_records(src_dir, recs, uses, self.std_types)]

    def enumerate_mods(self, src_dir: Path) -> list[ModRecord] | None:
        return self.extractor.enumerate_mods(src_dir)

    def enumerate_uses(self, src_dir: Path) -> list[UseRecord] | None:
        return self.extractor.enumerate_uses(src_dir)

    def enumerate_consts(self, src_dir: Path) -> list[ConstRecord] | None:
        return self.extractor.enumerate_consts(src_dir)

    def enumerate_dispatch_families(self, src_dir: Path) -> list[DispatchFamily] | None:
        return self.extractor.enumerate_dispatch_families(src_dir)

    def parse_ok(self, path: Path) -> bool:
        """Syn parse-check one file via the extractor (`--parse-check`). The F1
        splice gate's validator — fail-open (see Extractor.parse_check)."""
        return self.extractor.parse_check(path)


def crate_entry_point(cargo_toml_path: Path, module_dir_rel: str,
                      file_path_set: set[str]) -> str | None:
    """Return the crate entry point path (relative to project root) from Cargo.toml."""
    from pipeline_core.domain.cargo_toml import cargo_entry_point_paths

    candidates = []
    if cargo_toml_path.exists():
        text = cargo_toml_path.read_text(encoding="utf-8")
        for rel in cargo_entry_point_paths(text):
            candidates.append(str(Path(module_dir_rel) / rel) if module_dir_rel else rel)
    for default in ("src/lib.rs", "src/main.rs"):
        candidates.append(str(Path(module_dir_rel) / default) if module_dir_rel else default)
    return next((c for c in candidates if c in file_path_set), None)

