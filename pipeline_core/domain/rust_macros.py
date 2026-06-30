"""Rust ``macro_rules!`` definition scanning — pure domain (P7 source-regex→structured).

Replaces the four ``re.finditer(r"macro_rules!\\s+(\\w+)\\s*\\{")`` source scans in
pipeline.py with one structured scanner returning each definition's name, export
flag, and byte ranges. Deliberately NAIVE (not comment / string aware) so it is
BYTE-EQUIVALENT to the regex + hand-rolled brace counter it replaces: the
originals matched ``macro_rules!`` anywhere (including inside comments/strings)
and counted braces without skipping string/char literals, so this scanner does
exactly the same — no smarter, no different.

Pure: stdlib only, no I/O, no regex.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.rust_idents import attr_call_args

_MACRO_KW = "macro_rules!"
_EXPORT_ATTR = "#[macro_export]"


def proc_macro_fallback_requirements(
    relevant: list[tuple[str, str]], crate_name: str = "",
) -> str:
    """Static fallback: scan test sources for derive/attribute usage and
    synthesise minimal proc-macro requirements (markdown) via the structured
    `attr_call_args` scanner. Pure text→text."""
    derives: set[str] = set()
    attrs: set[str] = set()
    attr_name = (crate_name or "").replace("-", "_") or "derive"
    for _, content in relevant:
        for inner in attr_call_args(content, "derive"):
            for d in inner.split(","):
                derives.add(d.strip())
        for inner in attr_call_args(content, attr_name):
            attrs.add(inner.strip())
    req = "# Proc-macro interface\n\n## Derive macros\n"
    for d in sorted(derives):
        req += f"- `#[derive({d})]` — implement the `{d}` trait on annotated types\n"
    if attrs:
        req += "\n## Helper attributes\n"
        for a in sorted(attrs):
            req += f"- `#[{attr_name}({a})]`\n"
    req += (
        "\n## Entry points\n"
        "Each derive macro must be registered with `#[proc_macro_derive(Name)]`.\n"
        "Input: `proc_macro::TokenStream`, Output: `proc_macro::TokenStream`.\n"
        "Parse input with `syn::parse_macro_input!(input as syn::DeriveInput)`.\n"
        "Generate output with `quote::quote! { impl Name for #ident { ... } }`.\n"
    )
    return req


@dataclass(frozen=True, slots=True)
class MacroDef:
    """One ``macro_rules! NAME {`` header match and its byte offsets.

    Emitted for every header the regex ``macro_rules!\\s+(\\w+)\\s*\\{`` would match,
    even when the body's braces never balance (`balanced is False`); the name-only
    consumer counts those too, while the body-extracting consumers skip them
    (`if not md.balanced: continue`) — matching the originals' ``if depth != 0:``.
    """

    name: str
    exported: bool      # `#[macro_export]` on the line immediately preceding the decl
    export_start: int   # offset of that `#[macro_export]` line if exported, else == decl_start
    decl_start: int     # offset of the `macro_rules!` keyword
    body_open: int      # offset of the `{` opening the body
    body_close: int     # offset of the matching `}` (or n-1 if `balanced is False`)
    balanced: bool      # whether the naive brace walk reached depth 0


def _is_word_char(c: str) -> bool:
    """``\\w`` semantics (unicode word char): ASCII/unicode alphanumeric or ``_``."""
    return c.isalnum() or c == "_"


def macro_definitions(text: str) -> list[MacroDef]:
    """Every ``macro_rules! NAME { ... }`` in `text`, in source order.

    Byte-equivalent to ``re.finditer(r"macro_rules!\\s+(\\w+)\\s*\\{")`` plus the
    naive depth-counting brace walk the four callers used: a ``macro_rules!``
    literal, ``\\s+``, a ``\\w+`` name, ``\\s*``, then ``{``; the body runs to the
    brace-balanced ``}`` (braces inside strings/comments are NOT skipped — same as
    the originals' hand counter). Every header match is emitted; an unbalanced body
    gets ``balanced=False`` (the body-extracting callers skip those, the name-only
    caller keeps them — exactly as the originals did).
    """
    n = len(text)
    out: list[MacroDef] = []
    scan = 0
    while True:
        decl = text.find(_MACRO_KW, scan)
        if decl == -1:
            break
        # Advance past this literal regardless of outcome. The skipped span
        # `macro_rules!`…`{` cannot contain another `macro_rules!` literal, so
        # this is equivalent to finditer's non-overlapping advance.
        scan = decl + len(_MACRO_KW)
        j = scan
        while j < n and text[j].isspace():  # `\s+`
            j += 1
        if j == scan:
            continue
        name_start = j
        while j < n and _is_word_char(text[j]):  # `(\w+)`
            j += 1
        if j == name_start:
            continue
        name = text[name_start:j]
        while j < n and text[j].isspace():  # `\s*`
            j += 1
        if j >= n or text[j] != "{":  # `\{`
            continue
        body_open = j
        depth = 1
        k = body_open + 1
        while k < n and depth > 0:  # naive balanced-brace walk (not string/comment aware)
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
            k += 1
        balanced = depth == 0
        body_close = k - 1
        exported = False
        export_start = decl
        # `(?:#\[macro_export\][^\n]*\n)?` immediately preceding the decl.
        if decl > 0 and text[decl - 1] == "\n":
            line_start = text.rfind("\n", 0, decl - 1) + 1
            if text.startswith(_EXPORT_ATTR, line_start):
                exported = True
                export_start = line_start
        out.append(MacroDef(
            name=name, exported=exported, export_start=export_start,
            decl_start=decl, body_open=body_open, body_close=body_close,
            balanced=balanced,
        ))
    return out
