"""Which remaining stubs nothing in the rebuilt crate or its oracle can call
(pure domain): one fixed point over the tree's removable units, seeded by
rustc's dead-code lint and certified by a removal build.

The end gate excuses a crate-private stub nothing reaches: no test can enter
it, so no fill of it could be judged. Its first reader was rustc's
`dead_code` lint (`domain.dead_code`), which never reports a method of a
trait impl: a trait impl is live as soon as its trait and its type are. Run
`3323407-984b07f2` (2026-11-08) passed every oracle test and exited 1 on
`src/context.rs::write_str [impl Write for Quoted<&mut fmt::Formatter<'_>>]`,
an impl the rebuilt crate never builds a value for.

**Units.** A free fn or an inherent method is its fn item; a trait-impl
method belongs to its whole impl block (one method of a live impl cannot be
deleted alone). A trait's provided method has no unit and is always live.

**The seed.** Every unit the lint reports dead (library and test profile) is
dead: the lint is rustc's own transitive liveness walk over the real tree.

**The fixed point.** Every other unit no outside crate can reach starts as
dead, and is made live by any MIR use of one of its fns (a call naming it,
its fn item taken as a value, or for a trait-impl method any method of its
trait on its self type) that sits in a block whose owner unit is live. A
closure, or any item the compiler synthesizes inside a fn (`::{closure#0}`),
is its creator's (run `3323407-984b07f2`'s `debug::{closure#0}` shape). A
block whose owner cannot be told (a const, a static, a name several free fns
share) is live. Repeat until stable: the greatest set consistent with the
uses, so a cycle of units only each other call is dead.

**What certifies a proof: the compiler's output for everything else is
unchanged.** The fixed point only chooses candidates and bundles; a miss
there costs a proof, never a false one. Each stub's proof is a removal on a
scratch copy: delete the stub's unit together with every non-seed unit its
uses depend on (its bundle) and every lint-dead fn; the crate and every test
target must still build, AND every fn that survives the deletion must have
the same MIR (library and test profile; deletion blanks, so positions do
not move) as in the copy
with only the lint-dead fns deleted (`mir_unchanged`). Type-checking alone
answers too little: a deleted item can let a call fall back to another
candidate of the same name (autoderef, an impl on `&T`), and a call the
compiler inserts (a drop at a scope exit) needs no type requirement at all,
so in both cases the copy builds while the program changed. Measured
2026-11-08: deleting a held type's `Drop` impl builds and removes
`drop(_2) -> [return: bb1, …]` from its holder's MIR; deleting an impl a
call reached through `Deref` builds and turns `<Outer as Go>::go(…)` into
`Inner::go(…)`; deleting an unused `Write` impl changes no other fn. A `dyn`
coercion, an unsatisfied bound and a macro expansion fail the build. No
trait, construct or dispatch rule is kept here: whatever the compiler did
because of the deleted items shows up as MIR that changed.

**What the copy cannot see.** An outside crate: a fn the public index
lists, an inherent `pub` method of a type an outside crate can hold, and a
trait impl on such a type or on a generic or foreign self type are live. A
test crate cannot call a method on a value of a private type at all
(`error: type … is private`, measured 2026-11-08), so a call it makes always
reaches a type this check keeps, or goes through a bound the library build
already checks.

The failure direction is fixed: no reading in a world, a live use, an
outside caller, a failed build or a changed MIR leaves the stub orphaned.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.dead_code import WorldDeadCode, names_record
from pipeline_core.domain.fn_reachability import (
    MirBlock,
    PublicSurface,
    as_trait,
    call_or_item,
    defined_type_names,
    has_word,
    head_idents,
    header_name,
    impl_begin,
    parse_mir,
    record_first_line,
)
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.rust_idents import container_identity, impl_self_type

# (file, byte start, byte end) of one deletable item.
ItemKey = tuple[str, int, int]
# (file, signature byte offset, fn name): one record, whatever its container.
RecKey = tuple[str, int, str]


def rec_key(rec: FnRecord) -> RecKey:
    return (rec.file, rec.sig_byte_start, rec.fn_name)


def trait_of(rec: FnRecord) -> tuple[str, ...]:
    """The trait path of the impl `rec` is a method of, () for a free fn, an
    inherent method or a trait's provided method."""
    c = rec.container.strip()
    if not c.startswith("impl"):
        return ()
    return container_identity(c).trait


def is_trait_impl_method(rec: FnRecord) -> bool:
    return bool(trait_of(rec))


@dataclass(frozen=True, slots=True)
class Unit:
    """One deletable item and the fn records inside it."""

    key: ItemKey
    kind: str                       # "fn" | "impl"
    records: tuple[FnRecord, ...]


def units_of(tree: Sequence[FnRecord]) -> dict[RecKey, Unit]:
    """Each record's unit (records with no range, and a trait's provided
    methods, have none)."""
    impls: dict[ItemKey, list[FnRecord]] = {}
    out: dict[RecKey, Unit] = {}
    for r in tree:
        if r.container.strip().startswith("trait"):
            continue
        if is_trait_impl_method(r):
            if r.impl_byte_end > r.impl_byte_start:
                impls.setdefault((r.file, r.impl_byte_start, r.impl_byte_end), []).append(r)
        elif r.item_byte_end > r.item_byte_start:
            out[rec_key(r)] = Unit((r.file, r.item_byte_start, r.item_byte_end), "fn", (r,))
    for key, members in impls.items():
        unit = Unit(key, "impl", tuple(members))
        for r in members:
            out[rec_key(r)] = unit
    return out


def delete_ranges(text: bytes, ranges: Iterable[tuple[int, int]]) -> bytes:
    """`text` with each byte range deleted by blanking: every character in it
    becomes a space and every newline stays, so every line and column after
    it keeps its number. The compiler records source positions as data, not
    only in spans (a test's descriptor holds `start_line: const 55_usize`,
    measured 2026-11-08), so a cut would change surviving MIR that no
    deletion touched. Overlapping or nested ranges are merged first, so an
    impl and a method inside it delete once."""
    merged: list[list[int]] = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = bytearray()
    at = 0
    for a, b in merged:
        out += text[at:a]
        gone = text[a:b].decode("utf-8", errors="replace")
        out += "".join(ch if ch == "\n" else " " for ch in gone).encode("utf-8")
        at = b
    out += text[at:]
    return bytes(out)


def removable_subset(items: Sequence[ItemKey],
                     builds: Callable[[frozenset[ItemKey]], bool]) -> frozenset[ItemKey]:
    """The items whose deletion still builds, by group testing: all at once,
    and halves only when a group fails. Each returned item was deleted in a
    group that built."""
    if not items:
        return frozenset()
    group = frozenset(items)
    if builds(group):
        return group
    if len(items) == 1:
        return frozenset()
    mid = len(items) // 2
    return (removable_subset(items[:mid], builds)
            | removable_subset(items[mid:], builds))


_ALLOC = re.compile(r"\balloc\d+\b")


def _mir_items(text: str) -> dict[tuple[str, tuple[str, ...]], int]:
    """The dump's items with allocation ids normalized (a deleted fn's
    allocations renumber the rest), as a multiset. Positions need no
    normalizing: `delete_ranges` blanks, so every surviving line and column
    keeps its number."""
    out: dict[tuple[str, tuple[str, ...]], int] = {}
    for b in parse_mir(text):
        key = (_ALLOC.sub("alloc", b.header),
               tuple(_ALLOC.sub("alloc", ln) for ln in b.lines))
        out[key] = out.get(key, 0) + 1
    return out


def mir_unchanged(baseline: str, cut: str) -> bool:
    """Every item of the `cut` dump is an item of `baseline`, byte for byte
    up to allocation ids: a deletion may remove items, never change one that
    survives. An empty `cut` dump is no reading."""
    if not cut.strip():
        return False
    have = _mir_items(baseline)
    return all(have.get(key, 0) >= n for key, n in _mir_items(cut).items())


def outside_caller(rec: FnRecord, text: str, surface: PublicSurface,
                   crate_types: frozenset[str]) -> str:
    """Why another crate (or the compiler) may call `rec` with no call in
    this crate's MIR, or "" when nothing can."""
    if surface.lists_fn(rec.file, record_first_line(rec, text)):
        return "it is in the public index (an outside crate can name it)"
    container = rec.container.strip()
    if not container:
        return ""
    self_ty = impl_self_type(container)
    held = bool(self_ty) and (self_ty in surface.nameable or self_ty in surface.exposed)
    trait = trait_of(rec)
    if trait:
        if rec.impl_self_generic or not self_ty or self_ty not in crate_types:
            return "its impl's self type is generic or not defined by the crate"
        if held:
            return f"`{self_ty}` is nameable or spelled by a public signature"
        return ""
    if rec.is_exactly_pub and rec.is_pub and held:
        return f"`pub` method on `{self_ty}`, which an outside crate can hold"
    return ""


def _path_use(ln: str, name: str) -> bool:
    pos = ln.find("::" + name)
    while pos >= 0:
        end = pos + 2 + len(name)
        if end >= len(ln) or not (ln[end] == "_" or ln[end].isalnum()):
            return True
        pos = ln.find("::" + name, pos + 1)
    return False


def uses(rec: FnRecord, blocks: Sequence[MirBlock]) -> list[tuple[MirBlock, str]]:
    """Every MIR line that may use `rec`: for a free fn, its name as a word
    (debug-info lines, which name locals, excepted); for a method, a path
    naming it on its self type; for a trait-impl method, also a call or fn
    item of any method of its trait on that type (a provided method runs the
    required ones). A superset is safe: a use only ever keeps."""
    name = rec.fn_name
    container = rec.container.strip()
    self_ty = impl_self_type(container) if container else None
    trait = trait_of(rec)
    out: list[tuple[MirBlock, str]] = []
    for b in blocks:
        for ln in b.lines:
            if self_ty is None:
                if name in ln and has_word(ln, name) and not ln.lstrip().startswith("debug "):
                    out.append((b, ln))
                continue
            if self_ty not in ln or not has_word(ln, self_ty):
                continue
            if trait:
                callee = call_or_item(ln)
                if callee is not None and self_ty in head_idents(callee) \
                        and as_trait(callee) == trait[-1]:
                    out.append((b, ln))
                    continue
            if _path_use(ln, name):
                out.append((b, ln))
    return out


def creator_header(header: str) -> str:
    """The header of the fn a synthesized item belongs to: everything after
    the first `::{` of its path dropped (`…::debug::{closure#0}(…` ->
    `…::debug(`). Unchanged for an ordinary fn."""
    if not header.startswith(("fn ", "pub fn ", "unsafe fn ")):
        return header
    depth = 0
    for i, ch in enumerate(header):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "(" and depth == 0:
            path = header[:i]
            cut = path.find("::{")
            return header if cut < 0 else path[:cut] + "("
    return header


def block_owner(block: MirBlock, records: Sequence[FnRecord],
                file_texts: Mapping[str, str]) -> FnRecord | None:
    """The record whose body `block` is, or whose body created it (a
    closure). None when that cannot be told (a const, a static, a name
    several free fns share)."""
    h = creator_header(block.header)
    if not h.startswith(("fn ", "pub fn ", "unsafe fn ")):
        return None
    name = header_name(h)
    if "impl at " not in h:
        hits = [r for r in records if not r.container.strip() and r.fn_name == name]
        return hits[0] if len(hits) == 1 else None
    file = h.split("impl at ", 1)[1].split(":", 1)[0]
    begin = impl_begin(h, file)
    if begin is None:
        return None
    best: tuple[int, FnRecord] | None = None
    for r in records:
        if r.file != file or r.fn_name != name or r.impl_byte_end <= r.impl_byte_start:
            continue
        text = file_texts.get(r.file, "")
        start_line = text.count("\n", 0, r.impl_byte_start) + 1
        if start_line <= begin <= record_first_line(r, text) and (
                best is None or start_line > best[0]):
            best = (start_line, r)
    return best[1] if best else None


@dataclass(frozen=True, slots=True)
class CompilerView:
    """The unmodified tree's compiler view in one world: the lint's dead
    spans, the MIR (library and test profile) and the public index."""

    dead: WorldDeadCode
    mir_texts: tuple[str, ...] = ()
    surface: PublicSurface | None = None


@dataclass(frozen=True, slots=True)
class DeadUnits:
    """The fixed point of one world: the dead units, which of them the lint
    seeded, the non-seed units each dead unit's uses sit in, and why each
    live unit is live."""

    dead: frozenset[ItemKey]
    seeds: frozenset[ItemKey]
    depends: Mapping[ItemKey, frozenset[ItemKey]]
    live_why: Mapping[ItemKey, str]

    def bundle(self, key: ItemKey) -> frozenset[ItemKey]:
        """`key` and every non-seed unit its proof depends on."""
        out: set[ItemKey] = set()
        todo = [key]
        while todo:
            k = todo.pop()
            if k in out:
                continue
            out.add(k)
            todo.extend(self.depends.get(k, ()))
        return frozenset(out)


def dead_units(tree: Sequence[FnRecord], view: CompilerView,
               file_texts: Mapping[str, str]) -> DeadUnits:
    """The greatest set of units no live code can call (module docstring)."""
    unit_of = units_of(tree)
    units = {u.key: u for u in unit_of.values()}
    seeds = frozenset(u.key for u in units.values() if u.kind == "fn"
                      and names_record(u.records[0], view.dead.spans))
    live_why: dict[ItemKey, str] = {}
    if view.surface is None or not any(view.mir_texts):
        for k in units:
            if k not in seeds:
                live_why[k] = "no MIR or public index was read in this world"
        return DeadUnits(seeds, seeds, {}, live_why)
    blocks = [b for t in view.mir_texts if t for b in parse_mir(t)]
    crate_types = defined_type_names(file_texts.values())
    owner_cache: dict[int, ItemKey | None] = {}

    def owner_key(b: MirBlock) -> ItemKey | None:
        if id(b) not in owner_cache:
            o = block_owner(b, tree, file_texts)
            u = unit_of.get(rec_key(o)) if o is not None else None
            owner_cache[id(b)] = u.key if u is not None else None
        return owner_cache[id(b)]

    candidates: set[ItemKey] = set()
    hits: dict[ItemKey, list[tuple[ItemKey | None, str]]] = {}
    for k, u in units.items():
        if k in seeds:
            continue
        why = next((w for w in (outside_caller(r, file_texts.get(r.file, ""),
                                               view.surface, crate_types)
                                for r in u.records) if w), "")
        if why:
            live_why[k] = why
            continue
        candidates.add(k)
        hits[k] = [(owner_key(b), f"used in `{b.header[:60]}`: {ln.strip()[:90]}")
                   for r in u.records for b, ln in uses(r, blocks)]
    dead = set(candidates) | set(seeds)
    changed = True
    while changed:
        changed = False
        for k in sorted(candidates & dead):
            for owner, line in hits[k]:
                if owner is None or owner not in dead:
                    dead.discard(k)
                    live_why[k] = line
                    changed = True
                    break
    depends = {k: frozenset(o for o, _l in hits[k]
                            if o is not None and o != k and o not in seeds)
               for k in candidates & dead}
    return DeadUnits(frozenset(dead), seeds, depends, live_why)


@dataclass(frozen=True, slots=True)
class WorldUnreached:
    """One cluster world's reading: the lint's dead spans, the stub units
    whose bundle built without them, and why each kept stub is kept — or why
    the world has no reading."""

    cluster: str
    dead: WorldDeadCode
    removable: frozenset[ItemKey] = frozenset()
    kept: Mapping[FnSite, str] = field(default_factory=dict)
    reason: str = ""

    @property
    def measured(self) -> bool:
        return not self.reason and self.dead.measured


def removal_bundles(stubs: Iterable[FnRecord], tree: Sequence[FnRecord],
                    units: DeadUnits) -> dict[ItemKey, frozenset[ItemKey]]:
    """Stub unit -> what a build must delete to certify it, for the stub
    units the fixed point found dead and the lint did not already prove. A
    unit holding a filled method can be one too: deleting the whole impl
    asks about every method in it."""
    unit_of = units_of(tree)
    out: dict[ItemKey, frozenset[ItemKey]] = {}
    for r in stubs:
        u = unit_of.get(rec_key(r))
        if u is None or u.key in units.seeds or u.key not in units.dead:
            continue
        out[u.key] = units.bundle(u.key)
    return out


def unreached_stub_proofs(
    stub_records: Iterable[FnRecord],
    tree: Sequence[FnRecord],
    worlds: Sequence[WorldUnreached],
    compiled_in: Callable[[FnRecord, str], bool],
) -> dict[FnSite, str]:
    """Site -> proof for the stubs nothing can call in any world that compiles
    them. A site several records share (cfg-divergent copies of one fn) is
    proved only when each of its records is, and a record only when at least
    one world compiles it and every world that compiles it proves it — by the
    lint (a free fn or inherent method it reports dead) or by a removal build
    of the stub's unit."""
    unit_of = units_of(tree)
    verdict: dict[FnSite, bool] = {}
    proof: dict[FnSite, str] = {}
    for rec in stub_records:
        site = record_site(rec)
        mine = [w for w in worlds if compiled_in(rec, w.cluster)]
        u = unit_of.get(rec_key(rec))
        lint = [w.measured and not is_trait_impl_method(rec)
                and names_record(rec, w.dead.spans) for w in mine]
        built = [w.measured and u is not None and u.key in w.removable for w in mine]
        ok = bool(mine) and all(a or b for a, b in zip(lint, built, strict=True))
        verdict[site] = verdict.get(site, True) and ok
        if not ok:
            continue
        names = ", ".join(w.cluster for w in mine)
        if all(lint):
            proof[site] = f"rustc reports it never used in {names}, by the crate and its own tests"
        else:
            what = "its impl block" if u is not None and u.kind == "impl" else "the fn"
            proof[site] = (f"no live code's MIR uses it, and without {what} and the dead "
                           f"code that alone uses it the crate and its tests build and "
                           f"every other fn's MIR is unchanged in {names}")
    return {s: proof[s] for s, ok in verdict.items() if ok}
