"""StdTypeSource — type records for standard-library types, from `rust-src`.

The I/O half of `domain/external_types.py`: locate the toolchain's library
sources (`rustc --print sysroot`, the `rust-src` component), copy the one
file a fully qualified path names into a scratch crate, run the extractor's
`--enumerate-types` / `--enumerate-uses` over it through the `Extractor` port
(the same binary the crate is enumerated with — never a second location),
follow a top-level re-export when the file does not define the name
(`std::error::Error` is `pub use core::error::Error;`), and cache the cleaned
records in memory and on disk under `<workspace>/.phase_c_logs/std_types/`,
keyed by toolchain + path, so a run pays the extractor once per type.

`external_type_records` is the wiring `CliStructureReader.enumerate_types`
calls: resolve what the crate's `use` lines bind from `std`/`core`/`alloc`,
fetch records for every binding a crate record does not already answer, and
append them after the crate's own records. Measured on this machine
(2026-09-02, rustc 1.95.0): `std/src/backtrace.rs` (18 KB) and
`core/src/fmt/mod.rs` (112 KB) each enumerate in under 20 ms.

Never raises out of the public surface: missing `rustc`, missing `rust-src`,
a path no candidate file defines, and an extractor failure each yield `[]`
and ONE log line per process per class.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from pipeline_core.domain.external_types import (
    EXTERNAL_ROOTS,
    MAX_REEXPORT_DEPTH,
    SYSROOT_LIBRARY_REL,
    annotate_external_record,
    canonical_external_record,
    cfg_complement,
    code_only,
    external_names_in,
    hop_names_for,
    leading_cfg_predicates,
    module_of_library_file,
    qualify,
    resolve_external_paths,
    resolve_use_path,
    sysroot_file_candidates,
    unstable_items,
)
from pipeline_core.domain.models import TypeRecord, UseRecord
from pipeline_core.domain.workspace_layout import (
    PHASE_C_LOGS_DIR,
    RECONSTRUCTION_DIRS,
    SKIP_DIR_NAMES,
)
from pipeline_core.log import log as _ambient_log
from pipeline_core.ports import Extractor

# The env switch: "0" turns the whole mechanism off (crate records only).
ENV_SWITCH = "EXTERNAL_TYPE_RECORDS"

# Directories whose sources are consumers of the crate, not the crate.
_NON_CRATE_DIRS = frozenset({"tests", "benches", "examples"})

_LOG_TAG = "[std-types]"


class _ExtractorUnavailable(Exception):
    """The extractor could not enumerate a library file — a fact about this
    process (binary missing, timeout), not about the path, so the empty
    answer is memoised for the process and never written to the disk cache."""


def external_type_records_enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip() != "0"


class ExternalTypeSource(Protocol):
    """What the structure reader needs from a type source; `StdTypeSource`
    is the real one, tests inject a canned one."""

    def records_for(self, path: str, *, cache_dir: Path | None = None) -> list[TypeRecord]: ...


class StdTypeSource:
    """Records for one fully qualified standard-library path, via the extractor.

    `records_for(path)` returns the type's own canonical record FIRST (name =
    the item's own name, `external_path` = `path`), then one hop of the
    `pub` siblings its public method signatures and public fields name
    (`BacktraceStatus` for `Backtrace`, `Request` for `core::error::Error`).
    `[]` when the path cannot be answered."""

    def __init__(
        self, extractor: Extractor, *,
        log: Callable[[str], None] = _ambient_log,
        sysroot: Path | None = None, toolchain: str | None = None,
    ) -> None:
        self._extractor = extractor
        self._log = log
        self._sysroot = sysroot
        self._toolchain = toolchain
        self._toolchain_probed = sysroot is not None and toolchain is not None
        self._memo: dict[str, list[TypeRecord]] = {}
        self._reported: set[str] = set()
        self._lock = threading.Lock()

    # ── public ────────────────────────────────────────────────────────────────
    def records_for(self, path: str, *, cache_dir: Path | None = None) -> list[TypeRecord]:
        with self._lock:
            hit = self._memo.get(path)
            if hit is not None:
                return list(hit)
            toolchain = self._toolchain_id()
            if toolchain is None:
                self._memo[path] = []
                return []
            cached = self._read_cache(cache_dir, toolchain, path)
            if cached is not None:
                self._memo[path] = cached
                return list(cached)
            try:
                recs = self._resolve(path)
            except _ExtractorUnavailable:
                self._memo[path] = []
                return []
            except Exception as e:  # the mechanism is advisory: never fail a fill
                self._note("resolve-error", f"{_LOG_TAG} {path}: {type(e).__name__}: {e} — "
                                            "external records skipped")
                self._memo[path] = []
                return []
            self._memo[path] = recs
            self._write_cache(cache_dir, toolchain, path, recs)
            return list(recs)

    # ── toolchain ─────────────────────────────────────────────────────────────
    def _toolchain_id(self) -> str | None:
        if not self._toolchain_probed:
            self._toolchain_probed = True
            self._sysroot, self._toolchain = _probe_rustc()
        if self._sysroot is None or self._toolchain is None:
            self._note("no-rustc", f"{_LOG_TAG} `rustc --print sysroot` unavailable — "
                                   "no external type records this run")
            return None
        if not (self._sysroot / SYSROOT_LIBRARY_REL).is_dir():
            self._note("no-rust-src", f"{_LOG_TAG} {self._sysroot / SYSROOT_LIBRARY_REL} missing — "
                                      "install the `rust-src` component (`rustup component add "
                                      "rust-src`) for external type records")
            return None
        return self._toolchain

    def _library_dir(self) -> Path:
        assert self._sysroot is not None
        return self._sysroot / SYSROOT_LIBRARY_REL

    # ── resolution ────────────────────────────────────────────────────────────
    def _resolve(self, path: str) -> list[TypeRecord]:
        visited: set[str] = set()
        tried: list[str] = []
        found = self._walk(path, 0, visited, tried)
        if found is None:
            # `<file>: 0 record(s)` on an existing file is the extractor
            # failing to parse that library source (its syn predates some
            # of the newest syntax); a missing file is a path the library
            # does not spell this way.
            self._note(f"unresolved:{path}",
                       f"{_LOG_TAG} {path}: no library source defines it "
                       f"(tried {'; '.join(tried) or 'no candidate file'})")
            return []
        primary, hops = found
        return [replace(primary, external_path=path), *hops]

    def _walk(
        self, path: str, depth: int, visited: set[str], tried: list[str],
    ) -> tuple[TypeRecord, list[TypeRecord]] | None:
        if depth > MAX_REEXPORT_DEPTH or path in visited:
            return None
        visited.add(path)
        name = path.rsplit("::", 1)[-1]
        crate = path.split("::", 1)[0]
        for rel in sysroot_file_candidates(path):
            file = self._library_dir() / rel
            if not file.is_file():
                tried.append(f"{rel} (absent)")
                continue
            enumerated = self._enumerate_file(file)
            if enumerated is None:
                raise _ExtractorUnavailable(str(file))
            raw, uses, text = enumerated
            tried.append(f"{rel} ({len(raw)} record(s), {len(uses)} use(s))")
            module = module_of_library_file(rel)
            hit = next((r for r in raw if r.name == name and r.is_exactly_pub), None) or next(
                (r for r in raw if r.name == name), None)
            if hit is not None:
                return self._build(hit, raw, text, path, module)
            # Not defined here: follow the file's top-level re-export of the name.
            for u in uses:
                if u.is_glob or u.alias != name:
                    continue
                target = resolve_use_path(u.path, crate=crate, module=module)
                if target and target != path:
                    found = self._walk(target, depth + 1, visited, tried)
                    if found is not None:
                        return found
            for u in [u for u in uses if u.is_glob][:4]:
                target = resolve_use_path((*u.path, name), crate=crate, module=module)
                if target and target != path:
                    found = self._walk(target, depth + 1, visited, tried)
                    if found is not None:
                        return found
        return None

    def _build(
        self, hit: TypeRecord, raw: Sequence[TypeRecord], text: str,
        path: str, module: str,
    ) -> tuple[TypeRecord, list[TypeRecord]]:
        unstable = unstable_items(text)
        primary = canonical_external_record(
            hit, external_path=path, source_text=text, unstable=unstable)
        # `is_exactly_pub`: a `pub(crate)` sibling is not the library's API;
        # `mod_path`: nor is a `pub` item inside the file's private module
        # (`private::Internal` beside `core::error::Error`).
        siblings = {r.name: r for r in raw
                    if r.is_exactly_pub and r.is_pub and r.name and r.name != hit.name
                    and not r.mod_path}
        hops = [
            canonical_external_record(
                siblings[n], external_path=f"{module}::{n}", source_text=text,
                unstable=unstable)
            for n in hop_names_for(hit, siblings)
        ]
        return primary, hops

    def _enumerate_file(
        self, file: Path,
    ) -> tuple[list[TypeRecord], list[UseRecord], str] | None:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._note("unreadable", f"{_LOG_TAG} {file}: {e}")
            return None
        with tempfile.TemporaryDirectory(prefix="std_types_") as d:
            src = Path(d) / "src"
            src.mkdir()
            shutil.copy(file, src / file.name)
            # `test` is the library's own test cfg; a consumer crate never
            # sees the items behind it.
            types = self._extractor.enumerate_types(Path(d), cfg_false=("test",))
            if types is None:
                self._note("extractor", f"{_LOG_TAG} the extractor could not enumerate {file} "
                                        "— external type records unavailable")
                return None
            uses = self._extractor.enumerate_uses(Path(d), cfg_false=("test",)) or []
        return types, uses, text

    # ── cache ─────────────────────────────────────────────────────────────────
    @staticmethod
    def cache_file(cache_dir: Path, toolchain: str, path: str) -> Path:
        key = hashlib.sha256(f"{toolchain}\n{path}".encode()).hexdigest()[:24]
        return cache_dir / f"{key}.json"

    def _read_cache(self, cache_dir: Path | None, toolchain: str, path: str) -> list[TypeRecord] | None:
        if cache_dir is None:
            return None
        f = self.cache_file(cache_dir, toolchain, path)
        if not f.is_file():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("toolchain") != toolchain or data.get("path") != path:
                return None
            return [TypeRecord.from_dict(d) for d in data.get("records", [])]
        except Exception:
            return None

    def _write_cache(self, cache_dir: Path | None, toolchain: str, path: str,
                     recs: list[TypeRecord]) -> None:
        if cache_dir is None:
            return
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            f = self.cache_file(cache_dir, toolchain, path)
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "toolchain": toolchain, "path": path,
                "records": [r.to_dict() for r in recs],
            }, indent=1), encoding="utf-8")
            tmp.replace(f)
        except OSError as e:
            self._note("cache-write", f"{_LOG_TAG} cannot write {cache_dir}: {e} "
                                      "(records kept in memory only)")

    # ── logging ───────────────────────────────────────────────────────────────
    def _note(self, key: str, msg: str) -> None:
        if key in self._reported:
            return
        self._reported.add(key)
        self._log(msg)


def _probe_rustc() -> tuple[Path | None, str | None]:
    try:
        sysroot = subprocess.run(["rustc", "--print", "sysroot"], capture_output=True,
                                 text=True, timeout=30)
        version = subprocess.run(["rustc", "--version"], capture_output=True,
                                 text=True, timeout=30)
    except Exception:
        return None, None
    if sysroot.returncode != 0 or version.returncode != 0:
        return None, None
    root, ver = sysroot.stdout.strip(), version.stdout.strip()
    if not root or not ver:
        return None, None
    return Path(root), ver


# ── the wiring: crate tree → external records ─────────────────────────────────

def crate_root_of(src_dir: Path) -> Path | None:
    """The cargo package `src_dir` enumerates — `src_dir` itself or, for a
    bare `src/`, its parent — by the manifest. None for a directory that is
    not a package (a scratch tree holding one source string, as the
    public-API view builds): external resolution is a property of a crate,
    and a lone file has no `std` to resolve against."""
    d = Path(src_dir)
    if (d / "Cargo.toml").is_file():
        return d
    if d.name == "src" and (d.parent / "Cargo.toml").is_file():
        return d.parent
    return None


def cache_dir_for(crate_root: Path) -> Path:
    """`<workspace>/.phase_c_logs/std_types` — the workspace being the
    project directory even when the package is one of its per-cluster trees
    (`.skeleton/<c>`, `.skeleton-filled/<c>`, `.best_tree/<c>`)."""
    workspace = crate_root
    if crate_root.parent.name in RECONSTRUCTION_DIRS:
        workspace = crate_root.parent.parent
    return workspace / PHASE_C_LOGS_DIR / "std_types"


def _crate_sources(src_dir: Path) -> list[tuple[str, str]]:
    """(relative file, text) for the crate's own `.rs` files under `src_dir`,
    sorted — the same walk the extractor takes, minus test/bench/example
    directories and the pipeline's own scratch dirs."""
    out: list[tuple[str, str]] = []
    root = Path(src_dir)
    for p in sorted(root.rglob("*.rs")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIR_NAMES or part in _NON_CRATE_DIRS for part in rel.parts[:-1]):
            continue
        try:
            out.append((rel.as_posix(), p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def _shadowed(name: str, file: str, crate_records: Iterable[TypeRecord]) -> bool:
    """A crate record of this name that is NOT a file-scope item of the
    binding file: a genuinely different item, which the crate-wide name
    table already answers. Same file, file scope, is the cfg-shim pattern —
    Rust forbids two live items of one name there, so the two are
    cfg-exclusive arms and both records are emitted."""
    return any(
        r.name == name and not (r.file == file and not r.mod_path)
        for r in crate_records)


def _cfg_complement_for(name: str, file: str, crate_records: Iterable[TypeRecord]) -> str | None:
    for r in crate_records:
        if r.name == name and r.file == file and not r.mod_path:
            comp = cfg_complement(leading_cfg_predicates(r.definition))
            if comp:
                return comp
    return None


def external_type_records(
    src_dir: Path, crate_records: Sequence[TypeRecord], uses: Sequence[UseRecord],
    source: ExternalTypeSource, *, log: Callable[[str], None] = _ambient_log,
) -> list[TypeRecord]:
    """The external records to append after `crate_records` for the tree at
    `src_dir`: one per `std`/`core`/`alloc` name the crate's `use` lines
    bind (named by the crate's alias, located in the binding file), one per
    module-qualified external type written in the sources (`fmt::Formatter`
    through `use core::fmt`), each followed by its hops — in that order,
    each name once, so the crate's own records keep their positions and a
    prompt's type cap trims externals first. `[]` when `src_dir` is not a
    cargo package."""
    crate_root = crate_root_of(src_dir)
    if crate_root is None:
        return []
    cache_dir = cache_dir_for(crate_root)
    bindings = resolve_external_paths(uses)
    crate_names = frozenset(r.name for r in crate_records if r.name)
    # (record name, path, candidate binding files, note)
    wanted: list[tuple[str, str, tuple[str, ...], str]] = []
    for alias, path in bindings.aliases.items():
        wanted.append((alias, path, bindings.binding_files.get(alias, ()),
                       "re-exported by this crate's use lines"))
    for file, text in _crate_sources(src_dir):
        # Bare aliases are answered above; this scan is for module-qualified
        # and fully written paths in CODE (`fmt::Formatter`,
        # `std::backtrace::Backtrace::capture()`), never in doc comments.
        for written in external_names_in(code_only(text), {}, bindings.modules, crate_names):
            qpath = qualify(written, {}, bindings.modules)
            if qpath is None:
                continue
            wanted.append((qpath.rsplit("::", 1)[-1], qpath, (file,),
                           f"this crate writes it as `{written}`"))
    out: list[TypeRecord] = []
    emitted: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for name, path, files, note in wanted:
        if (name, path) in seen or name in emitted:
            continue
        seen.add((name, path))
        bound_file = next((f for f in files if not _shadowed(name, f, crate_records)), None)
        if bound_file is None:
            continue
        recs = source.records_for(path, cache_dir=cache_dir)
        if not recs:
            continue
        primary, hops = recs[0], [
            h for h in recs[1:]
            if h.name and h.name != name and h.name not in crate_names and h.name not in emitted]
        complement = _cfg_complement_for(name, bound_file, crate_records)
        out.append(annotate_external_record(
            primary, name=name, binding_file=bound_file, binding_note=note, hops=hops,
            cfg_complement=complement))
        emitted.add(name)
        for h in hops:
            out.append(annotate_external_record(
                h, name=h.name, binding_file=bound_file,
                binding_note="not imported by this crate; name it by this full path",
                cfg_complement=complement))
            emitted.add(h.name)
    if out:
        log(f"  {_LOG_TAG} {len(out)} external type record(s) appended for {src_dir}: "
            f"{', '.join(r.name for r in out[:12])}{', …' if len(out) > 12 else ''}")
    return out


__all__ = [
    "ENV_SWITCH", "EXTERNAL_ROOTS", "ExternalTypeSource", "StdTypeSource",
    "cache_dir_for", "crate_root_of", "external_type_records",
    "external_type_records_enabled",
]
