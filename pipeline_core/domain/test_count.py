"""Pure cfg-aware test-fn counting primitives (debt #29 extraction).

The FR-024 strict-verdict gate needs to know how many `#[test]` fns SHOULD run
under a cluster's cfg, so cargo-cannot-compile tests don't inflate the expected
count (an UNDERcount manufactures a fraudulent TASK-3 SUCCESS — FR-018/FR-022).

These three functions are the PURE half: given already-read text / typed fn
records plus the cluster's cfg sets, they count cargo-faithfully. No I/O, no
extractor — the orchestration that walks the tree and invokes the extractor
lives in :mod:`pipeline_core.application.test_count`.

Behaviour is byte-identical to the former `pipeline._count_test_fns_cfg_aware` /
`_count_test_fns_in_records` / `_file_top_cfg_excluded`: every cfg decision is
`CfgSet.of(...).evaluate(...)` / `.excludes_for_cargo(...)`, exactly as the old
thin shims built it.
"""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.cfg import (
    CfgSet,
    cfg_inline_predicates,
    crate_cfg_predicates,
)
from pipeline_core.domain.rust_scan import is_attr_line, is_test_attr_line


def file_top_cfg_excluded(
    content: str, cfg_true: set[str], cfg_false: set[str],
) -> bool:
    """True when a top-level inner `#![cfg(...)]` evaluates decidably false for
    this cluster — the file compiles to an EMPTY test binary, so none of its
    `#[test]` fns run. Conservative: any uncertainty (undecidable) → keep."""
    cs = CfgSet.of(cfg_true, cfg_false)
    return any(cs.evaluate(pred.strip()) is False
               for pred in crate_cfg_predicates(content))


def count_test_fns_cfg_aware(
    text: str,
    cfg_true: set[str],
    cfg_false: set[str],
    default_features: frozenset[str] | set[str] = frozenset(),
) -> int:
    """Count `#[test]`-attributed fn declarations in `text` whose surrounding
    `#[cfg(...)]` attributes don't gate them out of this cluster.

    Pragmatic line-level walker: for each `#[test]` line, collect the cfg
    predicates of the contiguous attribute block above the fn declaration. If
    any of them is cargo-excluded under (cfg_true, cfg_false, default_features),
    drop the fn; otherwise count it (true or undecidable predicates → counted).
    A file gated out by a top-level `#![cfg(...)]` counts zero."""
    if file_top_cfg_excluded(text, cfg_true, cfg_false):
        return 0
    cs = CfgSet.of(cfg_true, cfg_false, default_features)
    lines = text.splitlines()
    count = 0
    for i, line in enumerate(lines):
        if not is_test_attr_line(line):
            continue
        gates: list[str] = [p.strip() for p in cfg_inline_predicates(line)]
        j = i - 1
        while j >= 0:
            s = lines[j].strip()
            if not s:
                break  # blank line ends the attribute block
            if s.startswith("//") or s.startswith("/*"):
                j -= 1
                continue
            if not is_attr_line(lines[j]):
                break  # not an attribute line — chain ends
            gates.extend(p.strip() for p in cfg_inline_predicates(lines[j]))
            j -= 1
        if any(cs.excludes_for_cargo(g) for g in gates):
            continue
        count += 1
    return count


def count_test_fns_in_records(
    records: list[dict[str, Any]],
    cfg_true: set[str],
    cfg_false: set[str],
    default_features: frozenset[str] | set[str] = frozenset(),
) -> int:
    """Count test fns (`FnRecord.is_test`) from the syn extractor's typed records
    whose accumulated `#[cfg(...)]` predicates don't gate them out of this
    cluster. The extractor accumulates the WHOLE enclosing chain into
    `cfg_predicates` (mod/impl/trait + the fn's own attrs), so this is more
    cargo-faithful than the line-walking marker scan. File-level inner
    `#![cfg(...)]` is NOT on fn records — the caller applies
    `file_top_cfg_excluded` separately."""
    cs = CfgSet.of(cfg_true, cfg_false, default_features)
    count = 0
    for r in records:
        if not r.get("is_test"):
            continue
        gates = r.get("cfg_predicates") or []
        if any(cs.excludes_for_cargo(g) for g in gates):
            continue
        count += 1
    return count


def test_fn_names_in_records(
    records: list[dict[str, Any]],
    cfg_true: set[str],
    cfg_false: set[str],
    default_features: frozenset[str] | set[str] = frozenset(),
) -> frozenset[str]:
    """The NAMES of the test fns `count_test_fns_in_records` counts — same
    cfg filter, same records. Feeds the sweep-oracle reconciliation (which
    expected test never reported a verdict), where the count alone can only
    say HOW MANY are missing."""
    cs = CfgSet.of(cfg_true, cfg_false, default_features)
    out: set[str] = set()
    for r in records:
        if not r.get("is_test"):
            continue
        gates = r.get("cfg_predicates") or []
        if any(cs.excludes_for_cargo(g) for g in gates):
            continue
        name = str(r.get("fn_name") or r.get("name") or "")
        if name:
            out.add(name)
    return frozenset(out)
