"""Associated-item identity across the crate's files.

One rule lives here: a repair write may not INTRODUCE a definition of an item
that already exists on the same type, under the same trait, in another file of
the same crate. Rust calls that `E0592` — "duplicate definitions with name" —
and it is unreachable for every repair route the pipeline owns, because a
unit-scope fix may only rewrite a BODY. The only action that resolves it is
removing one of the two definitions.

The instance (2026-08-11, merged tree of run `3337398-fbf7e143`). Between
10:33:29Z and 10:43:11Z one whole-file rewrite took `src/error.rs` from
595L/27fn/3 diagnostics to 659L/29fn/23 diagnostics, and among the two fns it
added was `ErrorImpl::debug` — which the authored layouts place in
`src/fmt.rs`, where it also still was. Upstream anyhow keeps `debug`/`display`
in `error.rs`; the model rewrote the file toward the layout it had memorised.
The same pass tried to write `debug` and `display` into `src/lib.rs` too, and
those two were refused only because the SPLICER cannot create an item it
cannot locate (`fn_not_found`). Nothing refused the whole-file write.

Identity carries the trait name because an inherent `fn source` and a
`StdError::source` on the same type are two legal, distinct items; only a
collision within the same (type, trait) pair is E0592.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.rust_idents import impl_self_type, impl_trait_name


@dataclass(frozen=True, slots=True, order=True)
class AssociatedItem:
    """One named item on one type, under one trait (`trait_name` empty for an
    inherent impl). The identity rustc collides on."""

    type_name: str
    trait_name: str
    item_name: str

    def render(self) -> str:
        via = f" as {self.trait_name}" if self.trait_name else ""
        return f"{self.type_name}{via}::{self.item_name}"


@dataclass(frozen=True, slots=True)
class DuplicateIntroduction:
    """A write placed `item` in `introduced_in` while `already_in` held it."""

    item: AssociatedItem
    introduced_in: str
    already_in: tuple[str, ...]

    def render(self) -> str:
        return (f"{self.item.render()} introduced in {self.introduced_in}, "
                f"already defined in {', '.join(self.already_in)}")


def items_by_file(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, frozenset[AssociatedItem]]:
    """Fold extractor fn records into per-file associated-item sets.

    Records with no container are FREE fns: two same-named free fns in
    different modules are legal Rust (they are different paths), so they carry
    no cross-file collision and are dropped here.
    """
    acc: dict[str, set[AssociatedItem]] = {}
    for rec in records:
        container = str(rec.get("container", "") or "")
        if not container.strip():
            continue
        self_type = impl_self_type(container)
        if not self_type:
            continue
        rel = str(rec.get("file", "") or "")
        name = str(rec.get("fn_name", "") or "")
        if not rel or not name:
            continue
        acc.setdefault(rel, set()).add(AssociatedItem(
            type_name=self_type,
            trait_name=impl_trait_name(container) or "",
            item_name=name))
    return {rel: frozenset(items) for rel, items in acc.items()}


def introduced_duplicates(
    before: Mapping[str, frozenset[AssociatedItem]],
    after: Mapping[str, frozenset[AssociatedItem]],
) -> tuple[DuplicateIntroduction, ...]:
    """Items the write ADDED to a file that another file already defined.

    The other placement must pre-date the write. That asymmetry is deliberate:
    it names the new copy as the offender and leaves the established home
    alone, which is both the correct repair and the only one that can be made
    without consulting the authored layout. A write that creates BOTH copies in
    one go is not reported here — no evidence in it says which is meant — and
    falls to the diagnostic-count gate instead.
    """
    out: list[DuplicateIntroduction] = []
    for rel, items in sorted(after.items()):
        gained = items - before.get(rel, frozenset())
        for item in sorted(gained):
            already = tuple(sorted(
                other for other, other_items in after.items()
                if other != rel
                and item in other_items
                and item in before.get(other, frozenset())))
            if already:
                out.append(DuplicateIntroduction(item, rel, already))
    return tuple(out)


def removal_target(
    duplicate: DuplicateIntroduction,
    authored: Mapping[str, frozenset[AssociatedItem]],
) -> str:
    """Which file's copy should be deleted, per the authored layouts.

    The loser is the placement no authored `.skeleton/<cluster>/src` declares —
    that names the fix-loop-introduced copy exactly and can never delete frozen
    scaffold. Returns "" when the layouts do not disambiguate (both placements
    authored, or neither): a guess here removes real work, so the caller must
    fall back to refusing the write rather than choosing.
    """
    candidates = [duplicate.introduced_in, *duplicate.already_in]
    unauthored = [rel for rel in candidates
                  if duplicate.item not in authored.get(rel, frozenset())]
    if len(unauthored) != 1:
        return ""
    return unauthored[0]
