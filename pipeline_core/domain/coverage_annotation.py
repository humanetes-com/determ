"""Pure domain functions for annotating source with coverage information.

All functions are deterministic text transforms with no I/O, no global state.
"""
from __future__ import annotations

from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain.rust_idents import is_fn_signature_line
from pipeline_core.domain.rust_scan import branch_guard_label, find_mod_opener, is_rust_item_boundary_line


def extract_branch_guard(lines: list[str], line_no: int) -> str | None:
    """Walk backward from line_no to find the nearest enclosing if/match-arm/while condition."""
    for i in range(line_no - 2, max(-1, line_no - 30), -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if is_rust_item_boundary_line(stripped):
            break
        label = branch_guard_label(stripped)
        if label is not None:
            return label
    return None


def annotate_uncovered(content: str, uncovered: set[int]) -> str:
    """Return source annotated with '!!' markers on uncovered lines, including branch guards."""
    lines = content.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        if i in uncovered:
            guard = extract_branch_guard(lines, i)
            guard_hint = f" [guard: {guard}]" if guard else ""
            marker = f"  // !! UNCOVERED{guard_hint}"
        else:
            marker = ""
        out.append(f"{i:4d} | {line}{marker}")
    return "\n".join(out)


def focused_annotation(content: str, uncovered: set[int], ctx: int = 4) -> str:
    """Show only uncovered lines plus `ctx` lines of surrounding context."""
    lines = content.splitlines()
    show: set[int] = set()
    for u in uncovered:
        for i in range(max(1, u - ctx), min(len(lines) + 1, u + ctx + 1)):
            show.add(i)
    result, prev = [], None
    for i in sorted(show):
        if prev is not None and i > prev + 1:
            result.append("     | ...")
        if i in uncovered:
            guard = extract_branch_guard(lines, i)
            guard_hint = f" [guard: {guard}]" if guard else ""
            marker = f"  // !! UNCOVERED{guard_hint}"
        else:
            marker = ""
        result.append(f"{i:4d} | {lines[i - 1]}{marker}")
        prev = i
    return "\n".join(result)


def extract_uncovered_functions(content: str, uncovered: set[int], max_fns: int = 0) -> str:
    """Extract complete bodies of functions containing uncovered lines.

    Falls back to focused_annotation when no function bodies can be identified.
    """
    if not uncovered:
        return focused_annotation(content, uncovered)

    code_lines = content.splitlines()
    n = len(code_lines)
    result_blocks: list[str] = []

    i = 0
    while i < n:
        if is_fn_signature_line(code_lines[i]):
            fn_start = i
            depth = 0
            fn_end = i
            for j in range(i, min(n, i + 300)):
                depth += code_lines[j].count('{') - code_lines[j].count('}')
                fn_end = j
                if j > i and depth <= 0:
                    break
            fn_line_nums = set(range(fn_start + 1, fn_end + 2))
            if fn_line_nums & uncovered:
                annotated = []
                for k in range(fn_start, fn_end + 1):
                    lineno = k + 1
                    marker = "  // !! UNCOVERED" if lineno in uncovered else ""
                    annotated.append(f"{lineno:4d} | {code_lines[k]}{marker}")
                result_blocks.append("\n".join(annotated))
            i = fn_end + 1
        else:
            i += 1

    if not result_blocks:
        return focused_annotation(content, uncovered)

    if max_fns and len(result_blocks) > max_fns:
        result_blocks = result_blocks[:max_fns]

    return "\n\n// --- next function ---\n\n".join(result_blocks)


def partition_uncovered_lines(
    content: str, uncovered: set[int], n_groups: int
) -> list[set[int]]:
    """Split uncovered lines into up to n_groups disjoint buckets, keeping each function together.

    Used by task0c split-and-merge so N concurrent generations each own a distinct subset.
    """
    n_groups = max(1, n_groups)
    if not uncovered or n_groups == 1:
        return [set(uncovered)] if uncovered else []

    code_lines = content.splitlines()
    n = len(code_lines)
    remaining = set(uncovered)
    units: list[set[int]] = []

    i = 0
    while i < n:
        if is_fn_signature_line(code_lines[i]):
            depth = 0
            fn_end = i
            for j in range(i, min(n, i + 300)):
                depth += code_lines[j].count('{') - code_lines[j].count('}')
                fn_end = j
                if j > i and depth <= 0:
                    break
            fn_lines = set(range(i + 1, fn_end + 2)) & remaining
            if fn_lines:
                units.append(fn_lines)
                remaining -= fn_lines
            i = fn_end + 1
        else:
            i += 1
    if remaining:
        units.append(remaining)

    if len(units) <= 1:
        return units if units else [set(uncovered)]

    buckets: list[set[int]] = [set() for _ in range(min(n_groups, len(units)))]
    for unit in sorted(units, key=len, reverse=True):
        smallest = min(buckets, key=len)
        smallest |= unit
    return [b for b in buckets if b]


def filter_cfg_incompatible_lines(
    source_text: str,
    uncov_lines: set[int],
    active_cfgs: list[str],
) -> set[int]:
    """Return uncov_lines inside #[cfg(...)] blocks incompatible with active_cfgs.

    Uses typed CfgSet — no regex on source.
    """
    if not active_cfgs:
        return set()
    ctx = _cfg.CfgSet.of(cfg_true=[t for p in active_cfgs for t in (p, f'feature = "{p}"')])
    return ctx.excluded_uncovered_lines(source_text, uncov_lines)


def filter_cfg_excluded_lines(
    source_text: str,
    uncov_lines: set[int],
    excluded_cfgs: list[str],
) -> set[int]:
    """Return uncov_lines inside #[cfg(X)] blocks where X is in excluded_cfgs.

    Uses typed CfgSet — no regex on source.
    """
    if not excluded_cfgs:
        return set()
    ctx = _cfg.CfgSet.of(cfg_false=[t for p in excluded_cfgs for t in (p, f'feature = "{p}"')])
    return ctx.excluded_uncovered_lines(source_text, uncov_lines)


def inject_allow_before_mod(block: str, allow_line: str, *, gen_only: bool) -> str:
    """Insert `allow_line` before the first `[gen_]mod` opener (find_mod_opener)."""
    if allow_line in block:
        return block
    opener = find_mod_opener(block, gen_only=gen_only)
    if opener is None:
        return block
    return block[:opener[0]] + f"{allow_line}\n" + block[opener[0]:]
