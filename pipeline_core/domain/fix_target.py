"""What ONE fix unit repairs — the explicit target type behind the
`_fix_single_fn` / `_fix_impl_block` split (P3.C).

The old loop branched on an untyped pair of nearly-identical closures: one fixed
a single free fn, the other a whole impl block. The two differed in three
implicit ways — which fn names the unit owns, how its "is this unit clean now?"
verdict reads the compile frontier, and how it labels itself in the log. This
module makes those three explicit and type-driven:

* :class:`FnTarget`   — one free fn; clean ⇔ that (file, fn) left the frontier.
* :class:`ImplTarget` — a whole impl block; clean ⇔ NO member of the block is in
  the frontier (an impl is repaired as a unit, so any remaining member error
  keeps the whole block dirty).

Both satisfy :class:`FixTarget`, so the use-case drives them uniformly and the
verdict logic lives ON the data, not in a branch. Pure — no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pipeline_core.domain.models import Frontier, ImplBlock


@runtime_checkable
class FixTarget(Protocol):
    """A unit of repair: the file it lives in, the container header to splice
    into (`""` for a free fn), the fn names it owns, its log label, and the
    frontier predicate that decides whether its errors are gone."""

    @property
    def file_rel(self) -> str: ...
    @property
    def container(self) -> str: ...
    def member_names(self) -> frozenset[str]: ...
    def label(self) -> str: ...
    def is_clean(self, rc: int, frontier: Frontier) -> bool: ...


@dataclass(frozen=True, slots=True)
class FnTarget:
    """A single free (or unambiguous) function."""

    file_rel: str
    fn_name: str
    container: str = ""

    def member_names(self) -> frozenset[str]:
        return frozenset({self.fn_name})

    def label(self) -> str:
        return f"fn:{self.file_rel}::{self.fn_name}"

    def is_clean(self, rc: int, frontier: Frontier) -> bool:
        """Clean once the build passes OR this exact (file, fn) no longer carries
        a compile error."""
        return rc == 0 or (self.file_rel, self.fn_name) not in frontier


@dataclass(frozen=True, slots=True)
class ImplTarget:
    """A whole impl/trait block, repaired in one prompt so the model sees
    impl-level constraints and sibling impls."""

    file_rel: str
    container: str
    block: ImplBlock

    def member_names(self) -> frozenset[str]:
        return frozenset(m.fn_name for m in self.block.members if m.fn_name)

    def siblings(self) -> tuple[str, ...]:
        return tuple(self.block.related_containers)

    def label(self) -> str:
        return f"impl:{self.file_rel}::{self.container[:40]}"

    def is_clean(self, rc: int, frontier: Frontier) -> bool:
        """Clean once the build passes OR no member of this block remains in the
        frontier — a block is repaired as a unit, so any leftover member error
        keeps it dirty."""
        if rc == 0:
            return True
        names = self.member_names()
        return not any(f == self.file_rel and fn in names for (f, fn) in frontier)
