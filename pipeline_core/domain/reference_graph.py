"""Which crate bodies a body names — the tree's own call graph, resolved from
the references the extractor reads off each body (`FnRecord.refs`).

Why it exists. Who may write together had seven answerers, each added after
one run: the file route's whole file, `joint_write.admit_joint` within the
target's file, the fn-pointer table complement, the formatter pair, the
session's coverage slice, the recorded-locator sites and the aborted tests'
stack sites. None of them could see that three bodies in two files call each
other in a ring. Run `38934-2999cc94` (2026-09-26) and its successors spent
fills on exactly that: `Boxed::new` -> `Error::from_boxed` ->
`Error::construct_from_boxed` -> `Boxed::new`, every edge a qualified path
call on the tree, and every route that opened one member wrote it alone.
The stack overflow's frames named the ring only because the process crashed
under a sanitizer; a ring that hangs, or a caller chain that never crashes,
has no stack. The source always has the edges.

How a reference resolves — the spelling decides, or there is no edge:

- ``Q::name`` (kind ``path``): ``Q`` of ``Self`` is the caller's own impl
  self type; an upper-case-led ``Q`` is an impl self type; any other ``Q``
  (a module segment, or none for ``name::<T>``) names a free fn, preferring
  the module the segment names, then the caller's own module.
- ``name(…)`` (kind ``call``): a free fn of that name, the caller's own
  module first, then the crate.
- ``.name(…)`` (kind ``method``): the one method of that name in the crate,
  or nothing. A receiver's type is not in the syntax, so a method name with
  two bodies is not an edge: ``.fmt(f)`` resolved by name would join every
  formatting impl of the crate into one component. A method calling its own
  name (``self.0.fmt(f)`` inside ``fmt``) is delegation to another receiver,
  never an edge.

Exactly one candidate is an edge; zero or several is none. Test and oracle
files are not nodes. The upper-case rule is the spelling convention the
language's own lints enforce for type names; it names no type.

Pure: records in, graph out.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.models import FnRecord, FnRef, FnSite
from pipeline_core.domain.oracle_immutability import is_oracle_path
from pipeline_core.domain.rust_idents import impl_self_type

_PATH_ROOTS = frozenset({"crate", "self", "super", ""})


def _is_free(container: str) -> bool:
    return not container or container.startswith("mod ")


def _is_method(container: str) -> bool:
    return bool(container) and not container.startswith("mod ")


def _self_type(container: str) -> str | None:
    if not _is_method(container) or container.startswith("trait "):
        return None
    return impl_self_type(container)


def _file_stem(file: str) -> str:
    name = file.replace("\\", "/").rsplit("/", 1)[-1]
    stem = name[:-3] if name.endswith(".rs") else name
    if stem in ("mod", "lib", "main"):
        parts = file.replace("\\", "/").split("/")
        return parts[-2] if len(parts) >= 2 else stem
    return stem


@dataclass(frozen=True, slots=True)
class ReferenceGraph:
    """Resolved edges between crate bodies. ``unresolved`` counts the
    references that named a crate fn but did not decide which one — the
    honest gap, printed rather than guessed."""

    edges: Mapping[FnSite, frozenset[FnSite]]
    unresolved: int = 0
    _components: dict[FnSite, frozenset[FnSite]] = field(
        default_factory=dict, compare=False, repr=False)

    def callees(self, site: FnSite) -> frozenset[FnSite]:
        return self.edges.get(site, frozenset())

    def cycle_of(self, site: FnSite) -> frozenset[FnSite]:
        """The strongly connected component ``site`` sits in when that
        component is a cycle (two or more members, or a body that names
        itself); empty otherwise."""
        if not self._components:
            self._components.update(_components(self.edges))
        comp = self._components.get(site, frozenset())
        if len(comp) > 1 or site in self.callees(site):
            return comp
        return frozenset()


def _candidates(ref: FnRef, caller: FnRecord,
                by_name: Mapping[str, Sequence[FnRecord]]) -> list[FnRecord]:
    named = list(by_name.get(ref.name, ()))
    if not named:
        return []
    if ref.kind == "method":
        if ref.name == caller.fn_name:
            # `.fmt(f)` inside a `fmt` is delegation to another receiver's
            # method of the same name, not recursion: no edge.
            return []
        return [r for r in named if _is_method(r.container)]
    q = ref.qualifier
    if ref.kind == "path" and q == "Self":
        own = _self_type(caller.container)
        return [r for r in named
                if own is not None and _self_type(r.container) == own]
    if ref.kind == "path" and q[:1].isupper():
        return [r for r in named if _self_type(r.container) == q]
    free = [r for r in named if _is_free(r.container)]
    if ref.kind == "path" and q not in _PATH_ROOTS:
        in_mod = [r for r in free
                  if _file_stem(r.file) == q
                  or (r.mod_path and r.mod_path.rsplit("::", 1)[-1] == q)]
        if in_mod:
            return in_mod
    same = [r for r in free
            if r.file == caller.file and r.mod_path == caller.mod_path]
    return same or free


def reference_graph(records: Iterable[FnRecord]) -> ReferenceGraph:
    """The crate's call graph over ``records`` (the cluster's extractor
    records; oracle and test files are skipped)."""
    nodes = [r for r in records if not r.is_test and not is_oracle_path(r.file)]
    by_name: dict[str, list[FnRecord]] = {}
    for r in nodes:
        by_name.setdefault(r.fn_name, []).append(r)
    edges: dict[FnSite, set[FnSite]] = {}
    unresolved = 0
    for r in nodes:
        src = record_site(r)
        out = edges.setdefault(src, set())
        for ref in r.refs:
            if ref.name not in by_name:
                continue
            hits = _candidates(ref, r, by_name)
            sites = {record_site(h) for h in hits}
            if len(sites) == 1:
                out.add(next(iter(sites)))
            elif sites:
                unresolved += 1
    return ReferenceGraph(
        {k: frozenset(v) for k, v in edges.items()}, unresolved)


def _components(edges: Mapping[FnSite, frozenset[FnSite]]) -> dict[FnSite, frozenset[FnSite]]:
    """Tarjan's strongly connected components, iterative."""
    index: dict[FnSite, int] = {}
    low: dict[FnSite, int] = {}
    on_stack: set[FnSite] = set()
    stack: list[FnSite] = []
    out: dict[FnSite, frozenset[FnSite]] = {}
    counter = 0
    nodes = set(edges) | {v for vs in edges.values() for v in vs}
    for root in sorted(nodes):
        if root in index:
            continue
        work: list[tuple[FnSite, list[FnSite]]] = [
            (root, sorted(edges.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, todo = work[-1]
            if todo:
                w = todo.pop()
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, sorted(edges.get(w, ()))))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp: list[FnSite] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                frozen = frozenset(comp)
                for w in comp:
                    out[w] = frozen
    return out
