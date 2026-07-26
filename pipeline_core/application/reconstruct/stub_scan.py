"""Scan a workspace src tree for functions whose body is still a stub
(`unimplemented!()` / annotated failed-attempt).

Prefers the syn-based extractor (injected `enumerate_fns`) — it catches every
fn rustc would consider (free fns, impl methods, trait-method defaults,
inline-compressed declarations, cfg-attributed fns). Falls back to a brace-
scanning fn-header walk when the extractor is unavailable (test-fixture path,
partial setup) — that fallback reads source text but parses it structurally
(`fn_headers` + brace counting), never by regex.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import fn_headers


def scan_unimplemented_functions(
    src_dir: Path,
    *,
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
) -> list[FnLocator]:
    """Return [(file_rel_path, fn_name), ...] for every fn whose body is still
    `unimplemented!()` (or an annotated stub).

    This is the LEGACY name-only return form. New code should use the
    extractor records directly to also get container + signature, which are
    needed to disambiguate same-named fns within a single file.
    """
    recs = enumerate_fns(src_dir)
    if recs is not None:
        # `has_unimplemented_body` is the extractor's syntactic test and only
        # sees a literal stub. A body that is nothing but a nested copy of its
        # own fn contains no `unimplemented!()`, so the extractor calls it
        # filled while the fn does nothing at all — re-check it here so an
        # already-corrupted tree is re-attempted instead of staying invisible.
        return [FnLocator(r["file"], r["fn_name"]) for r in recs
                if r.get("has_unimplemented_body")
                or is_unimplemented_body(str(r.get("body_text", "")),
                                         str(r.get("fn_name", "")))]
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
            if is_unimplemented_body(body):
                out.append(FnLocator(str(p.relative_to(src_dir)), fn_name))
    return out
