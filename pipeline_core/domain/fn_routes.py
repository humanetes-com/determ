"""Which public calls reach a function no test can name, read from one world's MIR (pure).

`domain.fn_reachability` decides whether a test can execute a function at all.
A function it leaves testable may still be one no test names: a trait method
on a type of a private module, a private helper. A prompt asked to execute it
has to be told which PUBLIC call gets there, and the crate's own MIR records
every step of that path:

- a value of the type is BUILT (an aggregate, `_3 = DisplayError::<M>(copy _1)`)
  or COERCED to a `dyn` (`_0 = copy _2 as Box<dyn Error …> (PointerCoercion(Unsize …`)
  inside some crate fn;
- that fn is called by name (`_5 = Error::from_display::<C>(…)`), taken as a
  value (`… as fn(…) (PointerCoercion(ReifyFnPointer …`), or stored in a field
  of a struct aggregate (`_1 = VTable { object_boxed: move _3, … }`) that some
  other fn reads and calls (`_2 = copy ((*_4).1: unsafe fn(…))`, `move _2(…)`);
- the walk goes up those edges to the first function rustdoc's public index
  lists.

The line this produces quotes what the dump shows and says nothing more: it
names the callers it found, never "only by". A shape this module cannot read
(a callee it cannot resolve to exactly one function, a call through a local it
cannot type, a macro body) gives no edge, so a route may be missing; it is
never invented. When the walk finds nothing, the caller keeps the generic
trait-dispatch text.

The same dump also says when one of the crate's own `<dyn Trait>` calls runs
only on the true edge of a formatter flag (anyhow's `Debug for Error` calls
the object's `Debug` only under `f.alternate()`). That call is quoted on every
route of that trait, a coercion route included. Another `<dyn Trait>` call
that is not on that edge stays off the quote: the line names the calls the
dump gates, and a second call does not erase them.
"""
from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.fn_execution import site_label
from pipeline_core.domain.fn_reachability import (
    MirBlock,
    PublicIndex,
    PublicSurface,
    call_or_item,
    defined_type_names,
    has_word,
    header_name,
    idents,
    impl_begin,
    parse_mir,
    record_first_line,
)
from pipeline_core.domain.models import FnRecord, FnSite, TypeRecord
from pipeline_core.domain.rust_idents import container_identity, impl_self_type

# Bumped whenever what this module computes changes: the strategy file's
# computed part is re-rendered when the stored version differs.
ROUTES_VERSION = "3"

MAX_ROUTES = 4
MAX_DEPTH = 8


# ---------------------------------------------------------------------- facts

@dataclass(frozen=True, slots=True)
class PublicRoute:
    """One path in the dump from a public item to where the target is reached."""

    public: str
    # The crate functions between the public item and the reach, outermost first.
    via: tuple[str, ...] = ()
    # What happens at the bottom of the path ("builds `DisplayError`",
    # "coerces `ErrorImpl` to a `dyn`"); "" when the bottom is a call of the
    # target itself.
    reach: str = ""
    # The variant the public item wraps the next hop's result in (`Err` for
    # `Option::context`), read from its body; "" when it returns it as is or
    # the dump does not say.
    side: str = ""

    def render(self) -> str:
        chain = " → ".join((self.public, *self.via))
        notes = [n for n in (self.reach, f"returned as its `{self.side}`" if self.side else "") if n]
        return f"{chain} ({'; '.join(notes)})" if notes else chain


@dataclass(frozen=True, slots=True)
class GatedDispatch:
    """A `dyn Trait` call of the crate's own that runs only on the true branch
    of a formatter flag."""

    caller: str
    callee: str
    flag: str

    def render(self) -> str:
        spelled = {"alternate": "`f.alternate()` is true (`{:#?}`)"}.get(self.flag, f"`{self.flag}`")
        return f"`{self.callee}` is called in {self.caller} only when {spelled}"


@dataclass(frozen=True, slots=True)
class FnRoutes:
    """What the dump says about reaching one function from the public API."""

    routes: tuple[PublicRoute, ...] = ()
    gated: tuple[GatedDispatch, ...] = ()

    def lines(self) -> list[str]:
        """Prompt comment lines ([] without a route)."""
        if not self.routes:
            return []
        shown = self.routes[:MAX_ROUTES]
        more = len(self.routes) > MAX_ROUTES
        out = ["// Reached from the public API (callers in this crate's MIR): "
               + "; ".join(r.render() for r in shown)
               + (f" (the first {MAX_ROUTES}; more public callers exist)"
                  if more else "")]
        out.extend(f"// {g.render()}" for g in self.gated)
        return out


@dataclass(frozen=True, slots=True)
class TypeFact:
    """A type the source declares `pub` that `tests/` cannot name."""

    name: str
    file: str
    built_by: tuple[PublicRoute, ...] = ()

    def never_line(self) -> str:
        return (f"NEVER: {self.name}  # pub in a private module of {self.file}, not in the "
                "public index: tests/ cannot name or import it")

    def types_line(self) -> str:
        if not self.built_by:
            return (f"{self.name}: not nameable from tests/; no public call that builds it "
                    "was found in the MIR")
        return (f"{self.name}: not nameable from tests/; built by "
                + "; ".join(r.render() for r in self.built_by))


@dataclass(frozen=True, slots=True)
class WorldFacts:
    """One cluster world's computed facts, persisted for the phases that run
    before t0c reads its own (t0b's strategy file, t0d's probes)."""

    cluster: str
    routes: Mapping[str, FnRoutes] = field(default_factory=dict)   # site label -> routes
    unnameable: tuple[TypeFact, ...] = ()
    item_names: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def routes_for(self, site: FnSite) -> FnRoutes:
        return self.routes.get(site_label(site), FnRoutes())

    def unnameable_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.unnameable)

    def to_dict(self) -> dict[str, object]:
        return {
            "cluster": self.cluster,
            "routes": {k: _routes_dict(v) for k, v in sorted(self.routes.items())},
            "unnameable": [{"name": t.name, "file": t.file,
                            "built_by": [_route_dict(r) for r in t.built_by]}
                           for t in self.unnameable],
            "item_names": {k: sorted(v) for k, v in sorted(self.item_names.items())},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> WorldFacts:
        routes_raw = raw.get("routes")
        unnameable_raw = raw.get("unnameable")
        names_raw = raw.get("item_names")
        return cls(
            cluster=str(raw.get("cluster", "")),
            routes={str(k): _routes_of(v) for k, v in routes_raw.items()}
            if isinstance(routes_raw, dict) else {},
            unnameable=tuple(
                TypeFact(str(t.get("name", "")), str(t.get("file", "")),
                         tuple(_route_of(r) for r in t.get("built_by", ()) if isinstance(r, dict)))
                for t in unnameable_raw if isinstance(t, dict))
            if isinstance(unnameable_raw, list) else (),
            item_names={str(k): frozenset(str(x) for x in v) for k, v in names_raw.items()
                        if isinstance(v, list)}
            if isinstance(names_raw, dict) else {},
        )


def _route_dict(r: PublicRoute) -> dict[str, object]:
    return {"public": r.public, "via": list(r.via), "reach": r.reach, "side": r.side}


def _route_of(raw: Mapping[str, object]) -> PublicRoute:
    via = raw.get("via")
    return PublicRoute(str(raw.get("public", "")),
                       tuple(str(x) for x in via) if isinstance(via, list) else (),
                       str(raw.get("reach", "")), str(raw.get("side", "")))


def _routes_dict(fr: FnRoutes) -> dict[str, object]:
    return {"routes": [_route_dict(r) for r in fr.routes],
            "gated": [{"caller": g.caller, "callee": g.callee, "flag": g.flag} for g in fr.gated]}


def _routes_of(raw: object) -> FnRoutes:
    if not isinstance(raw, dict):
        return FnRoutes()
    routes = raw.get("routes")
    gated = raw.get("gated")
    return FnRoutes(
        tuple(_route_of(r) for r in routes if isinstance(r, dict)) if isinstance(routes, list) else (),
        tuple(GatedDispatch(str(g.get("caller", "")), str(g.get("callee", "")), str(g.get("flag", "")))
              for g in gated if isinstance(g, dict)) if isinstance(gated, list) else ())


def facts_to_json(facts: Iterable[WorldFacts], *, source: str = "") -> str:
    """``source`` is the fingerprint of the crate source the facts were
    measured from (`measurement.tree_content_sha` of `src/`): a stored file
    is current only for the version AND the source it describes."""
    return json.dumps({"version": ROUTES_VERSION, "source": source,
                       "worlds": [f.to_dict() for f in facts]}, indent=1, sort_keys=True) + "\n"


def facts_source(text: str) -> str:
    """The source fingerprint a stored facts file records ("" when absent)."""
    try:
        raw = json.loads(text)
    except ValueError:
        return ""
    return str(raw.get("source") or "") if isinstance(raw, dict) else ""


def facts_from_json(text: str) -> tuple[str, tuple[WorldFacts, ...]] | None:
    """`(version, worlds)`, or None for a text this reader does not understand."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("worlds"), list):
        return None
    return (str(raw.get("version", "")),
            tuple(WorldFacts.from_dict(w) for w in raw["worlds"] if isinstance(w, dict)))


# ------------------------------------------------------------------- the dump

def _tidy(text: str) -> str:
    """A container fragment as the extractor spaces it (`Option < T >`) in
    the spelling a test writes (`Option<T>`)."""
    s = " ".join(text.split())
    for a, b in (("< ", "<"), (" <", "<"), (" >", ">"), (" ,", ","), (":: ", "::"),
                 (" ::", "::"), ("& ", "&"), ("' ", "'")):
        s = s.replace(a, b)
    return s


def _split_impl(container: str) -> tuple[str, str]:
    """`(trait text, self text)` of an `impl` container; trait "" when inherent."""
    core = " ".join(container.split())
    if core.startswith("impl"):
        core = core[4:].lstrip()
    if core.startswith("<"):
        depth = 0
        for i, ch in enumerate(core):
            depth += ch == "<"
            depth -= ch == ">"
            if depth == 0:
                core = core[i + 1:].lstrip()
                break
    depth = 0
    for i, ch in enumerate(core):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0 and core.startswith(" for ", i):
            return core[:i].strip(), core[i + 5:].strip()
    return "", core.strip()


def _trait_last(container: str) -> str:
    path = container_identity(container).trait
    toks = idents(path[-1]) if path else []
    return toks[0] if toks else ""


def _segments(path: str) -> list[str]:
    """`path` split at `::` outside any `<…>` / `(…)`."""
    out: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(path):
        ch = path[i]
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        elif depth == 0 and path.startswith("::", i):
            out.append(path[start:i])
            start = i + 2
            i += 2
            continue
        i += 1
    out.append(path[start:])
    return [s for s in (x.strip() for x in out) if s]


def _type_head(text: str) -> str:
    """The first type identifier of a type expression (`&dyn Error` -> Error)."""
    for tok in idents(text):
        if tok not in ("mut", "dyn", "const", "impl", "std", "core", "alloc", "crate",
                       "boxed", "error", "fmt", "ptr", "sync"):
            return tok
    return ""


@dataclass(slots=True)
class _Fn:
    block: MirBlock
    name: str
    record: FnRecord | None
    is_const: bool
    # A closure is its own MIR item; what it calls, its creator calls.
    owner: int | None = None


def _item_path(header: str) -> str:
    """`error::<impl at …>::backtrace::{closure#0}` of an item header."""
    rest = header.split(" ", 1)[1] if " " in header else header
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "(" and depth == 0:
            return rest[:i]
    return rest


@dataclass(frozen=True, slots=True)
class _Callee:
    name: str
    self_ty: str = ""
    trait: str = ""
    modules: tuple[str, ...] = ()
    kind: str = "free"   # free | inherent | trait | foreign


def _parse_callee(callee: str, crate_types: frozenset[str]) -> _Callee | None:
    segs = _segments(callee)
    if not segs:
        return None
    first = segs[0]
    plain = [s for s in segs if not s.startswith("<")]
    names = [idents(s)[0] for s in plain if idents(s)]
    if first.startswith("<") and first.endswith(">"):
        inner = first[1:-1].strip()
        if not names:
            return None
        name = names[0]
        depth = 0
        for i, ch in enumerate(inner):
            if ch in "<(":
                depth += 1
            elif ch in ">)":
                depth -= 1
            elif depth == 0 and inner.startswith(" as ", i):
                trait_toks = idents(inner[i + 4:].split("<", 1)[0])
                return _Callee(name, _type_head(inner[:i]), trait_toks[-1] if trait_toks else "",
                               kind="trait")
        if inner.startswith("impl "):
            return _Callee(name, _type_head(inner[5:]), kind="inherent")
        return _Callee(name, _type_head(inner), kind="inherent")
    if not names:
        return None
    # `module::<impl X>::name`
    for s in segs:
        if s.startswith("<impl ") and s.endswith(">"):
            return _Callee(names[-1], _type_head(s[6:-1]), kind="inherent")
    name = names[-1]
    if len(names) >= 2 and names[-2][:1].isupper():
        if names[-2] in crate_types:
            return _Callee(name, names[-2], kind="inherent")
        return _Callee(name, names[-2], kind="foreign")
    return _Callee(name, modules=tuple(names[:-1]), kind="free")


def _local_type(block: MirBlock, local: str) -> str:
    for decl in (f"let mut {local}:", f"let {local}:"):
        for ln in block.lines:
            s = ln.strip()
            if s.startswith(decl):
                return s[len(decl):].rstrip(";").strip()
    return ""


def _struct_of(type_text: str) -> str:
    t = type_text.strip()
    while t.startswith(("&", "*")):
        t = t.lstrip("&*").strip()
        for kw in ("mut ", "const "):
            if t.startswith(kw):
                t = t[len(kw):]
    toks = idents(t.split("<", 1)[0])
    return toks[-1] if toks else ""


def _fn_field_reads(line: str) -> list[tuple[str, int]]:
    """`(local, field index)` of every fn-pointer field this line reads:
    `copy ((*_4).1: unsafe fn(…) -> …)` or `copy (_4.1: fn(…))`."""
    out: list[tuple[str, int]] = []
    pos = line.find(".")
    while pos >= 0:
        j = pos + 1
        while j < len(line) and line[j].isdigit():
            j += 1
        if j > pos + 1 and line.startswith(": ", j):
            k = pos - 1
            if k >= 0 and line[k] == ")":
                k -= 1
            end = k + 1
            while k >= 0 and (line[k].isalnum() or line[k] == "_"):
                k -= 1
            local = line[k + 1:end]
            ty_end = line.find(");", j)
            ty = line[j + 2:ty_end if ty_end > 0 else len(line)]
            if local.startswith("_") and "fn(" in ty:
                out.append((local, int(line[pos + 1:j])))
        pos = line.find(".", pos + 1)
    return out


def _aggregate(line: str) -> tuple[str, list[str]] | None:
    """`(struct name, operand per field in order)` of `_1 = S { a: move _2, … };`."""
    s = line.strip()
    if " = " not in s or " -> " in s or not s.endswith("};"):
        return None
    rhs = s.split(" = ", 1)[1]
    brace = rhs.find(" {")
    if brace < 0:
        return None
    head = rhs[:brace]
    name_toks = idents(head.split("::<", 1)[0])
    if not name_toks:
        return None
    body = rhs[brace + 2:].rstrip(";").rstrip("}").strip()
    operands = []
    for part in body.split(", "):
        if ":" in part:
            operands.append(part.split(":", 1)[1].strip())
    return name_toks[-1], operands


class MirGraph:
    """The call, value and vtable edges of one world's dump, keyed by the
    extractor's records."""

    def __init__(self, mir_text: str, records: Iterable[FnRecord],
                 file_texts: Mapping[str, str], surface: PublicSurface) -> None:
        self.surface = surface
        self.file_texts = file_texts
        self.records = [r for r in records if r.fn_name]
        self.crate_types = defined_type_names(file_texts.values())
        self.fns: list[_Fn] = []
        self._by_site: dict[FnSite, int] = {}
        self._first: dict[int, int] = {}
        blocks = parse_mir(mir_text)
        self._index(blocks)
        self.callers: dict[int, set[int]] = {i: set() for i in range(len(self.fns))}
        self._edges()

    # ---------------------------------------------------------------- index

    def _index(self, blocks: tuple[MirBlock, ...]) -> None:
        starts: dict[str, set[int]] = {}
        for b in blocks:
            if "impl at " in b.header:
                span = b.header.split("impl at ", 1)[1].split(">", 1)[0]
                f = span.split(":", 1)[0]
                begin = impl_begin(b.header, f)
                if begin is not None:
                    starts.setdefault(f, set()).add(begin)
        keyed: dict[tuple[str, int, str], list[FnRecord]] = {}
        free: dict[str, list[FnRecord]] = {}
        for r in self.records:
            text = self.file_texts.get(r.file)
            if text is None:
                continue
            first = record_first_line(r, text)
            c = r.container.strip()
            if c.startswith("impl"):
                before = [s for s in starts.get(r.file, ()) if s <= first]
                if before:
                    keyed.setdefault((r.file, max(before), r.fn_name), []).append(r)
            elif not c or c.startswith("mod"):
                free.setdefault(r.fn_name, []).append(r)
        ambiguous: set[FnSite] = set()
        for b in blocks:
            is_const = b.header.startswith(("const ", "static "))
            name = header_name(b.header) if not is_const else ""
            rec: FnRecord | None = None
            if not is_const and "impl at " in b.header:
                span = b.header.split("impl at ", 1)[1].split(">", 1)[0]
                f = span.split(":", 1)[0]
                begin = impl_begin(b.header, f)
                cands = keyed.get((f, begin, name), []) if begin is not None else []
                rec = cands[0] if len(cands) == 1 else None
            elif not is_const:
                cands = free.get(name, [])
                if len(cands) > 1:
                    path = _segments(b.header[len("fn "):].split("(", 1)[0])
                    mods = {idents(s)[0] for s in path[:-1] if idents(s)}
                    cands = [r for r in cands
                             if r.file.rsplit("/", 1)[-1].removesuffix(".rs") in mods
                             or any(m in r.container for m in mods)]
                rec = cands[0] if len(cands) == 1 else None
            idx = len(self.fns)
            self.fns.append(_Fn(b, name, rec, is_const))
            if rec is not None:
                site = record_site(rec)
                if site in ambiguous:
                    self.fns[idx].record = None
                elif site in self._by_site:
                    # Two blocks for one site (a cfg variant): name neither.
                    first_idx = self._by_site.pop(site)
                    self._first.pop(first_idx, None)
                    self.fns[first_idx].record = None
                    self.fns[idx].record = None
                    ambiguous.add(site)
                else:
                    self._by_site[site] = idx
                    self._first[idx] = record_first_line(rec, self.file_texts[rec.file])
        by_path = {_item_path(fn.block.header): i for i, fn in enumerate(self.fns)
                   if not fn.is_const and "::{closure" not in _item_path(fn.block.header)}
        for fn in self.fns:
            item = _item_path(fn.block.header)
            if "::{closure" in item:
                fn.owner = by_path.get(item.split("::{closure", 1)[0])

    def _resolve(self, callee: str) -> list[int]:
        parsed = _parse_callee(callee, self.crate_types)
        if parsed is None or parsed.kind == "foreign":
            return []
        out: list[int] = []
        for i, fn in enumerate(self.fns):
            rec = fn.record
            if rec is None or fn.is_const or rec.fn_name != parsed.name:
                continue
            c = rec.container.strip()
            if parsed.kind == "trait":
                if not c.startswith("impl") or _trait_last(c) != parsed.trait:
                    continue
                if impl_self_type(c) != parsed.self_ty:
                    continue
            elif parsed.kind == "inherent":
                if not c.startswith("impl") or _trait_last(c) or impl_self_type(c) != parsed.self_ty:
                    continue
            elif c and not c.startswith("mod"):
                continue
            elif parsed.modules and len(self._free_named(parsed.name)) > 1:
                stem = rec.file.rsplit("/", 1)[-1].removesuffix(".rs")
                if stem not in parsed.modules and not any(m in c for m in parsed.modules):
                    continue
            out.append(i)
        return out if len(out) == 1 else []

    def _free_named(self, name: str) -> list[int]:
        return [i for i, fn in enumerate(self.fns)
                if fn.record is not None and fn.record.fn_name == name
                and (not fn.record.container.strip() or fn.record.container.strip().startswith("mod"))]

    def _edges(self) -> None:
        # Fn pointers stored in struct aggregates: (struct, field index) -> fns.
        slots: dict[tuple[str, int], set[int]] = {}
        for fn in self.fns:
            reified: dict[str, list[int]] = {}
            for ln in fn.block.lines:
                if "ReifyFnPointer" in ln and " = " in ln and " as " in ln:
                    local = ln.strip().split(" = ", 1)[0].strip()
                    item = call_or_item(ln)
                    if item:
                        reified[local] = self._resolve(item)
            for ln in fn.block.lines:
                agg = _aggregate(ln)
                if agg is None:
                    continue
                struct, operands = agg
                for k, op in enumerate(operands):
                    local = op.split()[-1] if op else ""
                    for target in reified.get(local, ()):
                        slots.setdefault((struct, k), set()).add(target)
        for i, fn in enumerate(self.fns):
            if fn.is_const:
                continue
            g = fn.owner if fn.owner is not None else i
            for ln in fn.block.lines:
                callee = call_or_item(ln)
                if callee and not callee.split()[-1].startswith("_"):
                    for t in self._resolve(callee):
                        if t != g:
                            self.callers[t].add(g)
                for local, k in _fn_field_reads(ln):
                    struct = _struct_of(_local_type(fn.block, local))
                    for t in slots.get((struct, k), ()):
                        if t != g:
                            self.callers[t].add(g)

    # ---------------------------------------------------------------- reads

    def wrapped_as(self, caller: int, callee: int) -> str:
        """The enum variant `caller` wraps `callee`'s result in before it
        returns it (`_0 = Result::<T, Error>::Err(move _5)` after
        `_5 = Error::from_display::<C>(…)`), or ""."""
        block = self.fns[caller].block
        dests: set[str] = set()
        for ln in block.lines:
            target = call_or_item(ln)
            if target and " = " in ln and callee in self._resolve(target):
                dests.add(ln.strip().split(" = ", 1)[0].strip())
        for ln in block.lines:
            s = ln.strip()
            if not s.startswith("_0 = ") or " -> " in s:
                continue
            for variant in ("Err", "Ok", "Some"):
                if any(f"::{variant}(move {d})" in s or f"::{variant}(copy {d})" in s for d in dests):
                    return variant
        return ""

    def passes_through(self, caller: int, callee: int) -> bool:
        """`caller` returns `callee`'s result as it is: the call's
        destination is `_0`, or `_0` is moved/copied from it."""
        block = self.fns[caller].block
        dests: set[str] = set()
        for ln in block.lines:
            target = call_or_item(ln)
            if target and " = " in ln and callee in self._resolve(target):
                dests.add(ln.strip().split(" = ", 1)[0].strip())
        if "_0" in dests:
            return True
        for ln in block.lines:
            s = ln.strip()
            if s.startswith("_0 = ") and any(
                    s in (f"_0 = move {d};", f"_0 = copy {d};") for d in dests):
                return True
        return False

    def wrapped_along(self, path: Sequence[int]) -> str:
        """The variant the result is wrapped in on the way up ``path``
        (public fn first, then each callee): the first hop that wraps, as
        long as every hop above it passes its callee's result through. A
        public fn that returns a helper's `Err(…)` as it is used to read
        as unwrapped, and the ask omitted `unwrap_err`."""
        for n in range(len(path) - 1):
            got = self.wrapped_as(path[n], path[n + 1])
            if got:
                return got
            if not self.passes_through(path[n], path[n + 1]):
                return ""
        return ""

    def is_public(self, i: int) -> bool:
        rec = self.fns[i].record
        return rec is not None and i in self._first and self.surface.lists_fn(rec.file, self._first[i])

    def render(self, i: int) -> str:
        fn = self.fns[i]
        rec = fn.record
        if rec is None:
            return f"`{fn.name}`"
        c = rec.container.strip()
        if c.startswith("impl"):
            trait, self_txt = _split_impl(c)
            if trait:
                return f"`<{_tidy(self_txt)} as {_tidy(trait)}>::{rec.fn_name}`"
            return f"`{impl_self_type(c) or _tidy(self_txt)}::{rec.fn_name}`"
        if c.startswith("mod"):
            mod = c[3:].strip()
            return f"`{mod}::{rec.fn_name}`" if mod else f"`{rec.fn_name}`"
        return f"`{rec.fn_name}`"

    def index_of(self, site: FnSite) -> int | None:
        return self._by_site.get(site)

    def build_sites(self, ty: str) -> list[int]:
        """Fns whose body builds a value of `ty` as an aggregate."""
        out: list[int] = []
        for i, fn in enumerate(self.fns):
            if fn.is_const or fn.name == ty:
                continue
            for ln in fn.block.lines:
                s = ln.strip()
                if " = " not in s or " -> " in s:
                    continue
                rhs = s.split(" = ", 1)[1]
                head = rhs.split("::<", 1)[0].split("(", 1)[0].split(" {", 1)[0]
                # An aggregate has no terminator: a struct literal, a tuple
                # struct, an enum variant (`Ty::Variant(…)`), or a unit struct
                # / unit variant spelled bare (`_0 = Wrap;`).
                tail = rhs[len(head):]
                bare = tail.rstrip() in (";", "") and not head.startswith(("copy ", "move ", "const ", "&"))
                if ty in idents(head) and (tail.startswith(("(", " {", "::<")) or bare):
                    out.append(i)
                    break
        return out

    def unsize_sites(self, ty: str) -> list[int]:
        """Fns that coerce a value typed with `ty` to a `dyn`."""
        out: list[int] = []
        for i, fn in enumerate(self.fns):
            if fn.is_const:
                continue
            for ln in fn.block.lines:
                if "PointerCoercion(Unsize" not in ln or "dyn " not in ln:
                    continue
                src = ln.split(" as ", 1)[0].rsplit(" ", 1)[-1]
                if src.startswith("_") and has_word(_local_type(fn.block, src), ty):
                    out.append(i)
                    break
        return out

    def routes_from(self, starts: Iterable[tuple[int, str]], *, skip: int | None = None) -> tuple[PublicRoute, ...]:
        """Shortest paths up the caller edges from each start to a public fn.
        A start is `(fn, reach)`; `skip` is the target itself, which a path
        names only as its bottom."""
        found: list[PublicRoute] = []
        seen_public: set[str] = set()
        for start, reach in starts:
            if self.is_public(start) and start != skip:
                r = PublicRoute(self.render(start), (), reach)
                if r.public not in seen_public:
                    seen_public.add(r.public)
                    found.append(r)
                continue
            parent: dict[int, int] = {start: -1}
            depth = {start: 0}
            q: deque[int] = deque([start])
            while q and len(found) <= MAX_ROUTES:
                cur = q.popleft()
                if depth[cur] >= MAX_DEPTH:
                    continue
                for nxt in sorted(self.callers.get(cur, ())):
                    if nxt in parent:
                        continue
                    parent[nxt] = cur
                    depth[nxt] = depth[cur] + 1
                    if self.is_public(nxt):
                        chain: list[int] = []
                        path: list[int] = [nxt]
                        x = cur
                        while x != -1:
                            path.append(x)
                            if x != skip:
                                chain.append(x)
                            x = parent[x]
                        r = PublicRoute(self.render(nxt), tuple(self.render(c) for c in chain), reach,
                                        self.wrapped_along(path))
                        if r.public not in seen_public:
                            seen_public.add(r.public)
                            found.append(r)
                        continue
                    q.append(nxt)
            if len(found) > MAX_ROUTES:
                break
        # One past the cap is kept so the line can say the list is partial.
        return tuple(found[:MAX_ROUTES + 1])

    # --------------------------------------------------------- formatter flags

    def gated_dispatches(self, trait: str) -> tuple[list[GatedDispatch], int]:
        """The crate's `<dyn … as trait>::…` calls that run only when a
        formatter flag is true, and how many such calls there are in all."""
        gated: list[GatedDispatch] = []
        total = 0
        for i, fn in enumerate(self.fns):
            if fn.is_const:
                continue
            bbs, edges, flagged = _cfg(fn.block)
            for bb, lines in bbs.items():
                for ln in lines:
                    callee = call_or_item(ln)
                    if not callee or not callee.startswith("<dyn "):
                        continue
                    parsed = _parse_callee(callee, self.crate_types)
                    if parsed is None or parsed.trait != trait:
                        continue
                    total += 1
                    for flag, true_edges in flagged.items():
                        if bb in _reach(edges, "bb0") and bb not in _reach(edges, "bb0", drop=true_edges):
                            gated.append(GatedDispatch(self.render(i), callee.split("::<", 1)[0], flag))
                            break
        return gated, total


def _cfg(block: MirBlock) -> tuple[dict[str, list[str]], dict[str, list[str]],
                                   dict[str, set[tuple[str, str]]]]:
    """Basic blocks, successor edges (unwind edges dropped), and per formatter
    flag the set of `(from, to)` edges taken when the flag is true."""
    bbs: dict[str, list[str]] = {}
    current: str | None = None
    for ln in block.lines:
        s = ln.strip()
        if current is None and s.startswith("bb") and s.endswith("{"):
            current = s.split(":", 1)[0].split(" ", 1)[0]
            bbs[current] = []
        elif current is not None and ln == "    }":
            current = None
        elif current is not None:
            bbs[current].append(s)
    edges: dict[str, list[str]] = {}
    flag_locals: dict[str, str] = {}
    flagged: dict[str, set[tuple[str, str]]] = {}
    for bb, lines in bbs.items():
        out: list[str] = []
        for s in lines:
            if "Formatter::<'_>::alternate(" in s and " = " in s:
                flag_locals[s.split(" = ", 1)[0].strip()] = "alternate"
            # The terminator's target list is the LAST ` -> [`: a fn type in
            # a turbofish or a reified pointer spells `->` too.
            if " -> [" in s:
                tail = "[" + s.rsplit(" -> [", 1)[1].rstrip(";")
            elif s.startswith("goto -> "):
                tail = s[len("goto -> "):].rstrip(";")
            else:
                continue
            if tail.startswith("["):
                for entry in tail.strip("[]").split(","):
                    key, _, val = entry.strip().partition(":")
                    val = val.strip()
                    if key.strip() == "unwind" or not val.startswith("bb"):
                        continue
                    out.append(val)
                    if s.startswith("switchInt(") and key.strip() == "otherwise":
                        local = s[len("switchInt("):].split(")", 1)[0].split()[-1]
                        flag = flag_locals.get(local)
                        if flag:
                            flagged.setdefault(flag, set()).add((bb, val))
            elif tail.startswith("bb"):
                out.append(tail.split()[0])
        edges[bb] = out
    return bbs, edges, flagged


def _reach(edges: Mapping[str, list[str]], start: str,
           drop: set[tuple[str, str]] | None = None) -> set[str]:
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt in edges.get(cur, ()):
            if drop and (cur, nxt) in drop:
                continue
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ------------------------------------------------------------------- readings

def _gated_on(graph: MirGraph, trait: str, routes: tuple[PublicRoute, ...]) -> FnRoutes:
    """`routes` plus the crate's `<dyn Trait>` calls that run only on a
    formatter-flag true edge. An ungated call of the same trait is not one of
    them, and it does not remove the ones that are."""
    if not routes or not trait:
        return FnRoutes(routes)
    sites, _total = graph.gated_dispatches(trait)
    return FnRoutes(routes, tuple(sites))


def routes_for(graph: MirGraph, rec: FnRecord) -> FnRoutes:
    """How the dump reaches `rec` from the public API. Empty when `rec` is
    public itself, is not in the dump, or no path was found."""
    i = graph.index_of(record_site(rec))
    if i is None or graph.is_public(i):
        return FnRoutes()
    c = rec.container.strip()
    trait = _trait_last(c) if c.startswith("impl") else ""
    self_ty = impl_self_type(c) if c.startswith("impl") else None
    if trait and self_ty and self_ty in graph.crate_types and not rec.impl_self_generic:
        coerced = graph.unsize_sites(self_ty)
        if coerced:
            # A coerced value is dispatched through the `dyn` it becomes, and
            # the crate's own `<dyn Trait>` call of that trait is the
            # dispatcher: its flag edge is quoted here as on a build route.
            # Which test entered the function is a separate fact.
            return _gated_on(graph, trait, graph.routes_from(
                ((s, f"coerces `{self_ty}` to a `dyn`") for s in coerced), skip=i))
        built = graph.build_sites(self_ty)
        if built:
            return _gated_on(graph, trait, graph.routes_from(
                ((s, f"builds `{self_ty}`") for s in built), skip=i))
    return FnRoutes(graph.routes_from(((i, ""),), skip=i))


def type_facts(graph: MirGraph, types: Iterable[TypeRecord]) -> tuple[TypeFact, ...]:
    """The types the source declares `pub` that the public index does not
    list, each with the public calls that build one."""
    out: list[TypeFact] = []
    seen: set[str] = set()
    for t in types:
        if not (t.is_pub and t.is_exactly_pub) or t.name in seen:
            continue
        if t.name in graph.surface.nameable or graph.surface.names_item(t.file, t.name):
            continue
        seen.add(t.name)
        built = graph.build_sites(t.name)
        out.append(TypeFact(t.name, t.file, graph.routes_from(
            (s, f"builds `{t.name}`") for s in built)))
    return tuple(sorted(out, key=lambda f: (f.file, f.name)))


_ITEM_KEYWORDS = ("struct", "enum", "union", "trait", "fn", "type", "static", "const", "macro")


def _api_item_name(line: str) -> str:
    """The item a rendered API line declares (`pub struct DisplayError< M >`
    -> DisplayError, `pub const fn f(` -> f), or ""."""
    toks = idents(line)
    if not toks or toks[0] != "pub":
        return ""
    for i, tok in enumerate(toks[1:], 1):
        if tok in _ITEM_KEYWORDS and i + 1 < len(toks):
            nxt = toks[i + 1]
            if tok == "const" and nxt == "fn":
                continue
            return nxt
    return ""


def nameable_api(api_text: str, file: str, facts: WorldFacts | None) -> str:
    """`api_text` (one rendered `pub` item per line, for `file`) with every
    item the public index does not list for that file moved out of the list
    into one `internal` line, each with what builds it when the facts know.

    A `pub` item of a private module reads as importable in the source view
    and is not (`use anyhow::DisplayError` -> E0432). Without facts for the
    world, the text is returned as it was: the index cannot say what it did
    not measure. A file the measured index lists nothing for has nothing a
    test can name (anyhow's `src/wrapper.rs`)."""
    if facts is None:
        return api_text
    built = {t.name: t for t in facts.unnameable}
    # A failed public-index read (no item names) still has the measured
    # unnameable types: those are hidden on their own, and the line says the
    # rest of the list was not checked — it used to come back unchanged and
    # list `pub struct DisplayError` as importable.
    indexed = bool(facts.item_names)
    listed = PublicIndex.of(facts.item_names).names_in(file) if indexed else frozenset()
    unnameable_here = {t.name for t in facts.unnameable if t.file == file}
    kept: list[str] = []
    hidden: list[str] = []
    for line in api_text.splitlines():
        name = _api_item_name(line)
        if name and (name not in listed if indexed else name in unnameable_here):
            if name not in hidden:
                hidden.append(name)
            continue
        kept.append(line)
    unchecked = ("" if indexed else
                 "// The public index was not read for this world: the items "
                 "above are not checked as importable from tests/.")
    if not hidden:
        return api_text + ("\n" + unchecked if unchecked else "")
    notes = []
    for name in hidden:
        fact = built.get(name)
        route = ("; built by " + "; ".join(r.render() for r in fact.built_by)) if fact and fact.built_by else ""
        notes.append(f"`{name}`{route}")
    kept.append("// Internal, NOT nameable from tests/ (pub in a private module; do not `use` it): "
                + " | ".join(notes))
    if unchecked:
        kept.append(unchecked)
    return "\n".join(kept)


def world_facts(cluster: str, *, mir_text: str, surface: PublicSurface,
                records: Iterable[FnRecord], types: Iterable[TypeRecord],
                file_texts: Mapping[str, str]) -> WorldFacts:
    """Every non-public function's routes, and the unnameable types, of one world."""
    recs = [r for r in records if r.fn_name and r.file in file_texts]
    graph = MirGraph(mir_text, recs, file_texts, surface)
    routes: dict[str, FnRoutes] = {}
    for rec in recs:
        fr = routes_for(graph, rec)
        if fr.routes:
            routes[site_label(record_site(rec))] = fr
    return WorldFacts(cluster, routes, type_facts(graph, types), dict(surface.item_names))
