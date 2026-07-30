"""A fn's addressable SLOT inside a Rust file — pure domain.

A signature line does not identify a fn. `fmt` appears once per `Debug`/`Display`
impl, `source` once per `StdError` impl, `clone` once per pointer type, and
`ext_context` once per extension impl. Anything that moves BODIES between two
copies of the same file — the cross-cluster seed and the model-output merge both
do — must therefore address a fn by its enclosing item chain as well, or the last
body carrying a given signature silently wins for every slot that shares it.

Observed live (`anyhow`, cluster `nightly`): a seed keyed on the signature alone
put `impl StdError for Error`'s `self.context(context)` into
`impl<E> StdError for E`, so the cluster opened on
`error[E0599]: no method named 'context' found for type parameter 'E'`, 39 rustc
errors and an oracle of 0/124 — before a single model call, and unfixable by any
per-fn fill because every error was downstream of the target. Eleven signatures
across six files collided in that one seed. Five of them produce bodies that
COMPILE and are merely wrong (`fmt` swapping `Debug` for `Display`, `source`
returning `None`, `Own`/`Ref` swapping `clone`), which is the worse half: a red
tree announces itself, a silently mis-assigned body does not.

`owner_path` is the chain of enclosing brace-blocks, outermost first, each
normalised to single-spaced text. Both sides of a merge derive from the same
baked skeleton, so the chain is byte-identical where it must match; where it is
not (a model emitting a thin file with its own formatting) the merge falls back
to the signature and refuses only the AMBIGUOUS ones — see `fill_merge`.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from .rust_fn_headers import fn_headers
from .rust_lex import code_mask

__all__ = ["FnSlot", "FnBody", "fn_bodies"]

# Any of these at brace depth 0 ends the preceding item, so the text between it
# and a `{` is that block's own header.
_ITEM_DELIMITERS = frozenset(";{}")


@dataclass(frozen=True, slots=True)
class FnSlot:
    """Where a fn body lives, precisely enough to move a body between two copies
    of the same file without landing it in the wrong impl.

    Hashable and comparable by value, so it is usable directly as a merge key."""

    owner_path: tuple[str, ...]
    signature: str


@dataclass(frozen=True, slots=True)
class FnBody:
    """One fn's slot together with the byte span of its body.

    `body_start` is just past the opening `{`; `body_end` is the index of the
    matching `}` (exclusive end of the body), matching `locate_all_fns`."""

    slot: FnSlot
    header_start: int
    body_start: int
    body_end: int

    @property
    def signature(self) -> str:
        return self.slot.signature


def _normalise(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip — so a header that
    wraps its where-clause across lines compares equal to one that does not."""
    return " ".join(text.split())


def fn_bodies(text: str) -> list[FnBody]:
    """Every fn in `text` with a balanced body, in source order, each addressed
    by its enclosing item chain as well as its signature.

    Brace depth is counted over CODE only (`code_mask`), so a `}` inside a string
    literal or a doc-comment can neither close a body early nor desync the owner
    stack. Malformed (unbalanced) fns are skipped, exactly as the span scanner
    this replaces did."""
    mask = code_mask(text)
    n = len(text)

    # Sorted positions of the unmasked delimiters that can end an item, so
    # resolving a block's header is a bisect rather than a backward scan (which
    # would be quadratic on the 60 KB files this runs against).
    delimiters = [i for i in range(n) if not mask[i] and text[i] in _ITEM_DELIMITERS]

    def header_before(brace: int) -> str:
        """The declaration text introducing the block that opens at `brace`.

        Leading comments and `#[…]` attributes are skipped so the header starts
        at the item keyword. Both must be dropped for the chain to be a stable
        key across two bakes of the same file: a comment sits outside the
        previous item's `;`/`}` and would otherwise be glued onto this header,
        and a `#[cfg(…)]` is exactly the thing two clusters differ by."""
        k = bisect_left(delimiters, brace)
        i = delimiters[k - 1] + 1 if k > 0 else 0
        while i < brace:
            if mask[i] or text[i].isspace():
                i += 1
                continue
            if text[i] != "#":
                break
            open_bracket = text.find("[", i)
            if open_bracket == -1 or open_bracket >= brace:
                break
            depth = 0
            j = open_bracket
            while j < brace:
                if not mask[j]:
                    if text[j] == "[":
                        depth += 1
                    elif text[j] == "]":
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            i = j + 1
        return _normalise(text[i:brace])

    fn_brace_open = {h.brace_open: h for h in fn_headers(text, line_anchored=False)}

    out: list[FnBody] = []
    stack: list[str] = []
    # Owner chain captured at each open brace, so a fn's chain is restored
    # correctly however deeply its body nests.
    i = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch == "{":
            header = fn_brace_open.get(i)
            if header is not None:
                body_start = i + 1
                depth = 1
                j = body_start
                while j < n and depth > 0:
                    if not mask[j]:
                        if text[j] == "{":
                            depth += 1
                        elif text[j] == "}":
                            depth -= 1
                    j += 1
                if depth == 0:
                    out.append(FnBody(
                        slot=FnSlot(
                            owner_path=tuple(stack),
                            signature=_normalise(
                                text[header.start:body_start].rstrip("{ \t\n")),
                        ),
                        header_start=header.start,
                        body_start=body_start,
                        body_end=j - 1,
                    ))
            stack.append(header_before(i) if header is None
                         else _normalise(text[header.start:i]))
        elif ch == "}":
            if stack:
                stack.pop()
        i += 1
    return out
