"""Scan a workspace src tree for functions whose body is still a stub
(`unimplemented!()` / annotated failed-attempt).

Prefers the syn-based extractor (injected `enumerate_fns`) — it catches every
fn rustc would consider (free fns, impl methods, trait-method defaults,
inline-compressed declarations, cfg-attributed fns). Falls back to a brace-
scanning fn-header walk when the extractor is unavailable (test-fixture path,
partial setup) — that fallback reads source text but parses it structurally
(`fn_headers` + brace counting), never by regex.

The result is a :class:`StubInventory`, never a bare list of locators: the
extractor reports one record per implementation SITE, several sites share one
dispatch locator (`fmt` in every Debug/Display impl), and a list that repeated
the locator once per site made every consumer decide the cardinality for
itself — the cluster-entry re-seed of run `2973-66e50ad9` (2026-09-04) iterated
that list, handed the splicer `src/context.rs::ext_context` twice, and left the
file unparseable. The inventory carries both cardinalities by name.

A stub in an ORACLE file is never returned, by either route. A stub is a fill
target by definition, and no fill may rewrite a test; a stub that appears there
means something already damaged the oracle, so handing it back would ask the
model to re-author the specification and call the result progress. Observed
2026-08-01: a layout retrigger reverted `tests/common/mod.rs::bail_error` and
`tests/test_context.rs::make_chain` (a 29-line fixture) to `unimplemented!()`,
which this scan would then have offered as ordinary work.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.oracle_immutability import is_oracle_path
from pipeline_core.domain.rust_fn_headers import fn_headers
from pipeline_core.domain.stub_inventory import StubInventory

EnumerateFns = Callable[[Path], "list[dict[str, Any]] | None"]


def is_stub_record(r: Mapping[str, Any]) -> bool:
    """The extractor's syntactic flag widened by the body rule.

    `has_unimplemented_body` only sees a literal stub. A body that is nothing
    but a nested copy of its own fn contains no `unimplemented!()`, so the
    extractor calls it filled while the fn does nothing at all — re-check it
    here so an already-corrupted tree is re-attempted instead of staying
    invisible. Oracle files are refused before either test."""
    if is_oracle_path(str(r.get("file", ""))):
        return False
    return bool(r.get("has_unimplemented_body")) or is_unimplemented_body(
        str(r.get("body_text", "")), str(r.get("fn_name", "")))


def scan_stub_inventory(
    src_dir: Path, *, enumerate_fns: EnumerateFns,
) -> StubInventory:
    """Every stub site under `src_dir`, as the extractor names them.

    Files are keyed relative to `src_dir` — pass the crate root and they read
    `src/error.rs`, which is how cargo prints them and how every identity in
    the loop is spelled. With the extractor unavailable the text walk yields
    sites with no container and `cfg_predicates=None` (no authoritative list;
    the only case a caller may read gates out of the surrounding text)."""
    recs = enumerate_fns(src_dir)
    if recs is not None:
        return StubInventory.from_records(recs, keep=is_stub_record)
    out: list[FnLocator] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for h in fn_headers(text, line_anchored=False):
            fn_name = h.name
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
            body = text[body_start:i - 1]
            rel = str(p.relative_to(src_dir))
            if is_unimplemented_body(body) and not is_oracle_path(rel):
                out.append(FnLocator(rel, fn_name))
    # The text walk cannot see containers, so same-named fns in one file
    # collapse to one site here; that is a limitation of the fallback, not
    # of the type, and `cfg_predicates=None` marks every site it produced.
    return StubInventory.of_units(out, cfg_predicates=None)
