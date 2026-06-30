"""Cluster-aware static membership analysis (FR-040 / P5).

Given a build cluster (its `cfg_true`/`cfg_false` sets), decide which source
files and functions are *active* — i.e. would compile in that cluster — so the
per-fn fill loop only queues stubs that actually matter for the cluster under
reconstruction. Pure static analysis (no LLM).

Source facts come from the syn extractor through injected callables
(`mod_records`, `enumerate_fns`); cfg evaluation is delegated to
:mod:`pipeline_core.domain.cfg`. The only source-text path is the extractor-
absent regex *fallback* (`file_cfg_predicate_regex_fallback`), which locates a
`mod <stem>;` declaration via a structured line scan (no regex) and reads its
gating cfg attrs through `domain.cfg.cfg_inline_predicates`.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.stub_scan import (
    scan_unimplemented_functions,
)
from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain.models import ModRecord
from pipeline_core.domain.rust_scan import contains_decl_keyword, named_fn_decl_start


def _line_declares_mod(line: str, stem: str) -> bool:
    """True when `line` contains a `mod <stem>;` declaration (token-exact:
    `mod` is a whole word, `stem` follows after whitespace as a whole word,
    then `;` after optional whitespace). Structured replacement for the old
    `\\bmod\\s+stem\\s*;` regex — same match set, no regex on source."""

    def _word_char(c: str) -> bool:
        return c.isalnum() or c == "_"

    n = len(line)
    i = 0
    while True:
        j = line.find("mod", i)
        if j < 0:
            return False
        i = j + 3
        if j > 0 and _word_char(line[j - 1]):
            continue
        if i < n and _word_char(line[i]):
            continue
        k = i
        while k < n and line[k] in " \t":
            k += 1
        if k == i:  # `\s+` requires at least one space after `mod`
            continue
        if not line.startswith(stem, k):
            continue
        e = k + len(stem)
        if e < n and _word_char(line[e]):
            continue
        p = e
        while p < n and line[p] in " \t":
            p += 1
        if p < n and line[p] == ";":
            return True
    # unreachable


def _find_mod_decl(text: str, stem: str) -> tuple[str, int] | None:
    """Return (decl_line, line_start_offset) of the first `mod <stem>;`
    declaration in `text`, or None. Offset is the index of the line's first
    char (matching the old regex's post-newline match start)."""
    pos = 0
    for line in text.split("\n"):
        if _line_declares_mod(line, stem):
            return line, pos
        pos += len(line) + 1
    return None


def file_cfg_predicate_regex_fallback(
    target_src_dir: Path, file_rel: str,
) -> str | None:
    """Extractor-absent fallback for `file_cfg_predicate`. Scans lib.rs/main.rs
    for the file's `mod <stem>;` declaration (structured, no regex) and collects
    the cfg attrs gating it. Known limitation: cannot see cfg attrs the syn
    extractor would attribute to a sibling; the AST path is preferred whenever
    available.
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    for root in ("src/lib.rs", "src/main.rs"):
        p = target_src_dir / root
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        found = _find_mod_decl(text, stem)
        if found is None:
            continue
        decl_line, decl_start = found
        preds: list[str] = []
        mod_idx = decl_line.find("mod ")
        if mod_idx < 0:
            mod_idx = len(decl_line)
        for pred in _cfg.cfg_inline_predicates(decl_line[:mod_idx]):
            preds.append(pred.strip())
        head = text[:decl_start]
        for ln in reversed(head.splitlines()):
            s = ln.strip()
            if not s:
                break
            if s.startswith("//") or s.startswith("/*"):
                continue
            # Stop on any line that contains a declaration keyword — the
            # cfg attrs on such a line belong to ITS item, not ours.
            if contains_decl_keyword(s):
                break
            ln_preds = _cfg.cfg_inline_predicates(ln)
            if ln_preds:
                preds.extend(pr.strip() for pr in ln_preds)
                continue
            if s.startswith("#["):
                continue
            break
        if not preds:
            return None
        if len(preds) == 1:
            return preds[0]
        return "all(" + ", ".join(preds) + ")"
    return None


def file_cfg_predicate(
    target_src_dir: Path,
    file_rel: str,
    *,
    mod_records: Callable[[Path], list[ModRecord] | None],
) -> str | None:
    """Return the cfg predicate (if any) under which `file_rel` is compiled.

    AST-derived via the syn-based skeleton extractor (`mod_records`). Returns
    None when the file is unconditionally included or no parent mod decl is
    found. Falls back to the structured scan when the extractor is unavailable.
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    recs = mod_records(target_src_dir)
    if recs is None:
        return file_cfg_predicate_regex_fallback(target_src_dir, file_rel)
    file_name = Path(file_rel).name
    for r in recs:
        if r.name == stem or r.file == file_name:
            preds: list[str] = list(r.cfg_predicates)
            if not preds:
                return None
            if len(preds) == 1:
                return preds[0]
            return "all(" + ", ".join(preds) + ")"
    return None


def fn_cfg_predicates(file_text: str, fn_name: str) -> list[str]:
    """Return all `#[cfg(...)]` predicates that gate `fn_name` in `file_text`.

    FALLBACK ONLY (R7.4a): callers holding extractor records must pass the
    record's `cfg_predicates` to `fn_belongs_to_cluster` instead — this
    text-walk's backward window can over-capture a PRECEDING fn's cfg attrs
    when fns are not blank-line separated, and it only ever finds the FIRST
    same-named fn in the file.
    """
    m_start = named_fn_decl_start(file_text, fn_name)
    if m_start is None:
        return []
    decl_offset = m_start + (1 if file_text[m_start] == "\n" else 0)
    ws = file_text.rfind("\n\n", 0, decl_offset)
    window_start = ws + 2 if ws >= 0 else 0
    window = file_text[window_start:decl_offset + 1]
    return [pred.strip() for pred in _cfg.cfg_inline_predicates(window)]


def fn_belongs_to_cluster(
    target_src_dir: Path,
    file_rel: str,
    fn_name: str,
    cluster: Any,
    fn_cfg_preds: list[str] | None = None,
    *,
    mod_records: Callable[[Path], list[ModRecord] | None],
) -> bool:
    """True when the fn (or its enclosing file) is active in `cluster`.

    Evaluates the conjunction of the file's parent-mod cfg and the fn's own cfg
    attrs against the cluster's cfg_true / cfg_false sets.

    `fn_cfg_preds` (R7.4a): the syn extractor's cfg list for THIS fn record —
    callers holding extractor records pass it ([] = ungated, trusted as-is).
    Only when None does the legacy text-walk run.

    Conservative on uncertainty: an undecidable predicate defaults to True
    (include the stub) — better to waste a call than miss a fn we should fill.
    """
    cfg_set = _cfg.CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false))
    preds: list[str] = []
    file_pred = file_cfg_predicate(target_src_dir, file_rel, mod_records=mod_records)
    if file_pred:
        preds.append(file_pred)
    if fn_cfg_preds is not None:
        preds.extend(fn_cfg_preds)
    else:
        try:
            text = (target_src_dir / file_rel).read_text(encoding="utf-8")
        except Exception:
            text = ""
        if text:
            preds.extend(fn_cfg_predicates(text, fn_name))
    if not preds:
        return True  # no cfg gating → exists in every cluster
    # False → excluded; None (undecidable) → conservatively kept.
    return all(cfg_set.evaluate(p) is not False for p in preds)


def scan_unimplemented_for_cluster(
    target_src_dir: Path,
    cluster: Any,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
    mod_records: Callable[[Path], list[ModRecord] | None],
    log: Callable[[str], None],
) -> list[tuple[str, str]]:
    """Cluster-aware variant of `scan_unimplemented_functions`.

    Returns only stubs that belong to `cluster` per static cfg evaluation. When
    `cluster` is None, behaves identically to the unfiltered scan.
    """
    raw = scan_unimplemented_functions(target_src_dir, enumerate_fns=enumerate_fns)
    if cluster is None:
        return raw
    out: list[tuple[str, str]] = []
    skipped = 0
    for rel, fn in raw:
        if fn_belongs_to_cluster(target_src_dir, rel, fn, cluster,
                                 mod_records=mod_records):
            out.append((rel, fn))
        else:
            skipped += 1
    if skipped:
        log(f"  [phase-c/per-fn] cluster {cluster.name}: skipped "
            f"{skipped} stub(s) not active in this cluster's cfg-set.")
    return out
