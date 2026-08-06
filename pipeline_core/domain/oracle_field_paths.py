"""Member paths the crate's OWN frozen tests read on a model-authored type —
pure domain.

t1.5 elides the private fields of a public struct and asks the model to design
them (`bake-skeleton`: "public struct(s) whose private fields are model-authored:
Chain, Error"). The in-crate augmented unit tests are generated BEFORE that, from
the original source, where those fields are visible — and they are write-protected
oracle content afterwards. So an authored layout that renames a field the oracle
reads is not "a different valid design": it makes a test file no stage may edit
uncompilable, and the whole cluster with it.

Measured on `anyhow`, run `1171697-3838c847` (2026-08-06 17:27 UTC): the layout
authored for `Error` was `#[repr(transparent)] pub struct Error { ptr:
NonNull<ErrorImpl> }` — it satisfied all 6 harvested pins (two `size_of`, four
autotrait) and compiled. `src/error.gen_tests_3.rs:29` reads
`err.inner.ptr.as_ptr()`. `cargo check --lib` never builds `#[cfg(test)]` code,
so the gate saw nothing; at the first `cargo test` the lib test target failed with
`error[E0609]: no field 'inner' on type 'Error'`, taking all 56 unit tests with
it (0/56 in both clusters, 44 % of the 126-test oracle). `stable` then STOPPED —
"the build is red and no rustc diagnostic resolves to a fillable fn" — because
the only diagnostic pointed into oracle content the fill loop may not rewrite,
which is the correct refusal and a dead cluster either way.

What this module extracts is therefore a CONSTRAINT, not a preference: the
`(root type, field chain)` pairs the frozen oracle reads. `layout_probe` renders
them as lib-target items so the authoring gate decides them, and the authoring
prompt carries them so the first attempt already knows the names.

Everything here is a token scan — no regex (the layout modules' standing rule).
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

# `x.await` is not a field read, and neither is a method call — both are excluded
# by the walk below. Numeric segments ARE fields: `DisplayError<M>(pub M)` is a
# tuple struct and `.0` is how the crate reads it.
_KEYWORD_SEGMENTS = frozenset({"await"})


@dataclass(frozen=True, slots=True)
class FieldPath:
    """One member path an oracle test reads: `err.inner.ptr` on an `Error`
    becomes root_type="Error", fields=("inner", "ptr")."""

    root_type: str
    fields: tuple[str, ...]
    origin: str = ""            # "src/error.gen_tests_3.rs:29", for messages

    @property
    def expr(self) -> str:
        return ".".join((self.root_type, *self.fields))


def _strip_line_comment(line: str) -> str:
    """`line` up to its first `//` that is not inside a string or char literal.

    A doc comment mentioning a field name must not become a layout constraint:
    the failing anyhow test opens with `// SAFETY: Error's inner pointer points
    to a valid heap allocation`, which is prose about the field, not a read of
    it."""
    in_str = False
    quote = ""
    i = 0
    while i < len(line):
        c = line[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                in_str = False
        elif c in "\"'":
            in_str = True
            quote = c
        elif c == "/" and line.startswith("//", i):
            return line[:i]
        i += 1
    return line


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _read_ident(text: str, at: int) -> str:
    """The identifier (or digit run) starting at `at`, else ""."""
    j = at
    while j < len(text) and _is_ident_char(text[j]):
        j += 1
    return text[at:j]


def _word_at(text: str, at: int, word: str) -> bool:
    """Is `word` at `at` a whole word (no identifier char either side)?"""
    if not text.startswith(word, at):
        return False
    before = text[at - 1] if at else ""
    after = text[at + len(word)] if at + len(word) < len(text) else ""
    return not (_is_ident_char(before) or _is_ident_char(after))


def _type_from_annotation(text: str) -> str:
    """The type name in a `let x: T =` annotation, or "" when T is anything the
    probe cannot borrow a field through.

    Accepted: a plain path, optionally behind `&`/`&mut` — the two forms a
    `let _: &ErrorImpl = …` probe can reproduce as `fn p(v: &ErrorImpl)`.
    Rejected on purpose: generics (`Own<ErrorImpl>` — the probe would have to
    invent the parameters), raw pointers (`*const ErrorImpl` — reading a field
    needs an unsafe deref, so the path is not a statement about the field's
    NAME), tuples, slices, `dyn`, `impl`."""
    t = text.strip()
    while True:
        if t.startswith("&"):
            t = t[1:].lstrip()
            continue
        if t.startswith("mut ") or t.startswith("ref "):
            t = t[4:].lstrip()
            continue
        break
    if not t or not (t[0].isalpha() or t[0] == "_"):
        return ""
    j = 0
    while j < len(t) and (_is_ident_char(t[j]) or t.startswith("::", j)):
        j += 2 if t.startswith("::", j) else 1
    if t[j:].strip():          # anything trailing: `<…>`, `[…]`, `(…)`, `dyn`
        return ""
    last = t[:j].split("::")[-1]
    return last if last[:1].isupper() else ""


def _type_from_initializer(text: str) -> str:
    """The type name a `let x = …` initializer names outright: `Error::msg(..)`
    or `Error { .. }`. Anything else (a call through a variable, a method chain,
    a macro) leaves the binding unresolved rather than guessed."""
    t = text.strip()
    if t.startswith("unsafe"):
        t = t[len("unsafe"):].lstrip().lstrip("{").lstrip()
    if not t or not t[0].isupper():
        return ""
    head = _read_ident(t, 0)
    if not head:
        return ""
    rest = t[len(head):].lstrip()
    if rest.startswith("::") or rest.startswith("{"):
        return head
    return ""


def _local_types(src: str) -> dict[str, str]:
    """ident → type name, for the `let` bindings in `src` whose type is stated
    outright. A name bound twice to DIFFERENT types is dropped: with no scope
    tracking, the second binding's fields would be attributed to the first
    one's type, and a wrong root type is exactly the mistake that would freeze
    a component that was fine."""
    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for raw in src.splitlines():
        line = _strip_line_comment(raw)
        at = 0
        while (at := line.find("let ", at)) >= 0:
            if not _word_at(line, at, "let"):
                at += 4
                continue
            rest = line[at + 4:].lstrip()
            if rest.startswith("mut "):
                rest = rest[4:].lstrip()
            name = _read_ident(rest, 0)
            at += 4
            if not name or name[0].isupper():
                continue
            tail = rest[len(name):].lstrip()
            if tail.startswith(":"):
                eq = tail.find("=")
                ty = _type_from_annotation(tail[1:eq] if eq > 0 else tail[1:])
            elif tail.startswith("="):
                ty = _type_from_initializer(tail[1:])
            else:
                continue
            if not ty:
                continue
            if name in out and out[name] != ty:
                ambiguous.add(name)
            out[name] = ty
    for name in ambiguous:
        out.pop(name, None)
    return out


def _chain_at(text: str, after: int) -> tuple[str, ...]:
    """The field chain starting at the `.` at `after`: `("inner", "ptr")` for
    `err.inner.ptr.as_ptr()`.

    A segment followed by `(` or `::` is a METHOD, not a field, and ends the
    chain before itself — `as_ptr()` above. `.await` ends it too."""
    fields: list[str] = []
    i = after
    while i < len(text) and text[i] == ".":
        j = i + 1
        while j < len(text) and text[j].isspace():
            j += 1
        seg = _read_ident(text, j)
        if not seg or seg in _KEYWORD_SEGMENTS:
            break
        k = j + len(seg)
        while k < len(text) and text[k].isspace():
            k += 1
        if k < len(text) and (text[k] == "(" or text.startswith("::", k)):
            break
        fields.append(seg)
        i = k
    return tuple(fields)


def read_field_paths(
    rel_path: str, src: str, *, types: Collection[str],
) -> tuple[FieldPath, ...]:
    """Every member path `src` reads on a value whose type is in `types`.

    `types` is the set of names the layout phase is authoring for one component
    — a path rooted anywhere else constrains nothing this component can decide,
    and admitting it would gate a component on another's design.

    A path that is a strict PREFIX of another on the same root is dropped: the
    probe for `Error.inner.ptr` already requires `Error.inner` to exist, and one
    item per constraint keeps the diagnostic pointing at the deepest hop that
    actually failed."""
    wanted = set(types)
    locals_ = {n: t for n, t in _local_types(src).items() if t in wanted}
    if not locals_:
        return ()
    found: dict[tuple[str, tuple[str, ...]], str] = {}
    for lineno, raw in enumerate(src.splitlines(), start=1):
        line = _strip_line_comment(raw)
        for name, ty in locals_.items():
            at = 0
            while (at := line.find(name, at)) >= 0:
                end = at + len(name)
                if not _word_at(line, at, name):
                    at = end
                    continue
                at = end
                if end >= len(line) or line[end] != ".":
                    continue
                chain = _chain_at(line, end)
                if chain:
                    found.setdefault((ty, chain), f"{rel_path}:{lineno}")
    return tuple(
        FieldPath(ty, chain, origin)
        for (ty, chain), origin in sorted(found.items())
        if not _has_longer(found, ty, chain))


def _has_longer(
    found: dict[tuple[str, tuple[str, ...]], str], ty: str,
    chain: tuple[str, ...],
) -> bool:
    return any(t == ty and c[:len(chain)] == chain and len(c) > len(chain)
               for t, c in found)


def field_path_requirements_text(paths: Iterable[FieldPath]) -> str:
    """The paths as an authoring instruction, or "" when there are none.

    The prompt half of the constraint. The compile gate can only REJECT, and a
    rejection costs a whole authoring attempt; naming the paths up front is what
    makes attempt 1 able to satisfy them."""
    rows = [f"  - `{p.expr}`" + (f"   (read at {p.origin})" if p.origin else "")
            for p in paths]
    if not rows:
        return ""
    return ("\n\nMEMBER PATHS THE CRATE'S OWN TESTS READ — these are the crate's "
            "existing tests, they are WRITE-PROTECTED, and no later stage may "
            "edit them. A definition that does not provide these names at these "
            "depths cannot compile, and the whole cluster fails to build with "
            "it:\n" + "\n".join(rows)
            + "\nKeep every name in every path, spelled exactly as shown. You "
            "are free to choose the TYPE of each member; you are not free to "
            "rename it or to flatten the path.\n")
