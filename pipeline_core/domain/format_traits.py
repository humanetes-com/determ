"""Which std formatting traits a piece of Rust test text invokes.

Its own module so both `domain.attribution` (the observable reading) and
`domain.refill_stamp` (the homonym stamp, which imports `domain.attribution`)
can read the one rule without an import cycle.

WHY IT LEXES. The rule used to be substring tests over the whole text
(`"{}" in text`). An escaped brace is not a placeholder: `"value: {{}}"`
contains `{}` and formats no argument at all. Run `1430425-193a8f28`
(2026-10-14, cluster `stable`): two generated tests failed on
`assert!(err.is::<&str>())` after `let err = anyhow!("value: {{}}")`; the
observable slice (`failing_observable`) read `{Display}`, so both tests
were attributed to the Display formatter, rode the Display pair for 25
minutes, and their producer escalation was withheld. The same substring
test also matched `{}` in code (an empty block) and `{:?}` in a comment.
Placeholders are now read only inside string literals, by the
`std::fmt` grammar: `{{` and `}}` are literal braces, a placeholder is
`{` [argument] [`:` spec] `}`, and the spec's type decides the trait.
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_lex import code_mask

__all__ = ["format_traits_used", "placeholder_traits"]

#: The argument of a placeholder: a position, a name, or nothing.
_ARG = r"(?:[0-9]+|[A-Za-z_][A-Za-z0-9_]*)?"
#: A width or precision count: a number, or an argument followed by `$`.
_COUNT = r"(?:[0-9]+\$?|[A-Za-z_][A-Za-z0-9_]*\$)"
_SPEC = re.compile(
    r"(?:.?[<^>])?[+-]?#?0?" + _COUNT + r"?"
    r"(?:\.(?:" + _COUNT + r"|\*))?"
    r"(?P<ty>x\?|X\?|\?|[xXobeEp])?")
_PLACEHOLDER = re.compile(r"\s*" + _ARG + r"\s*(?::(?P<spec>.*))?", re.S)
#: Format-spec types that name `Debug`; no type names `Display`. The other
#: types (`x`, `o`, `b`, `e`, `p`, …) are other std traits, neither of the two.
_DEBUG_TYPES = frozenset({"?", "x?", "X?"})


def _string_literals(text: str) -> list[str]:
    """The contents of every string literal in `text` (plain, byte and raw),
    found through `rust_lex.code_mask`; comments and char literals are not
    string literals."""
    mask = code_mask(text)
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        run = text[i:j]
        body = run.lstrip("b")
        if body.startswith("r") and body[1:2] in ('"', "#"):
            hashes = len(body) - len(body[1:].lstrip("#")) - 1
            inner = body[1 + hashes:]
            out.append(inner[1:len(inner) - 1 - hashes] if inner.startswith('"') else "")
        elif body.startswith('"'):
            out.append(body[1:-1] if len(body) > 1 and body.endswith('"') else body[1:])
        i = j
    return out


def placeholder_traits(literal: str) -> frozenset[str]:
    """The traits the placeholders of one format-string literal name.
    `{{` / `}}` are literal braces; text between braces that is not a valid
    placeholder (a JSON-ish `{ a: 1 }`, a nested brace) names nothing."""
    traits: set[str] = set()
    i, n = 0, len(literal)
    while i < n:
        c = literal[i]
        if c in "{}" and literal[i + 1:i + 2] == c:
            i += 2
            continue
        if c != "{":
            i += 1
            continue
        close = literal.find("}", i + 1)
        if close == -1:
            break
        inner = literal[i + 1:close]
        m = _PLACEHOLDER.fullmatch(inner) if "{" not in inner else None
        spec = (m.group("spec") or "") if m else None
        sm = _SPEC.fullmatch(spec) if spec is not None else None
        if sm is not None:
            ty = sm.group("ty") or ""
            if ty in _DEBUG_TYPES:
                traits.add("Debug")
            elif not ty:
                traits.add("Display")
        i = close + 1
    return frozenset(traits)


def format_traits_used(test_src: str) -> frozenset[str]:
    """Which std formatting traits a test's OWN source invokes: the
    placeholders of its string literals (`{:?}`/`{:#?}`/`{x:?}` need
    `Debug`, `{}`/`{:#}`/`{0}` need `Display`) and `.to_string()` in code
    (`Display`).

    The single home of the rule. Which TEXT it reads is the caller's
    question: `AttributionResult.observable_traits_for` reads the failing
    observable (`domain.failing_observable`) for the hop, the escalation
    withhold and the sustain; the homonym stamp reads the whole source.
    """
    traits: set[str] = set()
    for lit in _string_literals(test_src):
        traits |= placeholder_traits(lit)
    mask = code_mask(test_src)
    for m in re.finditer(r"\.to_string\(\)", test_src):
        if not mask[m.start()]:
            traits.add("Display")
    return frozenset(traits)
