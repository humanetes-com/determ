"""Skeleton index for Phase-C prompts, relocated out of pipeline.py.

`format_skeleton_index` renders the on-disk skeleton as a signatures-only index
(bodies — `unimplemented!()` / macro rules — elided to save input tokens, keeping
type decls + fn signatures + macro NAMES for cross-reference).
`format_skeleton_index_for_cluster` additionally elides items whose leading
`#[cfg(...)]` is false under the cluster's cfg-set, so the model sees only what
compiles in THIS cluster. Pure text; fn/macro spans come from the structured
`fn_headers`/`macro_headers` scanners and cfg eval from `domain.cfg` (no regex).
"""
from __future__ import annotations

from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.prompt_holdings import FileView, file_block
from pipeline_core.domain.rust_fn_headers import (
    HeaderMatch,
    fn_headers,
    macro_headers,
)


def format_skeleton_index(skeleton_files: list[tuple[str, str]]) -> str:
    """Render skeleton as signatures-only index. Drops bodies (which are
    `unimplemented!()` anyway), keeps type declarations + function signatures
    + macro definitions for cross-reference. Used in Phase C prompts so we
    don't waste input tokens on body stubs the model already knows about.

    macro_rules! bodies are also elided. Phase A preserves macros
    verbatim on disk; the prompt's index just needs the macro NAMES so the
    model knows what's exported, not the multi-KB rule definitions.
    """
    out_parts: list[str] = []

    def _elide_bodies(text: str, headers: list[HeaderMatch], terminator: str) -> str:
        """Walk header matches; for each, brace-match to find the body and
        replace it with `terminator`. Returns the rewritten text."""
        result_parts: list[str] = []
        last = 0
        for h in headers:
            body_start = h.brace_open + 1
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            body_end = i
            result_parts.append(text[last:h.start])
            result_parts.append(text[h.start:h.brace_open].rstrip())
            result_parts.append(terminator)
            last = body_end
        result_parts.append(text[last:])
        return "".join(result_parts)

    for rel, content in skeleton_files:
        # Order matters: elide macros first (their bodies contain `fn` patterns
        # in rule expansions that would otherwise confuse the fn scan).
        condensed = _elide_bodies(content, macro_headers(content), " { /* macro body elided — preserved verbatim on disk */ }")
        condensed = _elide_bodies(condensed, fn_headers(condensed, line_anchored=True), ";")
        out_parts.append(file_block(rel, condensed, view=FileView.PART,
                                    attrs=' mode="signatures-only"'))
    return "\n\n".join(out_parts)


def format_skeleton_index_for_cluster(
    skeleton_files: list[tuple[str, str]],
    cluster: BuildCluster | None,
) -> str:
    """Same as format_skeleton_index but additionally elides items whose
    `#[cfg(...)]` evaluates to false under the cluster's cfg-set.

    Used in Phase C prompts so the model sees only items that compile in
    THIS cluster — reduces cognitive load without affecting the on-disk
    unfiltered skeleton (which Rust's own cfg eval handles correctly).

    Items whose cfg is undecidable (unknown keys) are kept (safe default).
    """
    if cluster is None:
        return format_skeleton_index(skeleton_files)
    # The cluster's ONE evaluation context (`domain.cfg.cluster_cfg_set`),
    # which carries the crate's default-on features: built from `cfg_true` /
    # `cfg_false` alone, `not(feature = "std")` was undecidable and the live
    # `stable` index kept `src/backtrace.rs`'s dead `enum Backtrace {}` arm
    # beside the `use` the build actually compiles (2026-09-01).
    cfg_set = _cfg.cluster_cfg_set(cluster)

    # Per-item cfg attr matcher: a leading `#[cfg(...)]` (or several) above the
    # item declaration — via domain.cfg.match_cfg_attr_line.
    filtered_files: list[tuple[str, str]] = []
    for rel, content in skeleton_files:
        # Walk the content line by line collecting leading #[cfg(...)] groups
        # before each top-level item; drop the item block if any cfg evaluates
        # to false.
        lines = content.split("\n")
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            # Collect attributes that precede the current item (any line
            # starting with `#[...]` or `#![...]` or doc comment).
            attr_start = i
            collected_cfgs: list[str] = []
            while i < len(lines):
                line = lines[i]
                stripped = line.strip()
                cfg_pred = _cfg.match_cfg_attr_line(line)
                if cfg_pred is not None:
                    collected_cfgs.append(cfg_pred.strip())
                    i += 1
                    continue
                # Any other attribute / doc / blank line: keep walking.
                if stripped.startswith("#[") or stripped.startswith("#![") or stripped.startswith("///") or stripped.startswith("//!") or stripped == "":
                    i += 1
                    continue
                break
            # i now points to (probably) an item declaration. Evaluate cfgs.
            keep = True
            for pred in collected_cfgs:
                if cfg_set.evaluate(pred) is False:
                    keep = False
                    break
            if keep:
                # Emit attr block + the item.
                out_lines.extend(lines[attr_start:i + 1])
                i += 1
            else:
                # Skip this item entirely — including its body if it has one.
                # Simple heuristic: skip until brace depth returns to zero or
                # we hit a `;` at depth zero (for body-less items like
                # `pub use X;`).
                if i >= len(lines):
                    break
                line = lines[i]
                if "{" in line:
                    depth = line.count("{") - line.count("}")
                    i += 1
                    while i < len(lines) and depth > 0:
                        depth += lines[i].count("{") - lines[i].count("}")
                        i += 1
                else:
                    # body-less item terminated by ';' on the same line
                    i += 1
        filtered_files.append((rel, "\n".join(out_lines)))
    return format_skeleton_index(filtered_files)
