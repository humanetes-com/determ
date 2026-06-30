"""Test-locator use-case — find the tests relevant to one reconstructed fn.

Verbatim relocation of pipeline.py's `_find_tests_for_fn` (P6 engine-relocation).
A layered, ranked heuristic over the crate's test files; all parsing goes through
the `domain.rust_*` structured scanners (no regex on source). Reads test files via
`Path` (application-layer I/O); the composition root re-imports `find_tests_for_fn`
for its two call sites.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.generation import strip_mod_test_prefix
from pipeline_core.domain.rust_idents import impl_self_type
from pipeline_core.domain.rust_macros import macro_definitions
from pipeline_core.domain.rust_scan import count_macro_calls, count_word

# Generic fn names (very common: new, fmt, source, …) whose tests are matched by the
# container TYPE name rather than the fn name alone (Tier 0).
_GENERIC_FN_NAMES = {
    "new", "fmt", "source", "clone", "default", "drop", "next",
    "from", "into", "deref", "deref_mut", "as_ref", "as_mut",
    "hash", "eq", "cmp", "partial_cmp", "from_str", "anyhow_kind",
    "provide",
}


def find_tests_referencing_fn(
    test_files: list[Path],
    fn_name: str,
    max_blocks: int = 4,
) -> list[tuple[str, str]]:
    """Return up to `max_blocks` test files whose source text mentions `fn_name`,
    ranked by reference count. Cheap textual heuristic — sufficient for Rust
    tests that call the fn by name (most common)."""
    if not fn_name:
        return []
    ranked: list[tuple[int, Path]] = []
    for tf in test_files:
        try:
            txt = tf.read_text(encoding="utf-8")
        except Exception:
            continue
        n = count_word(txt, fn_name)
        if n:
            ranked.append((n, tf))
    ranked.sort(key=lambda x: -x[0])
    out: list[tuple[str, str]] = []
    for _, tf in ranked[:max_blocks]:
        try:
            out.append((str(tf), tf.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def find_tests_for_fn(
    tests_dir_files: list[Path],
    fn_name: str,
    file_rel: str = "",
    src_dir: Path | None = None,
    max_blocks: int = 4,
    container: str = "",
) -> list[tuple[str, str]]:
    """Layered heuristic to find tests relevant to one fn.

    Order of attempts (each adds to the candidate set):
      0. Container-type filter (P5.4) — when fn_name is generic (`new`,
         `fmt`, `source`, etc.), match by the type name extracted from
         `container` rather than the fn name alone. Tests for
         `impl Display for ContextError::fmt` only count when they
         mention "ContextError".
      1. Direct name reference — tests calling `fn_name` literally.
      2. File-stem match — for fn in `src/ensure.rs`, include tests whose
         filename contains "ensure".
      3. Macro-aware — if `src_dir` is given and `file_rel`'s file defines
         macros, include tests that call those macros by name.

    Results ranked by relevance score (container > direct > stem > macro)
    then by hit count. Returns up to `max_blocks` (path, content) tuples.
    """
    if not fn_name and not file_rel:
        return []
    # Build candidate set with scores.
    scored: dict[Path, tuple[int, int]] = {}  # path → (tier, hits) — lower tier = better

    # P5.4 Tier 0: when fn_name is generic (very common: new, fmt, source,
    # clone, drop, default), filter by the container's TYPE NAME so we don't
    # match every test that mentions e.g. "new". Falls back to the regular
    # name-based search if the container doesn't yield a clear type name.
    container_type = ""
    if container:
        # "for X" of "impl Trait for X<...>", else "impl X<...>" (inherent).
        container_type = impl_self_type(container) or ""
    if fn_name in _GENERIC_FN_NAMES and container_type:
        for tf in tests_dir_files:
            try:
                txt = tf.read_text(encoding="utf-8")
            except Exception:
                continue
            n = count_word(txt, container_type)
            if n:
                cur = scored.get(tf, (99, 0))
                # Tier 0 = highest priority. Hit count = type mentions.
                scored[tf] = (0, max(cur[1], n))

    # Tier 1: direct name.
    if fn_name:
        for tf in tests_dir_files:
            try:
                txt = tf.read_text(encoding="utf-8")
            except Exception:
                continue
            n = count_word(txt, fn_name)
            if n:
                cur = scored.get(tf, (99, 0))
                scored[tf] = (min(cur[0], 1), max(cur[1], n))

    # Tier 2: file-stem match.
    if file_rel:
        stem = Path(file_rel).stem.lower()
        # Strip common prefixes/suffixes that don't help matching:
        stem = strip_mod_test_prefix(stem)
        for tf in tests_dir_files:
            tname = tf.stem.lower()
            if stem and (stem in tname or tname in stem):
                cur = scored.get(tf, (99, 0))
                scored[tf] = (min(cur[0], 2), max(cur[1], 1))

    # Tier 3: macro-aware.
    if file_rel and src_dir is not None:
        fp = src_dir / file_rel
        macro_names: set[str] = set()
        try:
            txt = fp.read_text(encoding="utf-8")
            for md in macro_definitions(txt):
                macro_names.add(md.name)
        except Exception:
            pass
        if macro_names:
            for tf in tests_dir_files:
                try:
                    ttxt = tf.read_text(encoding="utf-8")
                except Exception:
                    continue
                hits = sum(count_macro_calls(ttxt, n) for n in macro_names)
                if hits:
                    cur = scored.get(tf, (99, 0))
                    scored[tf] = (min(cur[0], 3), max(cur[1], hits))

    ranked = sorted(scored.items(), key=lambda kv: (kv[1][0], -kv[1][1]))[:max_blocks]
    out: list[tuple[str, str]] = []
    for path, _ in ranked:
        try:
            out.append((str(path), path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out
