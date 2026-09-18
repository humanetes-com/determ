"""Sibling fn body collection for per-fn prompt injection.

Extracted from pipeline._collect_sibling_filled_bodies. Pure: operates on
source text, no I/O.
"""

from __future__ import annotations

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.rust_fn_headers import fn_headers


def collect_sibling_filled_bodies(
    file_text: str,
    target_fn_name: str,
    target_container: str,
    max_siblings: int = 3,
    max_body_chars: int = 800,
) -> str:
    """Find already-filled fns with the same name in *other* impl blocks.

    Returns a formatted block ready for ``<SIBLING_REFERENCE>`` injection,
    or an empty string when no usable siblings exist.

    Siblings are identified by name; the target itself is excluded via a
    container-fragment heuristic (the container string appears in the ~200
    chars preceding the fn declaration).
    """
    out: list[str] = []
    count = 0
    for h in fn_headers(file_text, line_anchored=False):
        if h.name != target_fn_name:
            continue
        sig_start = h.start
        body_start = h.brace_open + 1
        depth = 1
        i = body_start
        while i < len(file_text) and depth > 0:
            ch = file_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        body_end = i - 1
        body = file_text[body_start:body_end].strip()
        if not body or is_unimplemented_body(body):
            continue
        if target_container:
            context_above = file_text[max(0, sig_start - 200):sig_start]
            if target_container.strip() in context_above:
                continue
        sig = file_text[sig_start:body_start].strip().rstrip("{").rstrip()
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + "\n    /* ...truncated... */"
        out.append(f"{sig} {{\n{body}\n}}")
        count += 1
        if count >= max_siblings:
            break
    return "\n\n".join(out)
