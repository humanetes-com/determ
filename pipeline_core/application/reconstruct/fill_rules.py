"""Pure rules of the per-fn fill loop (relocated verbatim from
pipeline.py; `_`-prefixes dropped on promotion).

`FAILED_ATTEMPT_SENTINEL` is a SEAM constant: skeleton.rs's
`is_failed_attempt_stub` matches the same string — change them together.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from enum import Enum
from pathlib import Path

from ... import log
from ...domain.last_error import LastErrorKind
from ...domain.rust_scan import body_is_only_redeclaration

FAILED_ATTEMPT_SENTINEL = "// SKELETON_STUB_FAILED_ATTEMPT"

# `(?s)` DOTALL so a body can span lines. The sig attribute value is in double
# quotes (most common) or single quotes (sometimes used). This parses MODEL
# OUTPUT (the `<FN sig="…">…</FN>` fill protocol), not Rust source — the no-regex
# hard rule is scoped to source parsing.
_FN_BLOCK_PATTERN = re.compile(
    r'<FN\s+sig\s*=\s*(?P<q>["\'])(?P<sig>.+?)(?P=q)\s*>(?P<body>.*?)</FN>',
    re.DOTALL,
)


def is_unimplemented_body(body_text: str, fn_name: str = "") -> bool:
    """True if a function body is not an implementation.

    Recognises three forms:
      1. Plain stub: contains `unimplemented!`/`todo!` and body < 60 chars.
         `todo!()` is the same "not implemented" panic the model can emit —
         accepting it as a real fill is a false success.
      2. Annotated stub: contains the SKELETON_STUB_FAILED_ATTEMPT sentinel
         (used by the fix-loop exhaustion path to preserve the failed attempt
         and cargo error as comments while keeping the fn callable)
      3. Self-redeclaration (needs `fn_name`): the body is nothing but a
         nested copy of the fn itself — what a model returns when it answers
         with the WHOLE item instead of a body. The outer fn is then EMPTY and
         silently returns `()`, but it no longer contains `unimplemented!()`,
         so without this arm it reads as FILLED to every scan and is never
         re-attempted. That is the same "not implemented" state as (1), and
         both the write gate and the stub scan must agree on it — one
         definition, read from both sides.
    """
    stripped = body_text.strip().strip("{").strip("}").strip()
    if FAILED_ATTEMPT_SENTINEL in stripped:
        return True
    if fn_name and body_is_only_redeclaration(body_text, fn_name):
        return True
    return (("unimplemented" in stripped or "todo!" in stripped)
            and len(stripped) < 60)


def extract_fn_blocks(response: str) -> dict[str, str]:
    """Parse `<FN sig="...">body</FN>` blocks from the LLM response.

    Returns {signature → body_text}. The body is the raw content between
    the opening `<FN ...>` and closing `</FN>` markers, trimmed of leading/
    trailing whitespace. The signature must include everything from "fn name"
    to the opening `{` exclusive — matching how `merge_fills_preserving_existing`
    keys its disk-function map.

    Tolerates loosely-formatted responses (extra whitespace, mixed quoting on
    the sig attribute).
    """
    out: dict[str, str] = {}
    stubs_dropped = 0
    for m in _FN_BLOCK_PATTERN.finditer(response):
        sig = m.group("sig").strip()
        body = m.group("body").strip()
        if not sig or not body:
            continue
        # Drop stub bodies so they don't appear successful.
        if is_unimplemented_body(body) or body.strip() in ("todo!()", "todo!();"):
            stubs_dropped += 1
            continue
        out[sig] = body
    if stubs_dropped:
        log.log(f"  [phase-c] dropped {stubs_dropped} stub <FN> block(s) "
                f"from model output (model re-emitted unimplemented!).")
    return out


class AttemptFailure(Enum):
    """How the LAST attempt a fix loop gave up on failed — the axis the
    failed-attempt comment labels the quoted body by.

    A comment that says "did not compile — do not repeat this approach" over
    a body that compiled is a false lesson written into the one file every
    later prompt for that fn quotes. Run `2585062-494e630e` (2026-09-08)
    wrote exactly that over the textbook `Own::new` one-liner after the
    body compiled and the test process aborted in a still-stubbed drop-path
    fn; the next four prompts for the file carried the correct body as the
    thing not to write."""

    COMPILE = "compile"   # rustc rejected it
    TEST = "test"         # compiled; its attributed tests still failed
    ABORT = "abort"       # compiled; the test process died under it


# The LAST_ERROR a behavioural refill's first ask carries: the body on disk
# compiled, and the only evidence is the attributed test source.
REFILL_SEED_EXCERPT = (
    "This body COMPILES but its attributed test(s) below still FAIL. "
    "Rewrite the BEHAVIOR so those test(s) pass; keep the signature "
    "and do not reintroduce `unimplemented!()`.")


def seed_attempt_failure(*, refill: bool,
                         crash_implicated: bool) -> AttemptFailure:
    """How the body a fix loop starts from failed, before this loop has
    measured anything.

    A stub has not failed yet, so COMPILE is the honest default (its first
    ask is the stub prompt, not a fix prompt). A behavioural refill is a
    body rustc already ACCEPTED: its attributed tests fail (TEST), or it is
    on a localized crash stack (ABORT). The same three-way the loop writes
    after a measured attempt. Seeding COMPILE for a refill is how every
    refill's first ask opened `Previous attempt broke cargo check` over a
    compiling body — run `3400-4d0618ba` (2026-09-21), `src/error.rs::
    context`: the model "fixed rustc" into an E0277 and oscillated 78
    minutes."""
    if not refill:
        return AttemptFailure.COMPILE
    return AttemptFailure.ABORT if crash_implicated else AttemptFailure.TEST


def fix_prompt_kind(failure: AttemptFailure, *,
                    quoted_run: bool) -> LastErrorKind:
    """The LastErrorKind a fix prompt is built with — the one mapping.

    ``quoted_run`` is whether the LAST_ERROR excerpt quotes this body's own
    run output (a measured attempt of this loop). A TEST failure with its
    panic lines quoted is REDDENED; the same failure with no run quoted (a
    refill's seed, whose only evidence is the test source) is UNPASSED,
    which names no faulting call — REDDENED there would order the model
    away from "the call the stack below shows" with no stack below. An
    ABORT is CRASHED either way: a crash-implicated seed carries its stack
    in the prompt's CRASH_EVIDENCE."""
    if failure is AttemptFailure.COMPILE:
        return LastErrorKind.COMPILE
    if failure is AttemptFailure.ABORT:
        return LastErrorKind.CRASHED
    return LastErrorKind.REDDENED if quoted_run else LastErrorKind.UNPASSED


def make_failed_attempt_body(
    attempted_body: str, error_excerpt: str, fix_attempts: int, *,
    failure: AttemptFailure,
    stub_sites: Iterable[tuple[str, int]] = (),
    own_panics: Iterable[str] = (),
) -> str:
    """Build a fn body that documents a failed fill attempt as comments and
    ends in `unimplemented!()` so the crate still compiles.

    The body carries the SKELETON_STUB_FAILED_ATTEMPT sentinel so a later
    pass's `_scan_unimplemented_functions` re-discovers this fn as still
    needing a fill. The attempted body is labelled by HOW it failed
    (``failure``): a compile failure is a do-not-repeat — quoting it as a
    hint handed the next fill the recipe that just failed (run
    ``1383168-eb151d09``, ``construct_from_boxed`` quoting
    ``construct_from_std`` on the Box); a test failure is a body to correct,
    not to avoid; an abort names what the aborted binaries printed —
    ``stub_sites`` (`(file, line)` of the still-stubbed fns whose panics
    the capture located, rendered `file:line`) and ``own_panics`` (the
    unit's own crate panics, already rendered) — and never calls the body
    wrong on the abort alone, because an abort through a hollow drop path
    is the tree's verdict, not the body's.
    """
    ts = int(time.time())
    _stubs = ", ".join(f"{f}:{ln}" for f, ln in sorted(set(stub_sites)))
    _own = "; ".join(own_panics)
    if failure is AttemptFailure.COMPILE:
        what = (f"// Previous attempt failed compilation after {fix_attempts} "
                "fix-loop attempts.")
        label = "// ATTEMPTED BODY (did not compile — do not repeat this approach):"
        evidence = "// CARGO ERROR (focused excerpt):"
    elif failure is AttemptFailure.TEST:
        what = (f"// Previous attempt COMPILED but its attributed tests still "
                f"failed after {fix_attempts} fix-loop attempts.")
        label = ("// ATTEMPTED BODY (compiled; its tests failed — correct it "
                 "against the excerpt below, do not avoid it):")
        evidence = "// TEST FAILURE (excerpt):"
    else:
        what = (f"// Previous attempt COMPILED; the test process ABORTED under "
                f"it after {fix_attempts} fix-loop attempts.")
        if _own:
            label = ("// ATTEMPTED BODY (compiled; the unit's own panic came "
                     f"first — {_own} — and a still-stubbed fn"
                     + (f" at {_stubs}" if _stubs else " on the drop path")
                     + " then panicked during unwinding; fix the first panic, "
                     "the abort is the tree's):")
        elif _stubs:
            label = ("// ATTEMPTED BODY (compiled; the abort came from "
                     f"still-stubbed fn(s) at {_stubs} reached during "
                     "unwinding — this body was NOT shown wrong and may be "
                     "correct as written):")
        else:
            label = ("// ATTEMPTED BODY (compiled; the test process aborted "
                     "with no panic located — likely undefined behaviour in "
                     "this body):")
        evidence = "// ABORT EVIDENCE (excerpt):"
    lines = [
        FAILED_ATTEMPT_SENTINEL,
        what,
        "// Body reverted to `unimplemented!()` so the crate compiles. A later",
        "// pass will retry — by then more sibling functions in this file will",
        "// be implemented, giving the model richer context.",
        "//",
        label,
    ]
    if attempted_body:
        for ln in attempted_body.splitlines()[:60]:
            lines.append(f"//   {ln}")
    else:
        lines.append("//   (no body captured)")
    lines.append("//")
    lines.append(evidence)
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
