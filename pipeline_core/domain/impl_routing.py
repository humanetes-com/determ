"""Which impl block does a model-returned FN body belong to? (debt #43)

The per-impl fill prompt shows a TARGET block plus RELATED blocks (same trait /
dispatch family); the model tags each returned body with a ``block`` attribute.
The old routing matched that attribute against the container strings LITERALLY
(exact, then a ``split("{")`` header-prefix fallback), so a multi-line container
(``impl<T> Own<T>\nwhere\n    T: ?Sized,``) never matched the model's
single-line rendering (``impl<T> Own<T>``) — every body was dropped and the
whole impl-scope LLM call wasted (anyhow run 187469: 11 drops in src/ptr.rs).

This module names the rule: bodies route by CANONICAL impl-header identity
(:func:`~pipeline_core.domain.rust_idents.canon_container` — generics-list,
where-clause and whitespace insensitive), with exact-string equality preferred
and the TARGET preferred over RELATED at each tier. Pure domain — testable
without the model, cargo, or the tree.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.rust_idents import canon_container


@dataclass(frozen=True, slots=True)
class RoutedBody:
    """One returned FN body resolved to the VERBATIM container it splices into."""

    fn_data: dict[str, Any]
    container: str


@dataclass(frozen=True, slots=True)
class BodyRouting:
    """The routing decision for one model reply: spliceable bodies in reply
    order, plus the fn names whose ``block`` attribute matched no container."""

    routed: tuple[RoutedBody, ...] = ()
    dropped: tuple[str, ...] = ()


def route_fn_bodies(
    parsed: list[dict[str, Any]], *, target: str, related: list[str],
) -> BodyRouting:
    """Resolve each parsed FN block to the TARGET or a RELATED container.

    Tiers per body (first hit wins): exact match on TARGET, exact on RELATED
    (listed order), canonical match on TARGET, canonical on RELATED (listed
    order). A body matching nothing is dropped by name."""
    routed: list[RoutedBody] = []
    dropped: list[str] = []
    target_canon = canon_container(target)
    related_canon = [(c, canon_container(c)) for c in related]
    for fn_data in parsed:
        block_attr = fn_data.get("block") or ""
        if block_attr == target:
            routed.append(RoutedBody(fn_data, target))
            continue
        exact = next((c for c in related if block_attr == c), None)
        if exact is not None:
            routed.append(RoutedBody(fn_data, exact))
            continue
        attr_canon = canon_container(block_attr)
        if attr_canon == target_canon:
            routed.append(RoutedBody(fn_data, target))
            continue
        canonical = next((c for c, cc in related_canon if attr_canon == cc), None)
        if canonical is not None:
            routed.append(RoutedBody(fn_data, canonical))
            continue
        dropped.append(str(fn_data.get("fn_name", "")))
    return BodyRouting(routed=tuple(routed), dropped=tuple(dropped))
