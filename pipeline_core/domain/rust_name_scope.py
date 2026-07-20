"""Crate-scoped name resolution — which crate item a bare identifier denotes.

A reference graph built by matching bare identifiers against a set of private
type names is wrong whenever a name is not unique in the crate's namespaces.
The failure is not theoretical and not rare: a crate that does

    // lib.rs
    use std::error::Error as StdError;   // `StdError` == std's trait
    // some_module.rs (inline `mod ext`)
    pub trait StdError { ... }           // a DIFFERENT, private trait

has every `dyn StdError` in every signature bind to the private trait under
bare-name matching. The private-layout graph then welds each of those items
into one component, and items that only ever mentioned std's trait — trait
impls on PUBLIC types — become "private-replaceable" and get rewritten.

This module resolves a bare ident the way Rust does, at file granularity:
an explicit `use` binding in the file wins; failing that a same-module
declaration; failing that a glob import; otherwise the name denotes nothing
private. Only `crate::` / `self::` / `super::` paths can reach crate-internal
items (edition 2018+: any other leading segment names an EXTERNAL crate), so
`use crate::StdError` — the crate ROOT's re-export of std's trait — correctly
resolves to nothing private.

Pure domain: consumes already-enumerated ModRecord/UseRecord, no IO, no regex.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from pipeline_core.domain.models import ModRecord, UseRecord
from pipeline_core.domain.module_reachability import ROOT_BASENAMES, resolve_child_path

# A path rooted at one of these reaches into the crate itself. Anything else
# leads with an external crate name (edition 2018+), so it can never denote a
# private item of THIS crate.
_CRATE_ROOTS = frozenset({"crate", "self", "super"})

ModulePath = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModuleScopes:
    """Where each file sits in the module tree, and what its top-level `use`
    declarations bind. Empty = "no scope facts available": every resolver built
    from it falls back to bare-name matching, which is what callers without
    module data (single-file fixtures) already assume."""

    module_of_file: Mapping[str, ModulePath] = field(default_factory=dict)
    uses_by_file: Mapping[str, tuple[UseRecord, ...]] = field(default_factory=dict)

    @property
    def is_unscoped(self) -> bool:
        return not self.module_of_file

    @staticmethod
    def from_records(
        mods: list[ModRecord], uses: list[UseRecord],
    ) -> ModuleScopes:
        """Build the module tree by walking `mod` declarations from the crate
        root, then index each file's `use` bindings. Files unreachable from the
        root keep no module path — a reference in them resolves by same-file
        declaration only, never by a path that would place it wrongly."""
        by_declared_in: dict[str, list[ModRecord]] = {}
        files = {m.declared_in for m in mods} | {u.file for u in uses}
        for m in mods:
            by_declared_in.setdefault(m.declared_in, []).append(m)
        roots = sorted(f for f in files if _basename(f) in ROOT_BASENAMES)
        module_of_file: dict[str, ModulePath] = {}
        stack: list[tuple[str, ModulePath]] = []
        for r in roots:
            module_of_file[r] = ()
            stack.append((r, ()))
        while stack:
            cur, path = stack.pop()
            for m in by_declared_in.get(cur, ()):
                child = resolve_child_path(cur, m.file)
                if child in module_of_file:
                    continue
                child_path = (*path, m.name)
                module_of_file[child] = child_path
                stack.append((child, child_path))
        uses_by_file: dict[str, list[UseRecord]] = {}
        for u in uses:
            uses_by_file.setdefault(u.file, []).append(u)
        return ModuleScopes(
            module_of_file=module_of_file,
            uses_by_file={f: tuple(v) for f, v in uses_by_file.items()},
        )

    def absolute(self, file: str, segments: tuple[str, ...]) -> ModulePath | None:
        """`segments` (a use path's module part, as written) as a module path
        from the crate root, or None when it leaves this crate."""
        if not segments:
            return None
        head, rest = segments[0], segments[1:]
        if head not in _CRATE_ROOTS:
            return None
        if head == "crate":
            return rest
        here = self.module_of_file.get(file)
        if here is None:
            return None
        if head == "self":
            return (*here, *rest)
        depth = 0
        while depth < len(segments) and segments[depth] == "super":
            depth += 1
        if depth > len(here):
            return None
        return (*here[:len(here) - depth], *segments[depth:])

    def resolver(
        self, declarations: Iterable[tuple[str, str]],
    ) -> NameResolver:
        """A resolver over the given private declarations — (name, declaring
        file) pairs, since one name may be declared in several modules. Files
        with no place in the module tree stay addressable from their OWN file
        but by no path, so an unplaceable file can never absorb a `crate::…`
        reference meant for a real module."""
        by_file: dict[str, set[str]] = {}
        by_module: dict[ModulePath, set[str]] = {}
        names: set[str] = set()
        for name, file in declarations:
            by_file.setdefault(file, set()).add(name)
            names.add(name)
            mod = self.module_of_file.get(file)
            if mod is not None:
                by_module.setdefault(mod, set()).add(name)
        return NameResolver(
            self, frozenset(names),
            {k: frozenset(v) for k, v in by_file.items()},
            {k: frozenset(v) for k, v in by_module.items()})


@dataclass(frozen=True, slots=True)
class NameResolver:
    """Resolves bare identifiers written in ONE file to the DECLARED private
    names they denote — which is not always the written ident, since
    `use crate::ptr::Own as P` makes `P` denote `Own`. Unscoped (no module
    facts) ⇒ every known name matches, preserving the bare-name behaviour for
    callers with no module tree to resolve in."""

    scopes: ModuleScopes
    declarations: frozenset[str]
    by_file: Mapping[str, frozenset[str]]
    by_module: Mapping[ModulePath, frozenset[str]]

    def resolve(self, file: str, names: Collection[str]) -> frozenset[str]:
        if self.scopes.is_unscoped:
            return frozenset(n for n in names if n in self.declarations)
        out: set[str] = set()
        for n in names:
            out.update(self._denotes(file, n))
        return frozenset(out)

    def _denotes(self, file: str, name: str) -> frozenset[str]:
        """The private declarations `name`, written bare in `file`, denotes.
        An explicit `use` wins over a same-file declaration: a top-level item
        and a `use` of the same name in one module is a compile error, so a
        file carrying both must declare it inside an inline `mod` — and there
        the OUTER scope is governed by the `use`."""
        uses = self.scopes.uses_by_file.get(file, ())
        explicit = [u for u in uses if not u.is_glob and u.alias == name and u.path]
        if explicit:
            return frozenset(
                u.path[-1] for u in explicit
                if self._declared_at(u.path[-1],
                                     self.scopes.absolute(file, u.path[:-1])))
        if name in self.by_file.get(file, frozenset()):
            return frozenset({name})
        if any(self._declared_at(name, self.scopes.absolute(file, u.path))
               for u in uses if u.is_glob):
            return frozenset({name})
        return frozenset()

    def _declared_at(self, name: str, module: ModulePath | None) -> bool:
        return module is not None and name in self.by_module.get(module, frozenset())


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]
