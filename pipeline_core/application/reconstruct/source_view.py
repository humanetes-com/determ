"""Prompt-facing source VIEWS — string→string transforms that shape how a Rust
file is shown to the model (never mutate the on-disk file).

`condense_rust_to_signatures` reduces a file to its structural skeleton (type
defs + fn signatures, bodies elided) to fit more context; it prefers grep-ast
(tree-sitter) and falls back to a domain brace scanner. `annotate_file_view`
marks each `unimplemented!()` stub (TARGET vs later-filled) so the model knows
which bodies it must write now vs may rely on. Both depend on the fill-rule
`is_unimplemented_body` / external parser, so they live in application, not
domain.
"""
from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.doc_render import condense_doc_attrs, strip_crate_attrs
from pipeline_core.domain.impl_block_ranges import parse_impl_block_ranges
from pipeline_core.domain.models import ConstRecord, FnSite, TypeRecord
from pipeline_core.domain.rust_fn_headers import HeaderMatch, fn_headers
from pipeline_core.domain.rust_idents import (
    canon_container,
    canon_token_text,
    contains_fn_decl,
)
from pipeline_core.domain.rust_scan import is_type_decl_line


def public_api_from_source(
    content: str,
    *,
    enumerate_types: Callable[[Path], list[TypeRecord] | None],
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
    enumerate_consts: Callable[[Path], list[ConstRecord] | None],
) -> str:
    """Render a single Rust source string's public API surface using the syn
    extractor — no regex on Rust source (Hard Rule). The extractor is path-based,
    so write the string to a throwaway crate tree and enumerate types / fns /
    consts over it (injected callables).

    Output mirrors the legacy format: `pub struct Name { f: T, ... }`, `pub enum
    Name { A, B }`, `pub trait Name`, full pub fn signatures, `pub const NAME: T`.

    Gated on `is_exactly_pub` (strictly `pub`), not `is_pub` (which folds
    `pub(crate)`/`pub(super)` in) — this renders the literal text "pub ...", so
    a `pub(crate)` item would otherwise show up as a factually wrong claim
    about the crate's real external API.
    """
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as _d:
        _src = Path(_d) / "src"
        _src.mkdir()
        (_src / "lib.rs").write_text(content, encoding="utf-8")
        _td = Path(_d)
        types = enumerate_types(_td) or []
        fns = enumerate_fns(_td) or []            # dicts (engine contract)
        consts = enumerate_consts(_td) or []

    for t in types:
        # Conjunction, not is_exactly_pub alone: the field defaults True when
        # absent (pre-field artifact / stale extractor), and alone that would
        # render fully-PRIVATE types as `pub` — is_pub can only narrow.
        if not (t.is_pub and t.is_exactly_pub):
            continue
        if t.kind == "struct":
            if t.fields:
                fields = ", ".join(f"{f.name}: {f.ty}" for f in t.fields)
                lines.append(f"pub struct {t.name}{t.generics} {{ {fields} }}")
            else:
                lines.append(f"pub struct {t.name}{t.generics}")
        elif t.kind == "enum":
            if t.variants:
                lines.append(f"pub enum {t.name}{t.generics} {{ {', '.join(t.variants)} }}")
            else:
                lines.append(f"pub enum {t.name}{t.generics}")
        elif t.kind == "trait":
            lines.append(f"pub trait {t.name}")

    for f in fns:
        # Missing key defaults True (matches the record classes' from_dict) so
        # a stale artifact degrades to pre-field behaviour, never to "no fns".
        if f.get("is_pub") and f.get("is_exactly_pub", True):
            # sig_text starts at the `fn`/`unsafe`/`async` keyword (the extractor's sig span
            # excludes the visibility), so prepend `pub ` to match the API-listing convention.
            sig = " ".join((f.get("sig_text") or "").split())
            if sig:
                lines.append(sig if sig.startswith("pub ") else f"pub {sig}")

    for c in consts:
        if c.is_pub:
            lines.append(f"pub const {c.name}: {c.ty}")

    return "\n".join(lines)


def condense_rust_to_signatures(source: str, filename: str = "code.rs") -> str:
    """Reduce a Rust source file to structural declarations (type definitions +
    function signatures); fn bodies become ``{ /* ... */ }``. Used by TASK 0b
    (fit more files in context) and TASK 0c attempt history (compress failed
    test blocks). Prefers grep-ast (tree-sitter); falls back to brace-counting.
    """
    try:
        from grep_ast import TreeContext
        tc = TreeContext(filename, source, color=False, line_number=False,
                         show_top_of_file_lines=0, margin=0)
        tc.add_lines_of_interest(set(range(1, source.count('\n') + 2)))
        tc.add_context()
        result = tc.format()
        if result and len(str(result).strip()) > 20:
            return str(result)
    except Exception:
        pass

    # Fallback: brace-counting line scanner.
    lines = source.splitlines(keepends=True)
    result_lines: list[str] = []
    pending_sig: list[str] = []
    depth = 0
    skip_until_depth: int | None = None

    for line in lines:
        if skip_until_depth is not None:
            depth += line.count('{') - line.count('}')
            if depth <= skip_until_depth:
                skip_until_depth = None
            continue

        stripped = line.strip()
        net = line.count('{') - line.count('}')

        # A multi-line signature is open: its continuation lines are ordinary
        # text (`error: E,`, `) -> Self`, `where`), so they must be ACCUMULATED,
        # not tested for fn-ness. It terminates at the body's `{` or, for a
        # bodyless trait/extern declaration, at `;`.
        if pending_sig:
            if '{' in line and net > 0:
                first = pending_sig[0]
                indent = first[:len(first) - len(first.lstrip())]
                head = line[:line.rindex('{')].rstrip()
                result_lines.extend(pending_sig)
                if head:
                    result_lines.append(head + '\n')
                result_lines.append(indent + '{ /* ... */ }\n')
                pending_sig = []
                skip_until_depth = depth
                depth += net
                continue
            pending_sig.append(line)
            depth += net
            if stripped.endswith(';'):
                result_lines.extend(pending_sig)
                pending_sig = []
            continue

        is_fn = contains_fn_decl(stripped) and not stripped.startswith('//')
        is_type_kw = is_type_decl_line(line)

        if is_fn and not is_type_kw and '{' in line and net > 0:
            last_brace = line.rindex('{')
            result_lines.append(line[:last_brace].rstrip() + ' { /* ... */ }\n')
            skip_until_depth = depth
            depth += net
        elif (is_fn and not is_type_kw and '{' not in line
                and not stripped.endswith(';')):
            # The body's `{` lands on a later line, after a multi-line parameter
            # list, a long generic list, or a `where` clause. Without this arm
            # the declaration was emitted verbatim and the WHOLE BODY followed
            # it — and since only long/complex fns are written this way, the
            # leak selected precisely the hardest fns in the crate.
            pending_sig.append(line)
            depth += net
        else:
            depth += net
            result_lines.append(line)

    result_lines.extend(pending_sig)   # unterminated tail: never silently drop
    return ''.join(result_lines)


def target_header_starts(text: str, headers: list[HeaderMatch],
                         target: FnSite, sig_text: str) -> frozenset[int]:
    """The header starts in ``text`` that ARE the target definition.

    A name alone does not name a definition: one file routinely holds several
    same-named fns (a `provide` per `StdError` impl, a `fmt` per Debug/Display
    impl). The tiers are the splicer's (`locate_fn_by_record`): name, then the
    canonical container of the impl block that encloses the header, then the
    signature. A tier narrows only when something survives it, so a target the
    text cannot place is still marked by name, never left unmarked.

    Run `240506-87538963` (2026-10-19), task 3 retry: the prompt for
    `provide` in `impl StdError for ContextError<C, E>` marked BOTH `provide`
    definitions TARGET, the reply was spliced into that one, and the test kept
    panicking in the stub of `impl<C> StdError for ContextError<C, Error>`
    while the retry walked 37 further fns three hops out."""
    named = [h for h in headers if h.name == target.fn_name]
    if len(named) < 2:
        return frozenset(h.start for h in named)
    ranges = parse_impl_block_ranges(text)

    def _enclosing(pos: int) -> str:
        inner = [r for r in ranges if r.body_open < pos < r.body_close]
        if not inner:
            return ""
        r = max(inner, key=lambda r: r.body_open)
        return canon_container(text[r.header_start:r.body_open])

    want = canon_container(target.container)
    by_impl = [h for h in named if _enclosing(h.start) == want]
    cands = by_impl or named
    if sig_text and len(cands) > 1:
        want_sig = canon_token_text(sig_text)
        by_sig = [h for h in cands
                  if canon_token_text(text[h.start:h.brace_open]).startswith(want_sig)]
        cands = by_sig or cands
    return frozenset(h.start for h in cands)


def annotate_file_view(file_text: str, target: FnSite, sig_text: str, *,
                       doc_keep_full_limit: int) -> str:
    """A view of ``file_text`` where every unimplemented stub is prefixed with a
    status comment (the TARGET definition marked distinctly), so the model treats
    stubs as "to-be-filled later" rather than permanent gaps. The target is a
    DEFINITION (:func:`target_header_starts`), not a name: a same-named
    definition in another impl is shown as any other stub. Doc comments are kept
    in FULL when the view fits under ``doc_keep_full_limit`` (the crate's own
    usage examples are concrete guidance); only large views are condensed.
    Non-doc crate attrs are always dropped. PROMPT view only — the on-disk file
    is untouched."""
    # Big view → condense docs to protect budget; else drop only #![...] crate attrs.
    file_text = (condense_doc_attrs(file_text) if len(file_text) > doc_keep_full_limit
                 else strip_crate_attrs(file_text))
    headers = fn_headers(file_text, line_anchored=False)
    targets = target_header_starts(file_text, headers, target, sig_text)
    out: list[str] = []
    last_fn_end = 0
    for h in headers:
        sig_start = h.start
        body_start = h.brace_open + 1
        depth = 1
        j = body_start
        while j < len(file_text) and depth > 0:
            c = file_text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth != 0:
            continue
        is_stub = is_unimplemented_body(file_text[body_start:j - 1])
        line_start = file_text.rfind("\n", 0, sig_start) + 1
        indent = ""
        for ch in file_text[line_start:sig_start]:
            if ch in (" ", "\t"):
                indent += ch
            else:
                break
        out.append(file_text[last_fn_end:sig_start])
        if sig_start in targets:
            annot = (f"{indent}// >>> TARGET — implement THIS function below; the body is "
                     f"currently `unimplemented!()`. <<<\n")
        elif is_stub:
            annot = (f"{indent}// SKELETON STUB — body will be filled in a separate "
                     f"call; you may rely on it existing (signature is fixed).\n")
        else:
            annot = ""
        out.append(annot)
        out.append(file_text[sig_start:j])
        last_fn_end = j
    out.append(file_text[last_fn_end:])
    return "".join(out)
