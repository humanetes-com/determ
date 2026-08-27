"""Filesystem I/O for gen_tests sibling files.

Handles reading, writing, reverting, quarantining, and stripping gen_tests_N.rs
sibling files and their `mod gen_tests_N;` path-declarations in parent sources.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from pipeline_core.domain.generation import (
    as_integration_test,
    build_gen_tests_decl,
    extract_gen_tests_body,
    gen_tests_integration_rel,
    gen_tests_sibling_number,
    has_gen_tests_mod_decl,
    is_claimed_pipeline_gen_test,
    is_claimed_pipeline_gen_test_path,
    is_gen_tests_sibling_name,
    parse_gen_tests_filename,
    parse_gen_tests_integration_filename,
    strip_all_gen_tests_decls,
    strip_gen_tests_decl,
)
from pipeline_core.domain.generation import (
    derive_gentest_prelude as _derive_gentest_prelude_pure,
)
from pipeline_core.domain.rust_scan import rust_delimiters_balanced


def gen_tests_siblings(src_file: Path) -> list[tuple[int, Path]]:
    """Return [(N, path)] for all foo.gen_tests_N.rs siblings, sorted by N."""
    stem = src_file.stem
    out: list[tuple[int, Path]] = []
    parent = src_file.parent
    if parent.exists():
        for f in parent.iterdir():
            num = gen_tests_sibling_number(f.name, stem)
            if num is not None:
                out.append((num, f))
    out.sort()
    return out


def gen_tests_count(src_file: Path) -> int:
    return len(gen_tests_siblings(src_file))


def next_gen_tests_n(src_file: Path) -> int:
    """Return the next free gen_tests pass number (max existing + 1, skipping gaps).

    Counts both `src/{stem}.gen_tests_N.rs` siblings (the pre-placement
    oracle) and `tests/{stem}_gen_N.rs` integration files, so a tree that
    already has a sibling pass 7 does not reuse `tests/error_gen_7.rs`."""
    nums = [n for n, _ in gen_tests_siblings(src_file)]
    root = _crate_root(src_file)
    if root is not None:
        tests = root / "tests"
        if tests.is_dir():
            for f in tests.iterdir():
                parsed = parse_gen_tests_integration_filename(f.name)
                if parsed and parsed[0] == src_file.stem:
                    nums.append(parsed[1])
    return (max(nums) + 1) if nums else 1


def gen_tests_path(src_file: Path, n: int) -> Path:
    return src_file.with_name(f"{src_file.stem}.gen_tests_{n}.rs")


def _crate_root(src_file: Path) -> Path | None:
    for p in src_file.parents:
        if (p / "Cargo.toml").is_file():
            return p
    return None


def gen_tests_integration_path(project_dir: Path, src_file: Path, n: int) -> Path:
    """`tests/{stem}_gen_{n}.rs` under the crate that `src_file` belongs to.

    `project_dir` is the workspace crate root (task0c's cwd). A src_file
    under a cluster tree would resolve via Cargo.toml; task0c always
    writes against the original crate."""
    return Path(project_dir) / gen_tests_integration_rel(src_file.stem, n)


def read_all_gen_tests(src_file: Path) -> str:
    """Concatenate gen_tests sibling bodies re-wrapped in `mod gen_tests_N { ... }`
    blocks, plus any `tests/{stem}_gen_N.rs` integration files for this stem.

    The concatenation is the `old_block` the generator is shown so it does
    not re-emit tests already on disk."""
    parts: list[str] = []
    for n, sibling in gen_tests_siblings(src_file):
        try:
            body = sibling.read_text(encoding="utf-8").rstrip()
        except OSError:
            continue
        parts.append(f"#[cfg(test)]\nmod gen_tests_{n} {{\n{body}\n}}")
    root = _crate_root(src_file)
    if root is not None:
        tests = root / "tests"
        if tests.is_dir():
            found: list[tuple[int, Path]] = []
            for f in tests.iterdir():
                parsed = parse_gen_tests_integration_filename(f.name)
                if parsed and parsed[0] == src_file.stem:
                    found.append((parsed[1], f))
            for _n, path in sorted(found):
                try:
                    body = path.read_text(encoding="utf-8").rstrip()
                except OSError:
                    continue
                if not is_claimed_pipeline_gen_test(path.name, body):
                    continue
                parts.append(f"// {path.relative_to(root).as_posix()}\n{body}")
    return "\n\n".join(parts)


def read_frozen_test_sources(project_dir: Path) -> dict[str, str]:
    """Every WRITE-PROTECTED test the crate carries: the gen_tests siblings PLUS
    the crate's own `tests/` integration tests, rel path → text.

    `read_oracle_sibling_sources` deliberately sees only the siblings, because
    what it feeds — the member-path constraint — is about the private members
    those generated tests read. That filter is wrong for any question about what
    the FROZEN SPECIFICATION demands, because the crate's hand-written
    integration tests are equally write-protected and equally unable to be
    edited to fit a layout.

    Measured cost of the gap, 2026-08-11: the derive requirement that broke five
    of eight layout authorings is `Counter::default()` in
    `tmp/hardcrate/tests/baseline.rs:14` and `tests/integration.rs:22` — both
    integration tests, neither a `gen_tests` sibling. A scan wired to the
    siblings alone returned nothing and the requirement never reached the
    prompt, which reads exactly like the scan not working.

    Same dot-directory and `target/` exclusions as the sibling reader, and for
    the same reason: `.skeleton/`, `.skeleton-filled/` and `.best_tree/` each
    hold a copy of these files.
    """
    out = read_oracle_sibling_sources(project_dir)
    root = Path(project_dir)
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return out
    for f in sorted(tests_dir.rglob("*.rs")):
        rel_parts = f.relative_to(root).parts
        if any(p.startswith(".") or p == "target" for p in rel_parts[:-1]):
            continue
        try:
            out[str(f.relative_to(root))] = f.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def read_oracle_sibling_sources(project_dir: Path) -> dict[str, str]:
    """Every gen_tests sibling under `project_dir`, rel path → text.

    The crate's in-crate test oracle as the layout phase needs to see it: these
    files are generated pre-strip, from the ORIGINAL source, and are
    write-protected from then on — so the member paths they read are a constraint
    on any layout the model authors for the types they touch.

    Dot-directories are skipped. `.skeleton/`, `.skeleton-filled/`, `.best_tree/`
    and `.cluster_augmented/` all hold copies of the same siblings (38 apiece on
    `anyhow` 2026-08-06); reading them would multiply every path by the number of
    working trees on disk while adding no constraint. `target/` is skipped for the
    same reason a wipe skips it — it is build output, not source."""
    out: dict[str, str] = {}
    root = Path(project_dir)
    if not root.exists():
        return out
    for f in sorted(root.rglob("*.rs")):
        rel_parts = f.relative_to(root).parts
        if any(p.startswith(".") or p == "target" for p in rel_parts[:-1]):
            continue
        if not is_gen_tests_sibling_name(f.name):
            continue
        try:
            out[str(f.relative_to(root))] = f.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def load_gen_tests_origin(project_dir: Path) -> dict[str, str]:
    """Load the gen_tests origin-cfg manifest baked pre-strip.

    Returns {sibling_stem → cluster_cfg_predicate}; '' = stable/default cluster.
    Empty dict if absent (older runs / pre-bake).
    """
    path = project_dir / ".skeleton" / "_gen_tests_origin.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


def write_gen_tests_pass(
    src_file: Path,
    src_text: str,
    block: str,
    allow_line: str,
    pass_n: int,
    cluster_cfg_attr: str = "",
    log_fn: Callable[[str], None] = lambda s: None,
) -> Path:
    """Write the sibling file and append its decl to src_file.

    Performs a delimiter-balance check; on failure quarantines the sibling without
    updating the parent (bad pass becomes a compile-time no-op). Returns sibling path.
    """
    sibling = gen_tests_path(src_file, pass_n)
    body = extract_gen_tests_body(block)
    if not rust_delimiters_balanced(body):
        quarantine = sibling.with_suffix(".rs.broken")
        quarantine.write_text(body, encoding="utf-8")
        with suppress(Exception):
            log_fn(
                f"  [gen_tests] REJECTED pass {pass_n} for {src_file.name}: "
                f"delimiters unbalanced. Quarantined → {quarantine.name}"
            )
        return sibling
    sibling.write_text(body, encoding="utf-8")
    decl = build_gen_tests_decl(sibling.name, pass_n, allow_line, cluster_cfg_attr=cluster_cfg_attr)
    src_file.write_text(src_text.rstrip() + decl, encoding="utf-8")
    return sibling


def write_gen_tests_integration(
    project_dir: Path,
    src_file: Path,
    block: str,
    *,
    crate: str,
    allow_line: str = "",
    pass_n: int,
    log_fn: Callable[[str], None] = lambda s: None,
) -> Path:
    """Write `tests/{stem}_gen_{n}.rs` as an integration test. Does not
    touch the source file — there is no path-mod to declare.

    A delimiter-unbalanced body is quarantined as `.rs.broken` and is a
    compile-time no-op, matching :func:`write_gen_tests_pass`."""
    dest = gen_tests_integration_path(project_dir, src_file, pass_n)
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = as_integration_test(block, crate=crate, allow_line=allow_line)
    if not rust_delimiters_balanced(body):
        quarantine = dest.with_suffix(".rs.broken")
        quarantine.write_text(body, encoding="utf-8")
        with suppress(Exception):
            log_fn(
                f"  [gen_tests] REJECTED pass {pass_n} for {src_file.name}: "
                f"delimiters unbalanced. Quarantined → {quarantine.name}"
            )
        return dest
    dest.write_text(body, encoding="utf-8")
    return dest


def quarantine_broken_gen_tests(
    project_dir: Path, log_fn: Callable[[str], None] = lambda s: None
) -> int:
    """Rename gen_tests siblings with unbalanced delimiters to .rs.broken and strip decls.

    Startup recovery for state from earlier runs. Returns count quarantined.
    """
    moved = 0
    for src in (project_dir / "src").rglob("*.gen_tests_*.rs"):
        try:
            text = src.read_text(encoding="utf-8")
        except Exception:
            continue
        if rust_delimiters_balanced(text):
            continue
        parsed = parse_gen_tests_filename(src.name)
        if parsed is None:
            continue
        stem, pass_n = parsed
        parent = src.parent / f"{stem}.rs"
        try:
            if parent.exists():
                ptext = parent.read_text(encoding="utf-8")
                ptext_new = strip_gen_tests_decl(ptext, src.name, pass_n)
                if ptext_new != ptext:
                    parent.write_text(ptext_new, encoding="utf-8")
        except Exception:
            pass
        try:
            src.rename(src.with_suffix(".rs.broken"))
            moved += 1
        except Exception:
            try:
                src.unlink()
                moved += 1
            except Exception:
                pass
    if moved:
        log_fn(f"  [gen_tests] quarantined {moved} broken sibling(s) at startup.")
    return moved


def revert_gen_tests_pass(src_file: Path, src_text: str, sibling: Path) -> None:
    """Restore src_file to src_text and delete the written test file.

    For a src sibling, also drop orphaned siblings whose decl the restore
    removed. For a `tests/{stem}_gen_N.rs` integration file there is no
    parent decl — only the file is removed, and src_text is written back
    only when it actually changed (the integration path never edits src).
    """
    if src_file.exists() or src_text:
        try:
            current = src_file.read_text(encoding="utf-8") if src_file.exists() else ""
        except OSError:
            current = ""
        if current != src_text:
            src_file.write_text(src_text, encoding="utf-8")
    with suppress(OSError):
        sibling.unlink()
    parsed = parse_gen_tests_integration_filename(sibling.name)
    if parsed is not None:
        return
    for _n, _sib in gen_tests_siblings(src_file):
        if not has_gen_tests_mod_decl(src_text, _n):
            with suppress(OSError):
                _sib.unlink()


def strip_all_gen_tests_artifacts(src_file: Path) -> int:
    """Delete all gen_tests siblings AND `tests/{stem}_gen_N.rs` files for
    this stem, and remove decls from src_file. Returns count deleted."""
    count = 0
    for _, sib in gen_tests_siblings(src_file):
        try:
            sib.unlink()
            count += 1
        except OSError:
            pass
    root = _crate_root(src_file)
    if root is not None:
        tests = root / "tests"
        if tests.is_dir():
            for f in list(tests.iterdir()):
                parsed = parse_gen_tests_integration_filename(f.name)
                if (parsed and parsed[0] == src_file.stem
                        and is_claimed_pipeline_gen_test_path(f)):
                    try:
                        f.unlink()
                        count += 1
                    except OSError:
                        pass
    if src_file.exists():
        original = src_file.read_text(encoding="utf-8")
        cleaned = strip_all_gen_tests_decls(original)
        if cleaned != original:
            src_file.write_text(cleaned, encoding="utf-8")
    return count


def derive_gentest_prelude(project_dir: Path) -> tuple[str, str]:
    """Derive the gen_tests allow-line + prelude from the crate root file
    (lib.rs, falling back to main.rs). Pure derivation lives in
    domain.generation.derive_gentest_prelude; this adapter owns the read."""
    lib_rs = project_dir / "src" / "lib.rs"
    if not lib_rs.exists():
        lib_rs = project_dir / "src" / "main.rs"
    text = lib_rs.read_text(encoding="utf-8") if lib_rs.exists() else ""
    return _derive_gentest_prelude_pure(text)
