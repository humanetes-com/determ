"""Inline unit-test modules — `#[cfg(test)] mod NAME { … }` written in the same
file as the code under test.

Why they are a hazard for reconstruction: the fill routes rewrite whole source
files, so an inline test body sits inside the text the model is SHOWN and inside
the text the splice WRITES BACK. A reply that restates a test lands verbatim,
yielding a test that compiles, runs, asserts nothing, and still counts toward the
oracle — and no fill mechanism can repair it, because a test fn is an immutable
oracle target by design. Relocating the block to a `gen_tests_N` sibling before
the skeleton is baked removes the hazard structurally: no test is left in the
file for any route to touch, while the tests themselves are preserved (the
sibling compiles into the same `--lib` binary from the same module position).

Pure: stdlib only, no I/O, no regex. Comment/string aware via `code_mask` — a
`{` in a doc example must not shift the brace depth, and a `#[cfg(test)]` quoted
inside a string is not an attribute.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pipeline_core.domain.cfg import (
    All,
    CfgAtom,
    CfgPred,
    CfgSet,
    Leaf,
    match_cfg_attr_line,
    normalize_pred,
    parse_cfg_pred,
)
from pipeline_core.domain.rust_lex import code_mask

_IDENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_IDENT_START = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")


@dataclass(frozen=True)
class InlineTestMod:
    """One `#[cfg(test)] mod NAME { … }` block declared at brace depth 0.

    `start`/`end` span the WHOLE item including its attributes, so replacing
    that slice with a `mod gen_tests_N;` path-decl is an exact swap. `body` is
    the dedented inner text, ready to become a sibling file. `extra_cfg` carries
    any cfg predicate beyond `test` the block was gated by, so the relocated
    sibling keeps the same cluster gate. When `refusal` is non-empty the block
    was located but must NOT be relocated — the caller leaves it in place and
    says why, rather than silently mangling a shape this module cannot model.
    """
    name: str
    start: int
    end: int
    body: str
    extra_cfg: str = ""
    refusal: str = ""

    @property
    def relocatable(self) -> bool:
        return not self.refusal


def find_inline_test_mods(text: str) -> tuple[InlineTestMod, ...]:
    """Every depth-0 `#[cfg(test)] mod NAME { … }` block, in source order.

    Path-declarations (`mod gen_tests_1;`) are NOT inline modules — they have no
    body to relocate and are already the target shape. Nested test mods (depth
    ≥ 1) are reported with a refusal instead of being moved: a `#[path]` decl
    resolves relative to the parent file's directory, so relocating one out of
    an inline `mod` wrapper would change which file the compiler looks for.
    """
    mask = code_mask(text)
    n = len(text)
    out: list[InlineTestMod] = []
    depth = 0
    i = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch not in _IDENT_START or (i > 0 and text[i - 1] in _IDENT_CHARS):
            i += 1
            continue
        j = i
        while j < n and not mask[j] and text[j] in _IDENT_CHARS:
            j += 1
        if text[i:j] != "mod":
            i = j
            continue
        found = _read_mod_item(text, mask, i, depth)
        if found is None:
            i = j
            continue
        mod, after = found
        out.append(mod)
        # The block's own braces were consumed by _read_mod_item; resuming past
        # its closing `}` keeps `depth` correct for the items that follow.
        i = after
    return tuple(out)


def splice_out_inline_test_mod(
    text: str, mod: InlineTestMod, replacement: str,
) -> str:
    """Replace one located block's exact span with `replacement`.

    Apply to several blocks in DESCENDING `start` order — an earlier splice
    would otherwise shift every later block's offsets.
    """
    return text[:mod.start] + replacement + text[mod.end:]


def _read_mod_item(
    text: str, mask: Sequence[bool], mod_idx: int, depth: int,
) -> tuple[InlineTestMod, int] | None:
    """Parse the `mod` item whose keyword starts at `mod_idx`.

    Returns (block, index just past its closing `}`) when the item is an inline
    module carrying `#[cfg(test)]`, else None (a path-decl, a non-test module,
    or an unterminated block — all of which the caller simply walks past).
    """
    n = len(text)
    k = mod_idx + 3
    while k < n and (mask[k] or text[k].isspace()):
        k += 1
    name_start = k
    while k < n and not mask[k] and text[k] in _IDENT_CHARS:
        k += 1
    name = text[name_start:k]
    if not name or text[name_start] not in _IDENT_START:
        return None
    while k < n and (mask[k] or text[k].isspace()):
        k += 1
    if k >= n or text[k] != "{":
        return None                      # `mod foo;` — a path-decl, not inline
    close = _matching_brace(text, mask, k)
    if close is None:
        return None
    head, attrs = _attrs_before(text, mask, _item_head_start(text, mask, mod_idx))
    cfgs = [p for p in (_cfg_predicate(a) for a in attrs) if p is not None]
    gate = _test_gate(cfgs)
    if gate is None:
        return None
    extra_cfg, refusal = gate
    if not refusal and depth != 0:
        refusal = "nested inside another module"
    return InlineTestMod(
        name=name,
        start=head,
        end=close + 1,
        body="" if refusal else _dedent(text[k + 1:close]),
        extra_cfg=extra_cfg,
        refusal=refusal,
    ), close + 1


_TEST_ATOM = CfgAtom(key="test")
_TEST_ON = CfgSet.of(cfg_true=["test"])
_TEST_OFF = CfgSet.of(cfg_false=["test"])


def _test_gate(preds: Sequence[str]) -> tuple[str, str] | None:
    """(cfg the relocated sibling must keep, refusal) for an item's cfg attributes.

    None when the attributes do not make this a TEST-ONLY module — the caller
    then treats the item as ordinary code. Two shapes land here that a substring
    look for "test" would misread: `#[cfg(not(test))]` excludes the item exactly
    when tests build, and `#[cfg(feature = "testing")]` never mentions the `test`
    atom at all. Both are production modules.

    The relocated decl reads `#[cfg(EXTRA)] #[cfg(test)] #[path…] mod gen_tests_N;`
    — a CONJUNCTION with `test`. So a gate is reproducible only if it likewise
    requires `test`; `any(test, feature = "x")` compiles outside test builds too
    and is refused rather than silently narrowed.
    """
    trees = [parse_cfg_pred(p) for p in preds]
    if not any(_TEST_ATOM in t.atoms() for t in trees):
        return None
    gate = trees[0] if len(trees) == 1 else All(tuple(trees))
    if gate.evaluate(_TEST_OFF) is not False:
        if gate.evaluate(_TEST_ON) is False:
            return None                      # gated OFF under test — not tests
        return "", f"cfg gate does not require test: {gate.normalized}"
    rest = [c for c in _conjuncts(gate)
            if not (isinstance(c, Leaf) and c.atom == _TEST_ATOM)]
    if any(_TEST_ATOM in c.atoms() for c in rest):
        # `test` is required, but not as a plain conjunct (`all(any(test), …)`)
        # — the residue cannot be restated without carrying `test` twice.
        return "", f"cfg gate ties test to a nested condition: {gate.normalized}"
    if not rest:
        return "", ""
    if len(rest) == 1:
        return rest[0].normalized, ""
    return All(tuple(rest)).normalized, ""


def _conjuncts(pred: CfgPred) -> tuple[CfgPred, ...]:
    """Flatten nested `all(...)` into the conditions it requires, all of them."""
    if isinstance(pred, All):
        return tuple(c for p in pred.parts for c in _conjuncts(p))
    return (pred,)


def _matching_brace(text: str, mask: Sequence[bool], open_idx: int) -> int | None:
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        if not mask[i]:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _item_head_start(text: str, mask: Sequence[bool], mod_idx: int) -> int:
    """Index of the item's first token — steps back over `pub` / `pub(crate)`."""
    j = _prev_code(text, mask, mod_idx)
    if j is None:
        return mod_idx
    if text[j] == ")":
        opener = _matching_paren_back(text, mask, j)
        if opener is None:
            return mod_idx
        j = _prev_code(text, mask, opener)
        if j is None:
            return mod_idx
    end = j + 1
    start = end
    while start > 0 and not mask[start - 1] and text[start - 1] in _IDENT_CHARS:
        start -= 1
    return start if text[start:end] == "pub" else mod_idx


def _attrs_before(
    text: str, mask: Sequence[bool], head: int,
) -> tuple[int, list[str]]:
    """(span start, text of each outer attribute) for the item starting at `head`.

    The span start is where the item's FIRST attribute begins, so the caller can
    swap the whole `#[…] #[…] mod X { … }` run in one slice — replacing only the
    `mod` would strand its attributes on whatever item follows. Walks backwards
    span by span so a multi-line attribute is ONE span, not one per line. Stops
    at an inner attribute (`#![…]`): that belongs to the enclosing module.
    """
    attrs: list[str] = []
    at = head
    while True:
        span = _attr_span_before(text, mask, at)
        if span is None:
            return at, list(reversed(attrs))
        start, end = span
        body = text[start:end]
        if body.lstrip("#").lstrip().startswith("!"):
            return at, list(reversed(attrs))
        attrs.append(body)
        at = start


def _attr_span_before(
    text: str, mask: Sequence[bool], at: int,
) -> tuple[int, int] | None:
    """(start, end) of the attribute whose `]` is the last code char before `at`."""
    j = _prev_code(text, mask, at)
    if j is None or text[j] != "]":
        return None
    close = j
    depth = 0
    while j >= 0:
        if not mask[j]:
            if text[j] == "]":
                depth += 1
            elif text[j] == "[":
                depth -= 1
                if depth == 0:
                    break
        j -= 1
    if j < 0:
        return None
    end = _prev_code(text, mask, j)
    if end is None:
        return None
    if text[end] == "!":                        # `#![…]` — inner attribute
        end = _prev_code(text, mask, end)
        if end is None:
            return None
    if text[end] != "#":
        return None
    return end, close + 1


def _prev_code(text: str, mask: Sequence[bool], at: int) -> int | None:
    j = at - 1
    while j >= 0 and (mask[j] or text[j].isspace()):
        j -= 1
    return j if j >= 0 else None


def _matching_paren_back(
    text: str, mask: Sequence[bool], close_idx: int,
) -> int | None:
    depth = 0
    j = close_idx
    while j >= 0:
        if not mask[j]:
            if text[j] == ")":
                depth += 1
            elif text[j] == "(":
                depth -= 1
                if depth == 0:
                    return j
        j -= 1
    return None


def _cfg_predicate(attr_text: str) -> str | None:
    """Predicate `P` of a `#[cfg(P)]` attribute, else None.

    The span may be multi-line (`#[cfg(\n  test\n)]`); flattening to one line
    first lets the single-line matcher stay the one parser for this shape.
    """
    flat = " ".join(attr_text.split())
    pred = match_cfg_attr_line(flat)
    return None if pred is None else normalize_pred(pred)


def _dedent(body: str) -> str:
    lines = body.splitlines()
    filled = [ln for ln in lines if ln.strip()]
    if not filled:
        return ""
    indent = min(len(ln) - len(ln.lstrip()) for ln in filled)
    if indent:
        lines = [ln[indent:] if ln[:indent].isspace() else ln for ln in lines]
    return "\n".join(lines).strip() + "\n"
