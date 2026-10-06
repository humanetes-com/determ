"""The fn sites a build cluster never compiles, whatever their body holds.

A site is dead in a cluster when the cluster's cfg-set decidably excludes
EVERY record of that `(file, fn_name)`: the file's parent-mod gate and the
record's own `cfg_predicates`, evaluated by `cluster_membership`, which owns
cfg evaluation and is the one producer of this value.

Why a property of the site and not of the stub. The previous verdict was the
difference between every STUB record and the cluster-active stub records, so a
dead fn stopped being dead the moment any route filled it: on run
`473471-33301154` (2026-09-29, cluster `stable`) `src/nightly.rs` — declared
`#[cfg(error_generic_member_access)] mod nightly;` — had its two `fmt` stubs
filled, after which the per-fn route re-dispatched both as `refill: true`
(31.6 of 32.0 minutes of that run) and the splice guard accepted the bodies,
because a filled fn is in neither stub set. Deadness depends on where the fn
is, under which cfg-set; the body is irrelevant.

Membership normalises the file (`norm_src_rel`: extractor records say
`nightly.rs`, dispatch locators say `src/nightly.rs`), so no consumer compares
raw paths. Iterating yields the normalised `(file, fn_name)` pairs.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from pipeline_core.domain.attribution import FormatterPair
from pipeline_core.domain.cluster_membership_report import ClusterMembershipReport
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator, FnSite

__all__ = ["CfgDeadSites", "FrontierAdmission"]


def _pair(key: FnLocator | FnSite | tuple[str, ...]) -> tuple[str, str]:
    # FnLocator and FnSite are named tuples whose first two fields are the
    # file and the fn name, so one indexing reads all three shapes.
    return norm_src_rel(str(key[0])), str(key[1])


@dataclass(frozen=True, slots=True)
class CfgDeadSites:
    """The dead `(file, fn_name)` sites of one cluster's tree."""

    pairs: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pairs",
                           frozenset(_pair(p) for p in self.pairs))

    def __contains__(self, key: object) -> bool:
        return (isinstance(key, tuple) and len(key) >= 2
                and _pair(key) in self.pairs)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def fns_in(self, file: str) -> frozenset[str]:
        """The dead fn names of `file` (any `src/` spelling)."""
        rel = norm_src_rel(file)
        return frozenset(fn for f, fn in self.pairs if f == rel)

    def split[K: (FnLocator, FnSite), V](
        self, frontier: Mapping[K, V],
    ) -> tuple[dict[K, V], tuple[K, ...]]:
        """`frontier` without its dead keys, and the dead keys it held."""
        if not self.pairs:
            return dict(frontier), ()
        kept = {k: v for k, v in frontier.items() if k not in self}
        return kept, tuple(k for k in frontier if k not in kept)


def _loc(key: FnLocator | FnSite | tuple[str, ...]) -> FnLocator:
    return FnLocator(str(key[0]), str(key[1]))


@dataclass
class FrontierAdmission:
    """One TDD iteration's admission of units under a cluster's dead sites.

    The locator map is filtered by the frontier filter (which drops dead
    sites itself), but four inserts write units the map filter never sees:
    a formatter pair's members (`dispatch_including_pairs` writes both,
    `FailureFrontier.build` keeps a pair while ANY member has tests,
    `slices_with_pair_hops` adds the other endpoint to a slice), the
    measured-owner admits (`escalate_to_measured_owners` inserts after the
    last map filter), and the session's coverage owners and slices. Each of
    those passes through here, so a dead site cannot be written by any of
    them; and a pair with a dead member is dropped whole — a pair is one
    transaction, and a half without its partner is a different question.

    It also records every unit any insert proposed and every one it
    refused, so the iteration logs ONE report with its denominator instead
    of one line per filter call (`report`).

    `proved` are the stub units the cluster's function record proved no test
    can execute (`FnCoverageRecord.proved_stub_units`). They are refused at
    the same inserts: a fill bought for one can only be judged by a test, and
    none can reach it (run `450922-661c5e9f`: `src/backtrace.rs::
    _assert_send_sync` dispatched at iterations 6 and 8, opened by no route,
    left admitted). Reported on their own line (`proved_report`), because
    the cfg-dead line is parsed back as a cfg-set reading."""

    dead: CfgDeadSites
    proved: frozenset[FnLocator] = frozenset()
    proposed: set[FnLocator] = field(default_factory=set)
    refused: set[FnLocator] = field(default_factory=set)
    withheld_proved: set[FnLocator] = field(default_factory=set)
    _proved_keys: frozenset[tuple[str, str]] = field(
        default=frozenset(), init=False, repr=False)

    def __post_init__(self) -> None:
        self._proved_keys = frozenset(
            (norm_src_rel(u.file), u.fn_name) for u in self.proved)

    def _is_proved(self, loc: FnLocator) -> bool:
        return (norm_src_rel(loc.file), loc.fn_name) in self._proved_keys

    def _closed(self, key: FnLocator | FnSite) -> bool:
        loc = _loc(key)
        return loc in self.dead or self._is_proved(loc)

    def observe(self, keys: Iterable[FnLocator | FnSite]) -> None:
        """Record `keys` as proposed, and the closed ones as refused — for a
        map an injected filter drops dead sites from."""
        for k in keys:
            loc = _loc(k)
            self.proposed.add(loc)
            if loc in self.dead:
                self.refused.add(loc)
            elif self._is_proved(loc):
                self.withheld_proved.add(loc)

    def admit[K: (FnLocator, FnSite), V](self, units: Mapping[K, V]) -> dict[K, V]:
        self.observe(units)
        return {k: v for k, v in units.items() if not self._closed(k)}

    def without_proved[K: (FnLocator, FnSite), V](self, units: dict[K, V]) -> dict[K, V]:
        """`units` without the proved stubs — for a map whose dead sites an
        injected filter already dropped (`observe` has recorded it)."""
        if not self.proved:
            return units
        return {k: v for k, v in units.items() if not self._is_proved(_loc(k))}

    def admit_sites(self, sites: Iterable[FnSite]) -> frozenset[FnSite]:
        sites = tuple(sites)
        self.observe(sites)
        return frozenset(s for s in sites if not self._closed(s))

    def admit_pairs(self, pairs: Iterable[FormatterPair]) -> frozenset[FormatterPair]:
        kept: set[FormatterPair] = set()
        for p in pairs:
            members = p.member_locators()
            self.observe(members)
            if not any(self._closed(m) for m in members):
                kept.add(p)
        return frozenset(kept)

    def proved_report(self) -> tuple[str, ...]:
        """The proved stubs some insert proposed this iteration, by name."""
        return tuple(sorted(f"{u.file}::{u.fn_name}" for u in self.withheld_proved))

    def report(self, cluster_name: str) -> ClusterMembershipReport | None:
        """The iteration's one admission line, or None when nothing was
        refused."""
        if not self.refused:
            return None
        return ClusterMembershipReport(
            cluster_name, len(self.proposed),
            tuple(sorted(f"{u.file}::{u.fn_name}" for u in self.refused)),
            frontier=True)
