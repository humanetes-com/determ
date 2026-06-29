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

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.doc_render import condense_doc_attrs, strip_crate_attrs
from pipeline_core.domain.rust_fn_headers import fn_headers
from pipeline_core.domain.rust_idents import contains_fn_decl
from pipeline_core.domain.rust_scan import is_type_decl_line


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
    depth = 0
    skip_until_depth: int | None = None

    for line in lines:
        if skip_until_depth is not None:
            depth += line.count('{') - line.count('}')
            if depth <= skip_until_depth:
                skip_until_depth = None
            continue

        stripped = line.strip()
        is_fn = contains_fn_decl(stripped) and not stripped.startswith('//')
        is_type_kw = is_type_decl_line(line)

        net = line.count('{') - line.count('}')

        if is_fn and not is_type_kw and '{' in line and net > 0:
            last_brace = line.rindex('{')
            result_lines.append(line[:last_brace].rstrip() + ' { /* ... */ }\n')
            skip_until_depth = depth
            depth += net
        else:
            depth += net
            result_lines.append(line)

    return ''.join(result_lines)


def annotate_file_view(file_text: str, target_fn: str, *,
                       doc_keep_full_limit: int) -> str:
    """A view of ``file_text`` where every unimplemented stub is prefixed with a
    status comment (the TARGET fn marked distinctly), so the model treats stubs
    as "to-be-filled later" rather than permanent gaps. Doc comments are kept in
    FULL when the view fits under ``doc_keep_full_limit`` (the crate's own usage
    examples are concrete guidance, FR-026 amended); only large views are
    condensed. Non-doc crate attrs are always dropped. PROMPT view only — the
    on-disk file is untouched."""
    # Big view → condense docs to protect budget; else drop only #![...] crate attrs.
    file_text = (condense_doc_attrs(file_text) if len(file_text) > doc_keep_full_limit
                 else strip_crate_attrs(file_text))
    out: list[str] = []
    last_fn_end = 0
    for h in fn_headers(file_text, line_anchored=False):
        fn_name = h.name
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
        if fn_name == target_fn:
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
