"""Disk-preserving fill merge (P6 relocation).

`merge_fills_preserving_existing` splices the model's fn BODIES into the on-disk
file without ever losing the disk's structure (imports/types/macros/attrs/prior
fills). Disk is the structural base; only `unimplemented!()` bodies are replaced,
matched by signature line. Pure text→text; fn boundaries come from the structured
`fn_headers` scanner with explicit brace-depth body spans (no regex on source).
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.rust_fn_headers import fn_headers


def merge_fills_preserving_existing(disk_text: str, model_text: str) -> str:
    """Merge model-emitted file with the on-disk file.

    Strategy: disk_text is the structural base (skeleton + any prior fills
    + all imports/types/macros/attributes). We walk it function-by-function
    and ONLY replace `unimplemented!()` bodies with the corresponding body
    from model_text. Everything else from disk is preserved verbatim —
    imports, type definitions, module declarations, attributes, comments.

    This is the safe direction: the model can emit a thin file that drops
    the surrounding context, but we never lose what's already on disk.
    The model contributes function bodies only.

    Matching is by function signature line. If the model didn't return a
    given function (or returned it with `unimplemented!()` still), the disk
    body is kept — Phase D will pick up any residuals.
    """
    def extract_fns(text: str) -> list[tuple[str, str, int, int]]:
        """Return [(signature_line, body, body_start, body_end), ...] for each fn.

        body_start is the offset of the first char inside the opening `{`.
        body_end is the offset of the closing `}` itself (exclusive end of body).
        """
        out: list[tuple[str, str, int, int]] = []
        for h in fn_headers(text, line_anchored=False):
            body_start = h.brace_open + 1  # right after the opening `{`
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue  # malformed; skip
            body_end = i - 1  # position of the matching '}'
            body = text[body_start:body_end]
            sig_line = text[h.start:body_start].strip()
            out.append((sig_line, body, body_start, body_end))
        return out

    disk_fns = extract_fns(disk_text)
    if not disk_fns:
        # Disk has no functions to fill — either an empty or
        # non-Rust-source-ish file. Don't risk overwriting structure;
        # return disk verbatim.
        return disk_text

    # Build a map from signature → model's body, for any signature the
    # model returned that ALSO doesn't look like a fresh unimplemented stub.
    # (If the model regurgitated `unimplemented!()` for some function, that
    # adds no value over what's already on disk.)
    model_bodies: dict[str, str] = {}
    for sig, body, _bs, _be in extract_fns(model_text):
        if is_unimplemented_body(body):
            continue
        model_bodies[sig] = body

    if not model_bodies:
        return disk_text  # model gave us nothing usable

    # Walk disk fns IN REVERSE so character offsets stay valid as we splice.
    # Replace body slice only when disk's body is unimplemented AND model
    # has a real body for the same signature.
    out = disk_text
    for sig, body, body_start, body_end in reversed(disk_fns):
        if not is_unimplemented_body(body):
            continue  # already filled — never overwrite
        new_body = model_bodies.get(sig)
        if new_body is None:
            continue  # model didn't return this function
        out = out[:body_start] + new_body + out[body_end:]
    return out
