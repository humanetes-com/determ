"""Pure cfg-aware test and doc-test counting primitives.

The strict per-cluster task3 verdict needs to know how many `#[test]` fns SHOULD run
under a cluster's cfg, so cargo-cannot-compile tests don't inflate the expected
count (an UNDERcount manufactures a fraudulent TASK-3 SUCCESS: every test the
count fails to expect is one that may silently never run).

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

import shlex
from collections.abc import Iterable, Mapping
from typing import Any

from pipeline_core.domain.cfg import (
    CfgSet,
    cfg_inline_predicates,
    crate_cfg_predicates,
)
from pipeline_core.domain.doc_tests import count_doc_tests_in_doc
from pipeline_core.domain.models import DocItemRecord
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
    `file_top_cfg_excluded` separately.

    Counts DISTINCT `(mod_path, fn_name)` pairs, not records. Rust forbids two
    items of one name in one module, so several surviving records for one pair
    are mutually-exclusive source variants of a single compiled test — the shape
    an attribute macro produces (`#[rustversion::not(nightly)] fn t()` beside
    `#[rustversion::nightly] fn t()`), where the gate is opaque to any cfg
    evaluation. Counting both inflated the expected oracle by one FOREVER: the
    surplus never reports a verdict, so every sweep charged a phantom failure,
    `SweepOracle.intact` could never hold, and a cluster at 100% real pass could
    never be declared done."""
    cs = CfgSet.of(cfg_true, cfg_false, default_features)
    seen: set[tuple[str, str]] = set()
    unnamed = 0
    for r in records:
        if not r.get("is_test"):
            continue
        gates = r.get("cfg_predicates") or []
        if any(cs.excludes_for_cargo(g) for g in gates):
            continue
        name = str(r.get("fn_name", ""))
        if not name:
            # Unidentifiable record: it cannot be PROVEN a duplicate, and
            # collapsing it would undercount — the one direction that
            # manufactures a fraudulent pass.
            unnamed += 1
            continue
        seen.add((str(r.get("mod_path", "")), name))
    return len(seen) + unnamed


def count_doc_tests_in_records(
    records: Iterable[DocItemRecord],
    cfg_true: set[str],
    cfg_false: set[str],
    default_features: frozenset[str] | set[str] = frozenset(),
) -> int:
    """Doc tests rustdoc collects from these items under the cluster's cfg.

    rustdoc only documents items that compile, so an item whose accumulated
    cfg chain (its own gate, every enclosing impl/trait/mod, the file's inner
    `#![cfg]`) excludes it contributes no doc test. A text scan cannot see that
    chain. A merged multi-cluster tree keeps one cfg-gated copy of every
    divergent item per cluster, doc comment included, and the scan counted the
    examples of every copy: on the merged anyhow tree (run 185710-f6135a93,
    2026-09-17) it expected 33 doc tests where rustdoc ran 25 under each
    cluster, so task3 read a clean `stable` run (150 passed, 2 ignored, rc=0)
    as incomplete against 160 expected and the run ended non-converged.

    Same cfg evaluation as :func:`count_test_fns_in_records`: an undecidable
    gate counts, so the expected count is never shrunk by a guess."""
    cs = CfgSet.of(cfg_true, cfg_false, default_features)
    return sum(
        count_doc_tests_in_doc(r.doc) for r in records
        if not any(cs.excludes_for_cargo(g) for g in r.cfg_predicates))



def _cfg_flag_names(flags: str) -> set[str]:
    """The `--cfg` names one RUSTFLAGS/RUSTDOCFLAGS value sets (`--cfg N` and
    `--cfg=N`; a `key="value"` cfg keeps its whole spelling)."""
    try:
        tokens = shlex.split(flags)
    except ValueError:
        tokens = flags.split()
    out: set[str] = set()
    for i, tok in enumerate(tokens):
        if tok == "--cfg" and i + 1 < len(tokens):
            out.add(tokens[i + 1].strip())
        elif tok.startswith("--cfg="):
            out.add(tok[len("--cfg="):].strip())
    return out


def rustdoc_cfg_sets(
    cfg_true: Iterable[str],
    cfg_false: Iterable[str],
    env_extra: Mapping[str, str] | None,
) -> tuple[set[str], set[str]]:
    """(true, false) cfg sets as RUSTDOC sees them for a cluster built under
    `env_extra`.

    cargo hands RUSTFLAGS to rustc and RUSTDOCFLAGS to rustdoc, so a cluster
    that activates a cfg with `RUSTFLAGS=--cfg N` compiles `#[cfg(N)]` items
    and still documents — and collects doc tests from — the `not(N)` arms.
    A cluster's `cfg_true` lists both spellings of such an atom (`N` and
    `feature = "N"`, since a cluster delivers one of the two); the `--cfg`
    delivery reaches neither form for rustdoc, so both move to false.
    Measured on the merged anyhow tree (run 185710-f6135a93, 2026-09-17): the
    `nightly` cluster (`RUSTFLAGS=--cfg error_generic_member_access`) ran the
    doc tests of the not-`error_generic_member_access` copies — 177 reported
    where the rustc cfg set counted 178."""
    rustc_only = _cfg_flag_names((env_extra or {}).get("RUSTFLAGS", "")) - \
        _cfg_flag_names((env_extra or {}).get("RUSTDOCFLAGS", ""))
    off = set(rustc_only) | {f'feature = "{n}"' for n in rustc_only}
    return ({p for p in cfg_true if p not in off},
            set(cfg_false) | off)


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
