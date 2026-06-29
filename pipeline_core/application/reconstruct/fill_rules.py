"""Pure rules of the per-fn fill loop (P2 slice 2, relocated verbatim from
pipeline.py; `_`-prefixes dropped on promotion).

`FAILED_ATTEMPT_SENTINEL` is a SEAM constant: skeleton.rs's
`is_failed_attempt_stub` matches the same string — change them together.
"""

from __future__ import annotations

import time
from pathlib import Path

FAILED_ATTEMPT_SENTINEL = "// SKELETON_STUB_FAILED_ATTEMPT"


def is_unimplemented_body(body_text: str) -> bool:
    """True if a function body looks like our `unimplemented!()` stub.

    Recognises three forms:
      1. Plain stub: contains `unimplemented!`/`todo!` and body < 60 chars.
         `todo!()` is the same "not implemented" panic the model can emit —
         accepting it as a real fill is a false success.
      2. Annotated stub: contains the SKELETON_STUB_FAILED_ATTEMPT sentinel
         (used by the fix-loop exhaustion path to preserve the failed attempt
         and cargo error as comments while keeping the fn callable)
    """
    stripped = body_text.strip().strip("{").strip("}").strip()
    if FAILED_ATTEMPT_SENTINEL in stripped:
        return True
    return (("unimplemented" in stripped or "todo!" in stripped)
            and len(stripped) < 60)


def make_failed_attempt_body(attempted_body: str, error_excerpt: str,
                               fix_attempts: int) -> str:
    """Build a fn body that documents a failed fill attempt as comments and
    ends in `unimplemented!()` so the crate still compiles.

    The body carries the SKELETON_STUB_FAILED_ATTEMPT sentinel so a later
    pass's `_scan_unimplemented_functions` re-discovers this fn as still
    needing a fill — but the model sees the failure history in context.
    """
    ts = int(time.time())
    lines = [
        FAILED_ATTEMPT_SENTINEL,
        f"// Previous attempt failed compilation after {fix_attempts} fix-loop "
        "attempts.",
        "// Body reverted to `unimplemented!()` so the crate compiles. A later",
        "// pass will retry — by then more sibling functions in this file will",
        "// be implemented, giving the model richer context.",
        "//",
        "// ATTEMPTED BODY:",
    ]
    if attempted_body:
        for ln in attempted_body.splitlines()[:60]:
            lines.append(f"//   {ln}")
    else:
        lines.append("//   (no body captured)")
    lines.append("//")
    lines.append("// CARGO ERROR (focused excerpt):")
    if error_excerpt:
        for ln in error_excerpt.splitlines()[:30]:
            lines.append(f"//   {ln}")
    else:
        lines.append("//   (no error captured)")
    lines.append(f"// Timestamp: {ts}")
    lines.append("unimplemented!()")
    return "\n".join(lines)



def module_of(rel_file: str) -> str:
    """Rust module identity of a source file: 'src/ptr.rs' -> 'ptr'. This is the
    granularity PIPELINE_CONVERGE_MODULE names: phase-c fills operate per-file, so
    the converge exemption must key on the file under fill, not the crate dir."""
    return Path(rel_file).stem



def failure_hit_unfilled_stub(detail: str) -> bool:
    """True when a test FAILURE is a panic from a still-unfilled `unimplemented!()`
    / `todo!()` stub rather than a real assertion mismatch.

    Used by the per-fn fill loop (#3): a multi-fn test attributed to fn A can fail
    because a SIBLING fn it also calls is still a stub — not because A is wrong.
    Runtime panic attribution is first-panic-order, so we can't tell from the
    frontier alone; the panic MESSAGE is the reliable signal. The fn just filled
    is non-stub (splice rejects stub bodies), so a "not implemented" panic here
    came from another, still-unfilled fn → keep A's compiling body and defer the
    verdict to the outer TDD loop. An assertion mismatch means A is wrong → don't.
    """
    d = detail or ""
    return ("not implemented" in d) or ("not yet implemented" in d)
