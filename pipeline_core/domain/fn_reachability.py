"""Which functions no test can execute, proved from the compiler's own view (pure).

task0c asked a model whether a function was testable (the retrospective) and
then counted failed attempts (the budget). Whether an integration test can run
a function is not an opinion: it is a fact about the crate's call and dispatch
structure, and rustc states both halves of it.

- **Who can name it.** rustdoc's JSON for the library, taken without
  `--document-private-items` and with `--document-hidden-items`, lists exactly
  the items reachable by a public path (`#[doc(hidden)]` ones included, because
  a test can still name them) and, per item, its span. A function with no such
  span, on a type that is not in the index and is spelled by no public
  signature, cannot be called from `tests/` by name, and no value of that type
  can be obtained either.
- **Who calls it.** The crate's generic MIR (`-Zunpretty=mir`) lists every call
  with its resolved callee, every fn item taken as a value, and every unsizing
  coercion to `dyn Trait`. A trait-impl method runs only through a call that
  names it, through a vtable built by an unsizing coercion of its self type, or
  through a generic bound the self type flows into.

A function is **unreachable by construction** when the first half says no
external code can name it or hold its type, and the second half finds no use of
it. The failure direction is fixed: anything this module cannot read (a MIR
shape it does not know, a signature it cannot tokenize, a macro body, a
`Drop`, a self type the crate does not define) leaves the function testable.
The result may be too small. It must never name a function a test could run;
`scripts/measure_fn_reachability.py` checks that against every function any
stored measurement saw executed, and task0c checks it again on every reading
(an executed function with a proof is a counterexample, logged, and the proof
is dropped).

**What this does not decide.** A function the proof leaves testable may still
be executed by no test; that stays with the attempt budget.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.rust_idents import container_identity, impl_self_type

# rustdoc item kinds that DEFINE a type a test could name or hold.
_TYPE_KINDS = frozenset({"struct", "enum", "union", "trait", "type_alias", "trait_alias",
                         "primitive", "assoc_type"})

# Callees that take a type as a generic argument and impose no bound on it that
# a user trait could satisfy (checked against the std signatures): constructors
# and plumbing. Everything else that receives a type this way is treated as a
# flow into code that may dispatch any trait the type implements. `from_residual`
# is NOT here: `Result`'s impl converts the error with `From`.
_PLUMBING = frozenset({
    "Some", "None", "Ok", "Err", "Continue", "Break", "branch", "new", "with_capacity",
    "push", "pop", "len", "is_empty", "as_ptr", "as_mut_ptr", "from_raw", "into_raw",
    "leak", "forget", "null", "null_mut", "dangling", "size_of", "align_of", "as_ref",
    "as_mut", "take", "replace", "swap", "new_unchecked", "drop", "drop_in_place",
})

# A std trait whose blanket impl calls another trait's method on the same type:
# `<S as ToOwned>::to_owned` runs `<S as Clone>::clone`.
_BLANKET_DISPATCH: Mapping[str, frozenset[str]] = {
    "ToOwned": frozenset({"Clone"}),
    "ToString": frozenset({"Display"}),
    "Into": frozenset({"From"}),
    "TryInto": frozenset({"TryFrom"}),
    "IntoIterator": frozenset({"Iterator"}),
    "Extend": frozenset({"Iterator"}),
}

# Never proved: the call is the compiler's, at every scope exit.
_NEVER_PROVED_TRAITS = frozenset({"Drop"})


@dataclass(frozen=True, slots=True)
class WorldReading:
    """The two compiler outputs of one cluster world, or why there are none.
    Taken by the adapter, read by :func:`read_world`."""

    cluster: str
    mir: str = ""
    rustdoc: str = ""
    reason: str = ""

    @property
    def measured(self) -> bool:
        return not self.reason and bool(self.mir) and bool(self.rustdoc)


# --------------------------------------------------------------------- tokens

def idents(text: str) -> list[str]:
    """Every identifier-shaped token of `text`, in order (no regex)."""
    out: list[str] = []
    start = -1
    for i, ch in enumerate(text):
        word = ch == "_" or ch.isalnum()
        if word and start < 0:
            start = i
        elif not word and start >= 0:
            tok = text[start:i]
            if not tok[0].isdigit():
                out.append(tok)
            start = -1
    if start >= 0:
        tok = text[start:]
        if not tok[0].isdigit():
            out.append(tok)
    return out


def has_word(text: str, word: str) -> bool:
    """`word` occurs in `text` as a whole identifier."""
    n = len(word)
    pos = text.find(word)
    while pos >= 0:
        before = text[pos - 1] if pos else ""
        after = text[pos + n] if pos + n < len(text) else ""
        if not (before == "_" or before.isalnum()) and not (after == "_" or after.isalnum()):
            return True
        pos = text.find(word, pos + 1)
    return False


def defined_type_names(source_texts: Iterable[str]) -> frozenset[str]:
    """Names the crate itself defines with `struct` / `enum` / `union`."""
    out: set[str] = set()
    for text in source_texts:
        toks = idents(text)
        for i, tok in enumerate(toks[:-1]):
            if tok in ("struct", "enum", "union"):
                out.add(toks[i + 1])
    return frozenset(out)


def macro_idents(source_texts: Iterable[str]) -> frozenset[str]:
    """Every identifier spelled inside a `macro_rules!` body. An exported macro
    expands in the TEST crate, so a name its body spells is reachable from a
    test and appears in no MIR of this crate."""
    out: set[str] = set()
    for text in source_texts:
        pos = text.find("macro_rules!")
        while pos >= 0:
            open_at = text.find("{", pos)
            if open_at < 0:
                break
            depth, i = 0, open_at
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            out.update(idents(text[open_at:i]))
            pos = text.find("macro_rules!", i)
    return frozenset(out)


# -------------------------------------------------------------- public surface

@dataclass(frozen=True, slots=True)
class PublicSurface:
    """What rustdoc's JSON says an outside crate can reach."""

    # Names of types/traits/aliases in the public index.
    nameable: frozenset[str] = frozenset()
    # Every identifier any public item spells: signatures, bounds, fields,
    # impls, associated types. A private type named here can still reach a test
    # as a value (a return type, a `Deref` target, a callback argument).
    exposed: frozenset[str] = frozenset()
    # `file -> ((first_line, last_line), ...)` of every public function.
    fn_spans: Mapping[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    # A public function returns `impl Trait`: any type behind it is unnamed.
    opaque_return: bool = False

    def lists_fn(self, file: str, first_line: int) -> bool:
        return any(a <= first_line <= b for a, b in self.fn_spans.get(file, ()))


def _walk_strings(node: object, out: set[str]) -> None:
    if isinstance(node, dict):
        for v in node.values():
            _walk_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_strings(v, out)
    elif isinstance(node, str):
        out.update(idents(node))


def _has_key(node: object, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_has_key(v, key) for v in node)
    return False


def public_surface(rustdoc_json: str) -> PublicSurface | None:
    """The surface, or None when the text is not a rustdoc JSON this reader
    understands (no reading is taken then; nothing is proved)."""
    try:
        doc = json.loads(rustdoc_json)
    except ValueError:
        return None
    index = doc.get("index") if isinstance(doc, dict) else None
    if not isinstance(index, dict) or not index:
        return None
    nameable: set[str] = set()
    exposed: set[str] = set()
    spans: dict[str, list[tuple[int, int]]] = {}
    opaque = False
    for item in index.values():
        if not isinstance(item, dict) or not isinstance(item.get("inner"), dict) or not item["inner"]:
            continue
        kind = next(iter(item["inner"]))
        inner = item["inner"][kind]
        if kind in ("module", "use"):
            continue
        name = item.get("name")
        if kind in _TYPE_KINDS and isinstance(name, str):
            nameable.add(name)
        _walk_strings(inner, exposed)
        if isinstance(name, str):
            exposed.add(name)
        if kind == "function":
            sig = inner.get("sig") if isinstance(inner, dict) else None
            out_ty = sig.get("output") if isinstance(sig, dict) else None
            if _has_key(out_ty, "impl_trait"):
                opaque = True
            span = item.get("span")
            if isinstance(span, dict) and isinstance(span.get("filename"), str):
                try:
                    spans.setdefault(span["filename"], []).append(
                        (int(span["begin"][0]), int(span["end"][0])))
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
    return PublicSurface(
        nameable=frozenset(nameable), exposed=frozenset(exposed),
        fn_spans={k: tuple(v) for k, v in spans.items()}, opaque_return=opaque)


# ------------------------------------------------------------------------ MIR

@dataclass(frozen=True, slots=True)
class MirBlock:
    """One top-level item of the dump: its header line and body lines."""

    header: str
    lines: tuple[str, ...]


def parse_mir(text: str) -> tuple[MirBlock, ...]:
    """Split a `-Zunpretty=mir` dump into its `fn` / `const` / `static` items.
    Allocation tables and comments are not items. A shape this does not know
    is simply not a block, and its text is scanned nowhere — so the dump's
    shape is pinned by the committed fixture, not trusted."""
    blocks: list[MirBlock] = []
    header = ""
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if not inside:
            if line.startswith(("fn ", "const ", "static ", "pub fn ", "unsafe fn ")):
                if line.rstrip().endswith("{"):
                    header, body, inside = line, [], True
                else:
                    blocks.append(MirBlock(line, ()))
            continue
        if line == "}":
            blocks.append(MirBlock(header, tuple(body)))
            inside = False
        else:
            body.append(line)
    return tuple(blocks)


def _callee(line: str) -> str | None:
    """The callee text of a call terminator line, or None for any other line."""
    marker = line.find(" -> [")
    if marker < 0 or " = " not in line:
        return None
    head = line[:marker].rstrip()
    if not head.endswith(")"):
        return None
    depth = 0
    open_at = -1
    for i in range(len(head) - 1, -1, -1):
        if head[i] == ")":
            depth += 1
        elif head[i] == "(":
            depth -= 1
            if depth == 0:
                open_at = i
                break
    if open_at < 0:
        return None
    return head[:open_at].split(" = ", 1)[1].strip()


def _call_or_item(line: str) -> str | None:
    """The callee of a call terminator, or the fn item a pointer coercion
    takes as a value (`_5 = <S as Tr>::m as fn(..) (PointerCoercion(ReifyFnPointer ..`)."""
    callee = _callee(line)
    if callee is not None:
        return callee
    if "FnPointer" in line and " = " in line and " as " in line:
        left = line.split(" = ", 1)[1].rpartition(" as ")[0].strip()
        return left or None
    return None


def _method_of(callee: str) -> str:
    """The last path segment's identifier, past any turbofish."""
    depth = 0
    cut = 0
    for i, ch in enumerate(callee):
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        elif depth == 0 and callee.startswith("::", i):
            cut = i + 2
    toks = idents(callee[cut:])
    return toks[0] if toks else ""


def _head_idents(callee: str) -> list[str]:
    """Identifiers of the callee's leading path, before its first generic list
    (`<Own<X> as Clone>::clone` -> [Own]; `ptr::Ref::<'_, T>::new` -> [ptr, Ref];
    `Option::<Ref<()>>::Some` -> [Option])."""
    s = callee[1:] if callee.startswith("<") else callee
    cut = len(s)
    for i, ch in enumerate(s):
        if ch == "<" or (ch == ":" and s.startswith("::<", i)):
            cut = i
            break
    return idents(s[:cut])


def _as_trait(callee: str) -> str:
    """`Tr` of a `<X as Tr>::m` callee, or ""."""
    if not callee.startswith("<"):
        return ""
    depth = 0
    for i, ch in enumerate(callee):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                inner = callee[1:i]
                parts = inner.rsplit(" as ", 1)
                if len(parts) == 2:
                    toks = idents(parts[1])
                    return toks[-1] if toks else ""
                return ""
    return ""


# ---------------------------------------------------------------- the oracle

@dataclass(frozen=True, slots=True)
class Verdict:
    """The oracle's reading of one function: `proof` is non-empty when it is
    unreachable by construction; otherwise `kept` says which fact keeps it
    testable (the reading `scripts/measure_fn_reachability.py` prints)."""

    site: FnSite
    proof: str = ""
    kept: str = ""


class _World:
    """Per-world indexes over the MIR, built once."""

    def __init__(self, blocks: tuple[MirBlock, ...]) -> None:
        self.blocks = blocks
        self._flow_cache: dict[str, str] = {}

    def _scan_lines(self) -> Iterable[tuple[MirBlock, str]]:
        for b in self.blocks:
            for ln in b.lines:
                yield b, ln

    def escapes(self, ty: str) -> str:
        """Why every trait impl of `ty` stays testable: it is coerced to a
        `dyn`, or flows as a generic argument into code that may dispatch it.
        "" when neither."""
        hit = self._flow_cache.get(ty)
        if hit is not None:
            return hit
        why = ""
        for b, ln in self._scan_lines():
            if "PointerCoercion(Unsize" in ln and has_word(ln, ty):
                why = f"`{ty}` is unsize-coerced in the crate ({ln.strip()[:80]})"
                break
            if "PointerCoercion(Unsize" in ln:
                src = ln.split(" as ", 1)[0].rsplit(" ", 1)[-1]
                if src.startswith("_"):
                    decl = f"let mut {src}:"
                    decl2 = f"let {src}:"
                    local = next((x for x in b.lines
                                  if x.strip().startswith((decl, decl2))), "")
                    if has_word(local or b.header, ty):
                        why = f"a value typed with `{ty}` is unsize-coerced to a dyn"
                        break
                continue
            callee = _call_or_item(ln)
            if callee is None or not has_word(callee, ty):
                continue
            if ty in _head_idents(callee):
                blanket = _BLANKET_DISPATCH.get(_as_trait(callee))
                if blanket:
                    why = f"`{ty}` is the receiver of a blanket-dispatching `{_as_trait(callee)}` call"
                    break
                continue
            if _method_of(callee) in _PLUMBING:
                continue
            why = f"`{ty}` flows as a generic argument into `{callee[:70]}`"
            break
        self._flow_cache[ty] = why
        return why

    def referenced(self, ty: str | None, name: str) -> str:
        """The first MIR use of `name` (as a path segment on `ty` when given,
        as a bare word otherwise), or ""."""
        for b, ln in self._scan_lines():
            if ty is None:
                if has_word(ln, name):
                    return f"used in `{b.header[:60]}`: {ln.strip()[:90]}"
                continue
            if not has_word(ln, ty):
                continue
            pos = ln.find("::" + name)
            while pos >= 0:
                end = pos + 2 + len(name)
                if end >= len(ln) or not (ln[end] == "_" or ln[end].isalnum()):
                    return f"used in `{b.header[:60]}`: {ln.strip()[:90]}"
                pos = ln.find("::" + name, pos + 1)
        return ""

    def trait_used(self, ty: str, trait: str) -> str:
        """Any call or fn-item reference of a method of `trait` on `ty`. A
        provided method of the trait runs the required ones (`Write::write_fmt`
        runs `write_str`), so one use keeps every impl of the trait on `ty`."""
        for b, ln in self._scan_lines():
            if not has_word(ln, ty):
                continue
            callee = _call_or_item(ln)
            if callee is not None and ty in _head_idents(callee) \
                    and _as_trait(callee) == trait:
                return f"used in `{b.header[:60]}`: {ln.strip()[:90]}"
        return ""

    def defines(self, rec: FnRecord, first_line: int) -> bool:
        """The dump holds this record's own body. A record the dump does not
        define was compiled out of the world the dump was taken in (or hides in
        a macro), and nothing may be proved about it.

        An impl method's header carries the span of its IMPL HEADER
        (`impl at src/ptr.rs:19:1: 21:15`), not of the method: the method
        belongs to the nearest impl that starts at or before its first line."""
        starts: set[int] = set()
        named: set[int] = set()
        for blk in self.blocks:
            if not blk.header.startswith(("fn ", "pub fn ", "unsafe fn ")):
                continue
            if "impl at " not in blk.header:
                if not rec.container and _header_name(blk.header) == rec.fn_name:
                    return True
                continue
            begin = _impl_begin(blk.header, rec.file)
            if begin is None:
                continue
            starts.add(begin)
            if _header_name(blk.header) == rec.fn_name:
                named.add(begin)
        before = [x for x in starts if x <= first_line]
        return bool(before) and max(before) in named


def _header_name(header: str) -> str:
    """The fn name a MIR item header declares."""
    tail = header.split(">::", 1)[-1] if "impl at " in header else header
    head = tail.split("(", 1)[0]
    toks = idents(head.rsplit("::", 1)[-1])
    return toks[-1] if toks else ""


def _impl_begin(header: str, file: str) -> int | None:
    """First line of the impl a method header names, when it is in `file`."""
    span = header.split("impl at ", 1)[1].split(">", 1)[0]
    parts = span.split(":")
    try:
        return int(parts[1]) if parts[0] == file else None
    except (IndexError, ValueError):
        return None


def _first_line(rec: FnRecord, text: str) -> int:
    return text.count("\n", 0, rec.sig_byte_start) + 1


def read_world(
    records: Iterable[FnRecord],
    *,
    mir_text: str,
    surface: PublicSurface | None,
    file_texts: Mapping[str, str],
) -> dict[FnSite, Verdict]:
    """A verdict per record. Empty when the MIR carries no block or there is
    no surface: with either missing nothing is proved."""
    blocks = parse_mir(mir_text)
    if surface is None or not blocks:
        return {}
    world = _World(blocks)
    crate_types = defined_type_names(file_texts.values())
    from_macros = macro_idents(file_texts.values())
    out: dict[FnSite, Verdict] = {}
    for rec in records:
        site = record_site(rec)
        text = file_texts.get(rec.file)
        if text is None or not rec.fn_name:
            continue
        v = _judge(rec, site, text, world, surface, crate_types, from_macros)
        prior = out.get(site)
        # A site several records share is proved only when each record is.
        if prior is not None and (not prior.proof or not v.proof):
            v = prior if not prior.proof else v
        out[site] = v
    return out


def _judge(rec: FnRecord, site: FnSite, text: str, world: _World,
           surface: PublicSurface, crate_types: frozenset[str],
           from_macros: frozenset[str]) -> Verdict:
    name = rec.fn_name
    if name in from_macros:
        return Verdict(site, kept="its name is spelled in a macro body that expands in the test crate")
    first = _first_line(rec, text)
    if surface.lists_fn(rec.file, first):
        return Verdict(site, kept="it is in the public index (a test can name it)")
    if not world.defines(rec, first):
        return Verdict(site, kept="the MIR dump does not define it (compiled out of the dumped "
                                  "world, or produced by a macro)")
    container = rec.container.strip()
    if container.startswith("trait"):
        return Verdict(site, kept="a trait's provided method is reached through any implementor")
    ident = container_identity(container) if container else None
    trait_path = ident.trait if ident else ()
    self_ty = impl_self_type(container) if container else None
    if not container:
        seen = world.referenced(None, name)
        if seen:
            return Verdict(site, kept=seen)
        return Verdict(site, proof=(
            f"free fn `{name}` is not in the public index and the crate's MIR "
            "never calls it or takes it as a value"))
    if rec.impl_self_generic or not self_ty or self_ty not in crate_types:
        return Verdict(site, kept="its self type is generic or not defined by the crate")
    if not trait_path:
        # An inherent method the index does not list.
        if rec.is_exactly_pub and rec.is_pub and (
                self_ty in surface.nameable or self_ty in surface.exposed):
            return Verdict(site, kept=f"`pub` method on `{self_ty}`, which an outside crate can hold")
        seen = world.referenced(self_ty, name)
        if seen:
            return Verdict(site, kept=seen)
        return Verdict(site, proof=(
            f"inherent method `{self_ty}::{name}` is not callable from outside and the "
            "crate's MIR never calls it or takes it as a value"))
    trait = trait_path[-1]
    if trait in _NEVER_PROVED_TRAITS:
        return Verdict(site, kept=f"`{trait}` runs at every scope exit, not at a call")
    if surface.opaque_return:
        return Verdict(site, kept="a public fn returns `impl Trait`, so a type behind it is unnamed")
    if self_ty in surface.nameable or self_ty in surface.exposed:
        return Verdict(site, kept=f"`{self_ty}` is nameable or spelled by a public signature")
    esc = world.escapes(self_ty)
    if esc:
        return Verdict(site, kept=esc)
    seen = world.trait_used(self_ty, trait) or world.referenced(self_ty, name)
    if seen:
        return Verdict(site, kept=seen)
    return Verdict(site, proof=(
        f"`{self_ty}` is not nameable and appears in no public signature; the crate's MIR "
        f"never calls `<{self_ty} as {trait}>::{name}`, takes it as a value, coerces "
        f"`{self_ty}` to a dyn, or passes it to generic code"))


def proofs(verdicts: Mapping[FnSite, Verdict]) -> dict[FnSite, str]:
    return {s: v.proof for s, v in verdicts.items() if v.proof}
