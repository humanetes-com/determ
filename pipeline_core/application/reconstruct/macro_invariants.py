"""Cluster-aware macro-expansion invariants (P6 relocation).

For macros defined multiple times under different `#[cfg(...)]`, render the single
variant active under the cluster's cfg-set — so the model doesn't have to reason
about which `#[cfg]` arm applies (which leads to defensive `unwrap_or(&DEFAULT)`
fills when the active expansion guarantees a `Some`). Reads the workspace `.rs`
source (macros are preserved verbatim on disk through strip); all parsing goes
through `domain.rust_macros` / `domain.cfg` (no regex on source).
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain.item_attributes import outer_attributes_before
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.rust_macros import macro_definitions
from pipeline_core.domain.text import collapse_blank_lines


def enumerate_macro_variants(src_dir: Path) -> list[tuple[str, str, str, str]]:
    """Scan all .rs files under `src_dir` for `macro_rules!` blocks and
    return a list of `(macro_name, cfg_pred, body, source_file)` tuples.

    `cfg_pred` is the gate of the macro's OWN outer-attribute block — the
    `#[cfg(...)]` attribute(s) bound to that `macro_rules!` item, conjoined as
    `all(...)` when there are several, `""` when the block carries none.
    `body` is the full `macro_rules! NAME { ... }` text. Used by
    `compute_cluster_macro_invariants` to decide which variant of a
    multiply-defined macro is active under a given cluster.

    The block is walked back from the `macro_rules!` keyword over contiguous
    outer attributes only (comment/string aware, a wrapped attribute is one
    span) and ends at the previous item's code, so a variant with no gate
    yields `""` and never a neighbour's predicate. The predicate is captured
    with a balanced-paren walk, so `all(not(a), feature = "std")` is read as
    written. The scan this replaced — the LAST `#[cfg(...)]` line in an
    800-char window, matched with a one-nested-level capture — tagged all
    three `backtrace_if_absent!` arms of the live `stable` tree's
    `src/backtrace.rs` with the first arm's `error_generic_member_access`
    (2026-09-01): the second arm's two-level gate was no match at all, so the
    last match in the window was the previous item's, and under `stable`
    every arm evaluated False and the macro was silently omitted.
    """
    variants: list[tuple[str, str, str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        mask = code_mask(text)
        for md in macro_definitions(text):
            if not md.balanced:  # original `if depth != 0: continue`
                continue
            _head, attrs = outer_attributes_before(text, mask, md.decl_start)
            preds = [q for q in (_cfg.cfg_attr_predicate(a) for a in attrs) if q is not None]
            cfg_pred = _cfg.conjoined_cfg_predicate(preds)
            variants.append((md.name, cfg_pred, text[md.decl_start:md.body_close + 1], p.name))
    return variants


def compute_cluster_macro_invariants(src_dir: Path, cluster: BuildCluster | None) -> str:
    """For macros defined multiple times under different `#[cfg(...)]`,
    select the variant active under `cluster`'s cfg-set and render a
    compact "in this cluster, NAME!(...) expands to: <body>" line per
    macro. Macros with a single variant are skipped (no ambiguity).

    Without this section the model has to read every `#[cfg(...)]`
    variant in the MACROS block and reason about which one applies —
    leading to defensive fills (e.g. `unwrap_or(&DEFAULT)`) when the
    active expansion in this cluster guarantees a `Some`.
    """
    if cluster is None:
        return ""
    variants = enumerate_macro_variants(src_dir)
    if not variants:
        return ""
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for name, cfg, body, src in variants:
        by_name.setdefault(name, []).append((cfg, body, src))
    # The cluster's ONE evaluation context, which knows the crate's default-on
    # features. Open-coded from `cfg_true`/`cfg_false` alone this section
    # rendered nothing on the live `stable` tree (2026-09-01): `backtrace!`'s
    # two arms are gated on `feature = "std"` and its negation, both
    # undecidable without the defaults, so the pair fill that had to know
    # what `Backtrace` is saw every arm in the FILE block and no expansion
    # here. With the defaults carried it renders the std arm —
    # `Some(std::backtrace::Backtrace::capture())`.
    cfg_set = _cfg.cluster_cfg_set(cluster)
    lines: list[str] = []
    for name in sorted(by_name):
        vs = by_name[name]
        if len(vs) < 2:
            continue
        # Pick the variant whose cfg evaluates True. A variant with no cfg
        # is treated as always-active fallback.
        active: tuple[str, str, str] | None = None
        unconditional: tuple[str, str, str] | None = None
        for cfg, body, src in vs:
            if not cfg:
                unconditional = (cfg, body, src)
                continue
            v = cfg_set.evaluate(cfg)
            if v is True:
                active = (cfg, body, src)
                break
        if active is None and unconditional is not None:
            active = unconditional
        if active is None:
            continue
        cfg, body, src = active
        # Render compactly: strip the `macro_rules! NAME {` header and
        # outer `}` so the model sees just the arm(s).
        compact = body
        defs = macro_definitions(compact)
        if defs:
            hb = defs[0].body_open + 1  # past the `{`, then the trailing whitespace
            while hb < len(compact) and compact[hb].isspace():
                hb += 1
            compact = compact[hb:].rstrip()
            if compact.endswith("}"):
                compact = compact[:-1].rstrip()
        # Collapse internal whitespace runs for compactness.
        compact = collapse_blank_lines(compact)
        if len(compact) > 240:
            compact = compact[:237] + "..."
        cfg_note = f" [cfg={cfg}]" if cfg else " [always]"
        lines.append(f"{name}!(...){cfg_note} → {compact}")
    if not lines:
        return ""
    header = (
        "Macros with multiple #[cfg]-gated variants — only the active "
        "expansion for this cluster is shown."
    )
    return header + "\n" + "\n".join(lines)
