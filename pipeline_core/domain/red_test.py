"""One record per owed red test: the units it is observed to run through.

WHY. On run `1604111-bb3716ef` (2026-11-05, cluster `stable`, iter 9) the
loop held four views of one fact, which units a red test executes:

- the rank's owner map: what the sweep's coverage saw the test enter,
  taken BEFORE `withhold_unmeasured` (13 sites for `test_downcast_low`,
  whose binary aborted), plus a recorded set where nothing was measured;
- the session slicer's owners: the coverage map AFTER the withhold, in
  which an aborted test is absent by design, plus recorded sites;
- the recorded sites (empty here: a positive owner existed);
- the attribution's site frontier, which `merge_slices` preferred as soon
  as the session slice was non-empty (`drop`, one unit).

The rank admitted six owners the owed walk could never owe, and the next
filter removed them as served with nothing owing them. Every view was
correct by its own contract; the split was that each reader built its own.

WHAT. `read_red_owners` is the one writer of the owner set. Every reader
takes a view of the same record:

- `rank_owners()` for the owner admission steps and the owner-map event
  (recorded sets as `(file, fn)` stamps with an empty container, which
  ranking converts back to locators);
- `session_owners()` for the session slicer: positive sites with the
  container the coverage reading carried, recorded sets only as sites the
  attribution frontier names with exactly one container (a recorded stamp
  with an empty container would be expanded by `expand_locator_sites` into
  every same-named body, run `110993-6efa23be`, 2026-09-03), plus the
  sites a faulting stack resolved to;
- `walk_owners()` for the owed walk (`merge_slices`), as `file::fn` keys.

All three are taken after `owning_view`: a unit whose own keep moved a
still-red test past it no longer owns that test.

What this record does NOT change: the coverage reading bound for negative
claims stays the post-withhold one (`coverage_slice.bind`). Binding the
positive prefix as `owners` would make an aborted test "measured", and
every unit its run did not reach would then read as proven unentered.

`owners_off_tiers` is the split check: an owner of an owed test that is on
none of that test's tiers. The loop logs it each sweep (shadow), and
`scripts/red_test_invariants.py` computes it over recorded sweeps.

Nothing here names a crate, file, test, trait or symbol.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pipeline_core.domain.coverage_slice import records_standing_in
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.premise_advance import owning_view
from pipeline_core.domain.slice_session import recorded_locator_sites

__all__ = [
    "OwnerSource",
    "RedOwners",
    "RedTest",
    "owners_off_tiers",
    "read_red_owners",
]


def _bare(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def _key(file: str, fn: str) -> str:
    return f"{file}::{fn}"


class OwnerSource(StrEnum):
    """Where a test's owner set came from on this sweep."""

    POSITIVE = "positive"   # what this sweep's coverage saw it enter
    RECORDED = "recorded"   # what an earlier run of it was recorded entering
    NONE = "none"


@dataclass(frozen=True)
class RedTest:
    """One owed red test's owners, after `owning_view`.

    `rank` holds every owner as a site (recorded owners with an empty
    container); `session` holds the owners a session may edit as sites
    (container as observed, homonyms resolved or left out)."""

    name: str
    source: OwnerSource
    rank: frozenset[FnSite] = frozenset()
    session: frozenset[FnSite] = frozenset()
    recorded: frozenset[FnLocator] = frozenset()

    @property
    def keys(self) -> frozenset[str]:
        """Every owner as `file::fn`, the owed walk's key."""
        return frozenset(_key(s.file, s.fn_name) for s in self.rank)


@dataclass(frozen=True)
class RedOwners:
    """The sweep's owner record, keyed by bare test name."""

    by_test: Mapping[str, RedTest] = field(default_factory=dict)

    def rank_owners(self) -> dict[str, frozenset[FnSite]]:
        return {t: r.rank for t, r in self.by_test.items()}

    def session_owners(self) -> dict[str, frozenset[FnSite]]:
        return {t: r.session for t, r in self.by_test.items() if r.session}

    def walk_owners(self, exclude: Collection[str] = ()) -> dict[str, frozenset[str]]:
        """`file::fn` owners per test, less the tests in `exclude` (bare or
        qualified names): a test whose hop-pair transaction is withheld takes
        attributed units only, in the walk as in the session."""
        out_ = {_bare(t) for t in exclude}
        return {t: r.keys for t, r in self.by_test.items()
                if r.keys and t not in out_}

    def recorded_owners(self) -> dict[str, frozenset[FnLocator]]:
        return {t: r.recorded for t, r in self.by_test.items() if r.recorded}


def read_red_owners(
    *,
    positive: Mapping[str, Collection[FnSite]],
    recorded: Mapping[str, Collection[FnLocator]],
    owed: Collection[str],
    advanced: Mapping[str, frozenset[FnLocator]],
    owned_elsewhere: Collection[str] = (),
    frontier_sites: Iterable[FnSite] = (),
    stack_sites: Mapping[str, Collection[FnSite]] | None = None,
) -> RedOwners:
    """The one writer of the owner set.

    `positive` is the coverage reading's owner map taken BEFORE the
    withhold (what every run, completed or not, was seen to enter);
    `recorded` the stand-in sets of runs that did not complete; `owed` the
    sweep's owed red names; `advanced` and `owned_elsewhere` the
    `owning_view` inputs; `frontier_sites` the attribution's site frontier
    (the only source of a container for a recorded locator); `stack_sites`
    the bodies an aborted test's own faulting stack resolved to.

    A recorded set stands in only where no positive owner exists
    (`records_standing_in`)."""
    standing = records_standing_in(dict(recorded), positive, owed)
    rank_src: dict[str, set[FnSite]] = {}
    session_src: dict[str, set[FnSite]] = {}
    source: dict[str, OwnerSource] = {}
    for t, ks in positive.items():
        b = _bare(t)
        rank_src.setdefault(b, set()).update(ks)
        session_src.setdefault(b, set()).update(ks)
        if ks:
            source[b] = OwnerSource.POSITIVE
    rec_by_test: dict[str, frozenset[FnLocator]] = {}
    for t, us in standing.items():
        b = _bare(t)
        rec_by_test[b] = rec_by_test.get(b, frozenset()) | frozenset(us)
        rank_src.setdefault(b, set()).update(
            FnSite(u.file, u.fn_name, "") for u in us)
        source.setdefault(b, OwnerSource.RECORDED)
    rec_sites = recorded_locator_sites(standing, frontier_sites)
    for t, ks in (stack_sites or {}).items():
        if t in standing:
            rec_sites[t] = rec_sites.get(t, frozenset()) | frozenset(ks)
    for t, ks in rec_sites.items():
        session_src.setdefault(_bare(t), set()).update(ks)
    rank = owning_view(rank_src, advanced, owned_elsewhere=owned_elsewhere)
    session = owning_view(session_src, advanced, owned_elsewhere=owned_elsewhere)
    names = set(rank) | set(session)
    return RedOwners({
        t: RedTest(
            name=t,
            source=source.get(t, OwnerSource.NONE),
            rank=rank.get(t, frozenset()),
            session=session.get(t, frozenset()),
            recorded=rec_by_test.get(t, frozenset()))
        for t in sorted(names)})


def owners_off_tiers(
    owners: Mapping[str, Collection[str]],
    tiers: Mapping[str, Iterable[Iterable[str]]],
) -> dict[str, frozenset[str]]:
    """Per owed test with tiers: the owners (`file::fn`) on none of them.

    Non-empty means a reader built the walk's slice from something other
    than the owner record: the walk can never owe those units, and the
    rank may admit them only for a later filter to remove them."""
    out: dict[str, frozenset[str]] = {}
    for t, ts in tiers.items():
        on = {u for tier in ts for u in tier}
        off = frozenset(owners.get(_bare(t), ())) - on
        if off:
            out[_bare(t)] = off
    return out
