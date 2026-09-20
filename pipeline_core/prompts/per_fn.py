"""Per-fn prompt builders — initial fill + compile-fix re-prompt.

Moved verbatim from pipeline.py. Pure text→text; impure collaborators
come from :mod:`pipeline_core.prompts.runtime`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.domain.construction_recipe import construction_recipe_prefix
from pipeline_core.domain.coverage_slice import instruction_opener
from pipeline_core.domain.crash_site import crash_evidence_prefix
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.last_error import LastErrorKind, last_error_prefix, resolve_kind
from pipeline_core.prompts.context import PerFnFixSections, PerFnSections
from pipeline_core.prompts.runtime import (
    annotate_file_view,
    promoted_error_rules,
    repo_spec_section,
)
from pipeline_core.prompts.sections import tag_block


def build_per_fn_prompt(
    file_text: str,
    fn_name: str,
    sig_text: str,
    test_blocks: list[tuple[str, str]],
    module_name: str,
    cluster_name: str,
    file_rel: str,
    sections: PerFnSections,
    *,
    fr_path: Path | None = None,
) -> str:
    """Small focused prompt for one fn fill. No crate-wide signature index,
    no full FR, no genesis — only what the model needs to implement THIS fn.

    `sections`: every injected section as a frozen no-default record — a
    forgotten section is a TypeError at construction, not a silent "".
    """
    macros_section = sections.macros
    notes_section = sections.notes
    container = sections.container
    types_section = sections.types
    invariants_section = sections.invariants
    siblings_section = sections.siblings
    crate_idioms_section = sections.crate_idioms
    tests_section = ""
    if test_blocks:
        parts = []
        for path, txt in test_blocks:
            parts.append(f'<TEST path="{path}">\n{txt}\n</TEST>')
        tests_section = "\n\n".join(parts)
    else:
        tests_section = ("(no tests directly reference this fn — it may be "
                         "invoked indirectly via a macro; consult MACROS below)")
    annotated = annotate_file_view(file_text, fn_name)
    notes_block = tag_block("MODEL_NOTES", notes_section)
    crash_block = tag_block(
        "CRASH_EVIDENCE", sections.crash_evidence,
        prefix=crash_evidence_prefix(
            "A test binary CRASHED and this fn is on the faulting or "
            "origin stack. This is runtime evidence from the actual "
            "crash, stronger than any failing-test guess:\n",
            (fn_name,)))
    # Concrete RIGHT usage examples harvested from the real
    # lib's doc-tests/examples — teaches the API SHAPE by example. After notes,
    # before the file context (same slot the archetype prescribes).
    usage_block = tag_block(
        "USAGE_EXAMPLES", sections.usage_examples,
        prefix="Real, working uses of this crate's public API (harvested from "
               "the library's own tests/examples). Match how the API is called "
               "here:\n```rust\n", suffix="\n```")
    macros_block = tag_block("MACROS", macros_section)
    # Type definitions for types the fn touches. The model often
    # confuses Own/Ref/ErrorImpl fields with methods (writes self.ptr.as_ref
    # instead of self.ptr.as_ref()); listing each type's actual fields and
    # methods makes this unambiguous.
    types_block = tag_block("TYPES_IN_SCOPE", types_section)
    # Methods the fn's frozen where-bounds make callable — steer the body to
    # a bound-provided trait method instead of a mis-bound free constructor
    # (the E0277 thrash root).
    bound_methods_block = tag_block("BOUND_METHODS", sections.bound_methods)
    blueprint_block = tag_block(
        "DESIGN_BLUEPRINT", sections.blueprint,
        prefix="YOUR crate design, written by you before filling began — this\n"
               "body must agree with it:\n")
    # Cluster-aware macro invariants: which arm of each cfg-gated macro
    # is active for THIS cluster. Without this the model defends against
    # cases that can't occur (e.g. `unwrap_or(&default)` when the active
    # macro always returns Some).
    invariants_block = tag_block("CLUSTER_INVARIANTS", invariants_section)
    # Sibling reference (initial-prompt variant).
    siblings_block = tag_block(
        "SIBLING_REFERENCE", siblings_section,
        prefix=f"Fns with the same name `{fn_name}` in OTHER impl blocks of "
               "this file (already filled — your TARGET should follow the "
               "same structural pattern):\n```rust\n",
        suffix="\n```")
    # Trait-contract tutorial: catalog driver injects a std-trait table;
    # generic (default) injects nothing — the impl header and the failing
    # test are the evidence, the fill model is the language expert.
    trait_block = tag_block(
        "TRAIT_CONTRACT", language_driver().trait_contract(container))
    # Crate idioms — recurring patterns mined from already-filled fns in
    # THIS file (same section the per-impl prompt carries).
    idioms_block = tag_block(
        "CRATE_IDIOMS", crate_idioms_section,
        prefix="Patterns this file already uses — design your fn to MATCH "
               "these idioms whenever its situation matches one of them. Do "
               "NOT invent a new approach when one clearly applies.\n")
    # Promoted recurring errors (E0xxx codes seen 3+ times this run).
    # Passes fr_path so past notes tagged with active codes get surfaced.
    recurring_block = tag_block("RECURRING_ERRORS", promoted_error_rules(fr_path))
    # Repository Specifications: FR + genesis + API, relevance-selected and
    # budget-capped — every code-producing prompt carries the crate's specs.
    repo_spec_block = tag_block(
        "REPOSITORY_SPECIFICATIONS",
        repo_spec_section(fr_path, file_rel=file_rel, fn_name=fn_name,
                          container=container),
        prefix="The crate's own specification documents (functional "
               "requirements, build rules, API ground truth). When your plan "
               "conflicts with them, follow the spec.\n")
    container_line = f"In: {container}\n" if container else ""
    recipe_part = construction_recipe_prefix(fn_name, container, sig_text)
    identity_part = (
        f"{sections.identity_must}\n\n" if sections.identity_must.strip() else "")
    coverage_part = instruction_opener(file_rel, (fn_name,))
    return f"""Implement ONE Rust function so its tests pass.

Crate: {module_name} | Cluster: {cluster_name} | File: {file_rel} | Fn: {fn_name}
{container_line}Signature (verbatim, do not change):
{sig_text}

{recipe_part}{identity_part}{coverage_part}<RUST_GOTCHAS>
{language_driver().gotchas_summary()}
</RUST_GOTCHAS>
{crash_block}{repo_spec_block}{blueprint_block}{siblings_block}{trait_block}{types_block}{bound_methods_block}{invariants_block}{idioms_block}{recurring_block}{notes_block}{usage_block}
FILE — stubs are annotated. SKELETON STUB = will be filled later (rely on
the signature). TARGET = implement now.

<FILE path="{file_rel}">
{annotated}
</FILE>
{macros_block}
Tests (read-only):

{tests_section}

Output:
  <BODY>
  body without surrounding braces
  </BODY>

Rules:
- Body only — no sig, no markdown, no use statements inside body.
- Use fully-qualified paths (std::fmt::Write, crate::Error) for symbols
  not in this file's `use` block.
- No unimplemented!()/todo!()/panic-only bodies. Omit <BODY> if you can't
  implement; a later pass retries.
- SKELETON STUB fns are part of the CONTRACT. If a SKELETON STUB fn's
  signature matches what you need, CALL it — that is the intended path,
  not a hint you should reinvent. The pipeline will fill the stub
  separately. Reinventing what a sibling stub already promises is the
  most common cause of fix-loop divergence.
"""


def build_per_fn_fix_prompt(
    file_text: str,
    fn_name: str,
    sig_text: str,
    test_blocks: list[tuple[str, str]],
    module_name: str,
    cluster_name: str,
    file_rel: str,
    prev_body: str,
    error_excerpt: str,
    sections: PerFnFixSections,
    *,
    allow_sig_change: bool = False,
    fr_path: Path | None = None,
    attempt_history: list[dict[str, Any]] | None = None,
    kind: LastErrorKind | None = None,
) -> str:
    """Re-prompt for a fn whose previous fill failed.

    ``kind`` is the measurement of that failure. A rustc-accepted body
    that then reddened or aborted is not a cargo-check miss: the opener
    and the excerpt tag follow :func:`last_error_prefix`. Omit ``kind``
    (or pass COMPILE) and the text stays the cargo-check / COMPILE_ERRORS
    form.

    When `allow_sig_change` is True (fired by the per-fn loop after
    attempt 5), the prompt invites the model to optionally return a
    <SIGNATURE>...</SIGNATURE> block proposing a new sig (e.g. adding a
    `T: 'static` bound). The loop applies it only when the fn is non-pub.

    `attempt_history` is a list of dicts (oldest first), each of:
        {"attempt": int, "body": str, "error_sig": str, "outcome": str}
    Rendered as a <PRIOR_ATTEMPTS> block so the model sees what it has
    already tried (and what failed) rather than re-deriving from scratch.

    `sections`: every injected section as a frozen no-default record.
    """
    macros_section = sections.macros
    notes_section = sections.notes
    container = sections.container
    types_section = sections.types
    invariants_section = sections.invariants
    retrospective_guidance = sections.retrospective_guidance
    siblings_section = sections.siblings
    crate_idioms_section = sections.crate_idioms
    tests_section = ""
    if test_blocks:
        parts = [f'<TEST path="{p}">\n{t}\n</TEST>' for p, t in test_blocks]
        tests_section = "\n\n".join(parts)
    else:
        tests_section = "(no tests directly reference this fn — see MACROS)"
    annotated = annotate_file_view(file_text, fn_name)
    notes_block = tag_block("MODEL_NOTES", notes_section)
    crash_block = tag_block(
        "CRASH_EVIDENCE", sections.crash_evidence,
        prefix=crash_evidence_prefix(
            "A test binary CRASHED and this fn is on the faulting or "
            "origin stack. This is runtime evidence from the actual "
            "crash, stronger than any failing-test guess:\n",
            (fn_name,)))
    usage_block = tag_block(
        "USAGE_EXAMPLES", sections.usage_examples,
        prefix="Real, working uses of this crate's public API (harvested from "
               "the library's own tests/examples). Match how the API is called "
               "here:\n```rust\n", suffix="\n```")
    macros_block = tag_block("MACROS", macros_section)
    # Full detail block for gotchas matching the current error.
    gotchas_block = tag_block(
        "RUST_GOTCHAS_DETAIL", language_driver().gotchas_for_error(error_excerpt))
    container_line = f"In: {container}\n" if container else ""
    recipe_part = construction_recipe_prefix(fn_name, container, sig_text)
    identity_part = (
        f"{sections.identity_must}\n\n" if sections.identity_must.strip() else "")
    coverage_part = instruction_opener(file_rel, (fn_name,))
    types_block = tag_block("TYPES_IN_SCOPE", types_section)
    # Bound-provided method inventory (see initial builder) — surfaced on
    # the fix path where the E0277 thrash happens.
    bound_methods_block = tag_block("BOUND_METHODS", sections.bound_methods)
    blueprint_block = tag_block(
        "DESIGN_BLUEPRINT", sections.blueprint,
        prefix="YOUR crate design, written by you before filling began — this\n"
               "body must agree with it:\n")
    invariants_block = tag_block("CLUSTER_INVARIANTS", invariants_section)
    # Retrospective guidance: surfaced FIRST in the prompt when available —
    # the prior diagnosis is the highest-signal hint.
    _ran = resolve_kind(kind, error_excerpt).compiled
    retro_block = tag_block(
        "RETROSPECTIVE_GUIDANCE", retrospective_guidance,
        prefix=(
            "Prior diagnosis of a body rustc accepted that then failed "
            "at run time (act on this — do NOT repeat the previous "
            "approach):\n"
            if _ran else
            "Prior diagnosis of this fn's compile failures (act on this "
            "— do NOT repeat the previous approach):\n"))
    # Sibling reference: when the same fn name has a sibling impl already
    # filled correctly in this file, hoist it to the top of the prompt. Best
    # source-of-truth for "what does a body for this fn look
    # like" — pattern-matching beats reasoning from scratch.
    siblings_block = tag_block(
        "SIBLING_REFERENCE", siblings_section,
        prefix=f"Fns with the same name `{fn_name}` in OTHER impl blocks of "
               "this file (already filled — your TARGET should follow the "
               "same structural pattern):\n```rust\n",
        suffix="\n```")
    trait_block = tag_block(
        "TRAIT_CONTRACT", language_driver().trait_contract(container))
    # Crate idioms — same section the per-impl / per-fn initial prompts carry.
    idioms_block = tag_block(
        "CRATE_IDIOMS", crate_idioms_section,
        prefix="Patterns this file already uses — design your fn to MATCH "
               "these idioms whenever its situation matches one of them. Do "
               "NOT invent a new approach when one clearly applies.\n")
    recurring_block = tag_block("RECURRING_ERRORS", promoted_error_rules(fr_path))
    repo_spec_block = tag_block(
        "REPOSITORY_SPECIFICATIONS",
        repo_spec_section(fr_path, file_rel=file_rel, fn_name=fn_name,
                          container=container),
        prefix="The crate's own specification documents (functional "
               "requirements, build rules, API ground truth). When your plan "
               "conflicts with them, follow the spec.\n")
    # Prior-attempts block. Show up to the last 3 distinct attempts
    # with their compact error signature, so the model can see the search
    # space it has already explored. Budget capped to keep prompt lean.
    prior_block = ""
    if attempt_history:
        _PRIOR_MAX_ITEMS = 3
        _PRIOR_BODY_CAP = 320
        _PRIOR_ERR_CAP = 700
        recent = attempt_history[-_PRIOR_MAX_ITEMS:]
        rendered: list[str] = []
        for h in recent:
            body_excerpt = (h.get("body") or "").strip()
            if len(body_excerpt) > _PRIOR_BODY_CAP:
                body_excerpt = body_excerpt[:_PRIOR_BODY_CAP] + "..."
            # Cap at a LINE boundary: a rustc diagnostic sliced mid-line
            # ("- th...") is unactionable — the model needs the span arrows
            # and expected/found lines whole (same rule as focus_diagnostics).
            err_excerpt = (h.get("error_sig") or "").strip()
            if len(err_excerpt) > _PRIOR_ERR_CAP:
                _kept: list[str] = []
                _used = 0
                for _ln in err_excerpt.splitlines():
                    if _used + len(_ln) + 1 > _PRIOR_ERR_CAP and _kept:
                        break
                    _kept.append(_ln)
                    _used += len(_ln) + 1
                err_excerpt = "\n".join(_kept) + "\n..."
            outcome = h.get("outcome", "fail")
            attempt_n = h.get("attempt", "?")
            rendered.append(
                f"attempt {attempt_n} → {outcome}\n"
                f"body:\n{body_excerpt}\n"
                f"errors:\n{err_excerpt}"
            )
        if rendered:
            prior_block = (
                "\n<PRIOR_ATTEMPTS>\n"
                "These bodies have already been tried for THIS fn and failed.\n"
                "Do not repeat them — try a structurally different approach.\n\n"
                + "\n---\n".join(rendered)
                + "\n</PRIOR_ATTEMPTS>\n"
            )
    if _ran:
        _opener = (
            "RE-implement ONE Rust function. "
            + last_error_prefix(error_excerpt, kind).rstrip()
            + "\nReturn a corrected body. Don't repeat the previous body."
        )
        _err_open, _err_close = "<LAST_ERROR>", "</LAST_ERROR>"
    else:
        _opener = (
            "RE-implement ONE Rust function. Previous attempt broke cargo check.\n"
            "Diagnose the errors, return a corrected body. Don't repeat the previous body."
        )
        _err_open, _err_close = "<COMPILE_ERRORS>", "</COMPILE_ERRORS>"
    return f"""{_opener}

Crate: {module_name} | Cluster: {cluster_name} | File: {file_rel} | Fn: {fn_name}
{container_line}Signature (verbatim, do not change):
{sig_text}

{recipe_part}{identity_part}{coverage_part}<RUST_GOTCHAS>
{language_driver().gotchas_summary()}
</RUST_GOTCHAS>
{crash_block}{repo_spec_block}{blueprint_block}{retro_block}{siblings_block}{gotchas_block}{trait_block}{types_block}{bound_methods_block}{invariants_block}{idioms_block}{recurring_block}{notes_block}{usage_block}{prior_block}
FILE (previous attempt reverted; stubs annotated):
<FILE path="{file_rel}">
{annotated}
</FILE>
{macros_block}
<PREVIOUS_BODY>
{prev_body}
</PREVIOUS_BODY>

{_err_open}
{error_excerpt}
{_err_close}

Tests:
{tests_section}

Output:
  <BODY>
  corrected body without surrounding braces
  </BODY>
{
((("\nIf the fix REQUIRES a signature change (e.g. adding `T: 'static`, "
   "tightening lifetimes, or relaxing a generic bound), AND the function is "
   "private (not pub), include a `<SIGNATURE>` block AFTER your `<BODY>`:\n"
   "  <SIGNATURE>\n")
  + language_driver().sig_change_example(fn_name)
  + ("  </SIGNATURE>\n"
     "The signature line must end at the opening brace `{{`. We will splice it "
     "in place of the current signature. This option is available only after "
     "multiple failed attempts; use it only when the body cannot be made to "
     "compile under the existing signature."))
 if allow_sig_change else "")
}
Rules:
- Body only — no sig in the BODY block.
- Use fully-qualified paths for symbols not in the file's `use` block.
- No unimplemented!()/todo!()/panic-only. Omit <BODY> if you cannot fix.
{("- Public fns: signature MUST remain verbatim (tests depend on it).\n"
  if allow_sig_change else "")}"""
