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
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.rust_macros import macro_definitions
from pipeline_core.domain.text import collapse_blank_lines


def enumerate_macro_variants(src_dir: Path) -> list[tuple[str, str, str, str]]:
    """Scan all .rs files under `src_dir` for `macro_rules!` blocks and
    return a list of `(macro_name, cfg_pred, body, source_file)` tuples.

    `cfg_pred` is the predicate of the `#[cfg(...)]` attribute immediately
    preceding the macro (empty string if no cfg gate). `body` is the full
    `macro_rules! NAME { ... }` text. Used by
    `compute_cluster_macro_invariants` to decide which variant of a
    multiply-defined macro is active under a given cluster.
    """
    variants: list[tuple[str, str, str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for md in macro_definitions(text):
            if not md.balanced:  # original `if depth != 0: continue`
                continue
            name = md.name
            start = md.decl_start
            # Walk backwards from the macro to find a leading #[cfg(...)]
            # attribute on the same item. Stop at a blank line or another
            # item — only attributes that immediately precede the macro
            # gate it.
            head = text[max(0, start - 800):start]
            cfg_pred = _cfg.last_trailing_cfg_pred(head).strip()
            variants.append((name, cfg_pred, text[start:md.body_close + 1], p.name))
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
