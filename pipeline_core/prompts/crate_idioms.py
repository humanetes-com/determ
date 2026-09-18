"""`<CRATE_IDIOMS>` prompt section — pure crate-pattern mining, relocated out of pipeline.py.

The strongest reconstruction signal for a stub is how the SAME crate already does
things in its other (already-filled) fns: recurring `Self::name(args)` constructors,
fn-pointer dispatch `(vtable.method)(this)`, single-line `unsafe { … }` chains, and
non-trivial `Struct { … }` literal construction. `extract_crate_idioms` scans the
file's filled bodies, ranks the highest-signal idioms (biased toward symbols the
target stub's signature already mentions), and renders a compact cheat-sheet the
per-impl prompt injects alongside the generic gotchas.

Generic over crates — no crate-specific code; smaller crates simply emit fewer
idioms. The `_IDIOM_*`-style detectors here mine recurring TEXT shapes (fuzzy
pattern-mining, not structured-fact extraction) so they are NOT a no-regex-on-source
violation. Pure: operates on `ImplBlock` domain
records, no I/O.
"""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.models import ImplBlock
from pipeline_core.domain.rust_idents import identifier_tokens, mask_identifiers
from pipeline_core.domain.rust_idioms import (
    fn_pointer_field_calls,
    self_constructor_calls,
    struct_literals,
    unsafe_block_chains,
)


def extract_filled_fn_bodies(
    impl_blocks: list[ImplBlock], file_rel: str,
) -> list[dict[str, Any]]:
    """Return [{fn_name, container, body}] for every FILLED fn in `file_rel`,
    pulled from the impl block records. Free fns are dropped (they have no
    container). Used by `extract_crate_idioms` to scan for patterns."""
    out: list[dict[str, Any]] = []
    for b in impl_blocks:
        if b.file != file_rel:
            continue
        for m in b.members:
            if m.has_unimplemented_body:
                continue
            body = m.body_text.strip()
            if not body:
                continue
            out.append({
                "fn_name": m.fn_name,
                "container": b.container,
                "body": body,
            })
    return out


def extract_crate_idioms(
    impl_blocks: list[ImplBlock],
    file_rel: str,
    target_sig_text: str = "",
    max_idioms: int = 5,
) -> str:
    """Walk filled fn bodies in `file_rel` and surface up to `max_idioms`
    distinctive patterns the model would benefit from seeing while filling
    a stub in the same file.

    Returned string is the body of a `<CRATE_IDIOMS>` section — empty
    when no patterns clear the relevance bar. Each idiom is one line:

        # Pattern label — used by `fn caller_name` in this file
        <one-line code snippet>

    Detection axes (each rendered at most twice):
      1. Self::method(arg, None) family — recurrent constructor pattern.
      2. (field.subfield)(args) — fn-pointer-via-field dispatch (vtable).
      3. unsafe { … } single-line chains — pointer / NonNull manipulation.
      4. StructName { field: …, … } — wrapper-type construction.

    Relevance: prefer idioms whose symbols (Self::method name, struct
    name, field name) appear in `target_sig_text` so the model sees
    crate-specific patterns it's about to need.
    """
    fns = extract_filled_fn_bodies(impl_blocks, file_rel)
    if not fns:
        return ""

    target_tokens = set(identifier_tokens(target_sig_text or ""))

    # Per-axis collectors with rank score.
    candidates: list[tuple[float, str]] = []

    # Axis 1 — Self:: family. Count occurrences per method name.
    self_method_counts: dict[str, int] = {}
    self_method_examples: dict[str, tuple[str, str]] = {}  # name → (caller, snippet)
    for fn in fns:
        for full, name in self_constructor_calls(fn["body"]):
            self_method_counts[name] = self_method_counts.get(name, 0) + 1
            self_method_examples.setdefault(name, (fn["fn_name"], full))
    family_names = [n for n, c in self_method_counts.items() if c >= 2]
    if family_names:
        # One synthesised entry: "this file uses Self::X / Self::Y / Self::Z as constructors"
        family_list = ", ".join(f"Self::{n}" for n in sorted(family_names))
        # Show ONE concrete call from the highest-count family member.
        top = max(family_names, key=lambda n: self_method_counts[n])
        caller, snippet = self_method_examples[top]
        snippet = " ".join(snippet.split())  # collapse whitespace
        if len(snippet) > 140:
            snippet = snippet[:140] + " …"
        rel_score = 2.0 if any(n in target_tokens for n in family_names) else 1.0
        candidates.append((
            rel_score + 0.1 * sum(self_method_counts[n] for n in family_names),
            f"# Self:: family — this file's constructors: {family_list}.\n"
            f"# Example, from `fn {caller}`:\n"
            f"#   {snippet}"
        ))

    # Axis 2 — fn-pointer field dispatch `(x.y)(args)`. Group by the path.
    fnptr_counts: dict[str, int] = {}
    fnptr_examples: dict[str, tuple[str, str]] = {}
    for fn in fns:
        for raw_path in fn_pointer_field_calls(fn["body"]):
            path = raw_path.strip()
            if "." not in path:
                continue  # likely an arithmetic expression, not a vtable access
            base = path.rsplit(".", 1)[1]
            fnptr_counts[base] = fnptr_counts.get(base, 0) + 1
            fnptr_examples.setdefault(base, (fn["fn_name"], path))
    # Pick the 2 most frequent fn-pointer fields.
    for name in sorted(fnptr_counts, key=lambda n: -fnptr_counts[n])[:2]:
        if fnptr_counts[name] < 1:
            continue
        caller, path = fnptr_examples[name]
        rel_score = 2.0 if name in target_tokens else 1.0
        candidates.append((
            rel_score + 0.05 * fnptr_counts[name],
            f"# Fn-pointer field dispatch — call via the field, NOT via\n"
            f"# method syntax. Used in `fn {caller}`:\n"
            f"#   ({path})(args…)   // NOT  {path.split('.')[-1]}(args…)"
        ))

    # Axis 3 — unsafe { … } single-line chains. Surface the most common
    # SHAPE of such a chain (collapsed by token-pattern, not exact text).
    unsafe_examples: dict[str, tuple[str, str]] = {}
    unsafe_counts: dict[str, int] = {}
    for fn in fns:
        for inner in unsafe_block_chains(fn["body"]):
            chain = " ".join(inner.split())
            if not chain:
                continue
            # Shape = identifier-only with `.`s preserved.
            shape = mask_identifiers(chain)
            shape = " ".join(shape.split())
            unsafe_counts[shape] = unsafe_counts.get(shape, 0) + 1
            unsafe_examples.setdefault(shape, (fn["fn_name"], chain))
    for shape in sorted(unsafe_counts, key=lambda s: -unsafe_counts[s])[:2]:
        if unsafe_counts[shape] < 1:
            continue
        caller, chain = unsafe_examples[shape]
        if len(chain) > 140:
            chain = chain[:140] + " …"
        rel_score = 1.0 + 0.05 * unsafe_counts[shape]
        # Bias if the chain mentions a target token (e.g. inner, vtable, ptr).
        for t in identifier_tokens(chain):
            if t in target_tokens:
                rel_score += 0.5
                break
        candidates.append((
            rel_score,
            f"# Unsafe chain pattern — used in `fn {caller}`:\n"
            f"#   unsafe {{ {chain} }}"
        ))

    # Axis 4 — Struct literal construction. Show wrapper types the file
    # constructs that aren't trivial single-field types.
    struct_examples: dict[str, tuple[str, str]] = {}
    struct_counts: dict[str, int] = {}
    for fn in fns:
        for name, raw_inner in struct_literals(fn["body"]):
            # Skip stdlib-y names that are noise.
            if name in {"Self", "Some", "None", "Ok", "Err", "Box", "Vec",
                        "String", "Result", "Option"}:
                continue
            inner = " ".join(raw_inner.split())
            if not inner or "," not in inner and ":" not in inner:
                continue
            struct_counts[name] = struct_counts.get(name, 0) + 1
            struct_examples.setdefault(name, (fn["fn_name"], inner))
    for name in sorted(struct_counts, key=lambda n: -struct_counts[n])[:2]:
        if struct_counts[name] < 1:
            continue
        caller, inner = struct_examples[name]
        if len(inner) > 140:
            inner = inner[:140] + " …"
        rel_score = 2.0 if name in target_tokens else 1.0
        candidates.append((
            rel_score + 0.05 * struct_counts[name],
            f"# Wrapper construction — used in `fn {caller}`:\n"
            f"#   {name} {{ {inner} }}"
        ))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: -x[0])
    chosen = [snippet for _score, snippet in candidates[:max_idioms]]
    return "\n\n".join(chosen)
