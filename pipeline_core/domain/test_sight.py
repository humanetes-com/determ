"""Whether each owed red test was SEEN this sweep, through what, or why not.

WHY. Every plateau that needed a session to diagnose since August had one
of a few causes, and a large share of them was a red test the loop could not
observe: a binary that died with no verdict, a crash whose stack named no
crate frame, a test built only in a world the owner probe did not build. The
loop never said "I cannot see this test". It fell back to a guess (the
recently-filled ring, the classifier, the test text's identifiers, the
static reverse map) and kept dispatching. On run `1947461-b4eb65c3`
(2026-10-24) the last owed red test, `doc_error_example_6`, had no
observation at all from iter 13: the iteration line printed `unowned=0`
because a recency edge counted as an owner, and fills 13 and 14 aimed every
run at units off the failing path. Over every recorded run, iterations whose
frontier came only from those guesses numbered 17 and were followed by one
pass increase.

WHAT. A test is SEEN when this sweep holds an observation of it executing on
this tree, and the sources are only those observations:

- ``MEASURED``: a non-empty coverage owner set (the bodies it entered before
  its failure was reported), from a world that built it;
- ``RECORDED``: the recorded entry set standing in for a run that started
  and did not complete. Not for a test no probed binary claimed: that is
  not a run that started;
- ``CRASH_STACK``: crate frames on ANY crash site of the test, not the first
  one; one site that names nobody does not make the test unseen when
  another site has a crate frame;
- ``FAILURE_SITE``: its first failure inside a crate body, a held stub
  included.

Everything else is an inference, and no constructor here takes one:
`Observations` is built from the measurement types only (the coverage
reading, the recorded entry sets, the crash sites, the first-failure sites),
never from a frontier or an attribution result. A test's own source naming
a function (the behaviour path, the reverse map) is text, not execution.

An UNSEEN test carries the reason the measurements give, most specific
first: ``not_built_in_probed_world`` (it failed only in a world the probe
did not run), ``probe_unavailable`` (no coverage reading this sweep),
``unclaimed`` (probed, no binary claimed the name), ``enters_no_body``
(measured, entered no crate body), ``crash_no_crate_frame`` (it crashed and
no site names a crate frame), ``did_not_complete_no_record`` (its run died,
nothing recorded), else ``no_observation``.

The reading is not taken on a red build (the compile frontier is the
observation and every test is red because of it), nor when the loop has no
coverage port at all: a loop configured without the instrument cannot say a
test was unseen, only that it never looked.

Nothing here names a crate, file, test, trait or symbol.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pipeline_core.domain.coverage_slice import CoverageSlice, matching_test_key
from pipeline_core.domain.crash_site import CrashSite, authorable_frames
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator, FnSite

__all__ = [
    "Observations",
    "Sight",
    "SightReading",
    "SightSource",
    "UnseenReason",
    "sight_reading",
]


class SightSource(StrEnum):
    MEASURED = "measured"
    RECORDED = "recorded"
    CRASH_STACK = "crash_stack"
    FAILURE_SITE = "failure_site"


class UnseenReason(StrEnum):
    NOT_BUILT_IN_PROBED_WORLD = "not_built_in_probed_world"
    PROBE_UNAVAILABLE = "probe_unavailable"
    UNCLAIMED = "unclaimed"
    ENTERS_NO_BODY = "enters_no_body"
    CRASH_NO_CRATE_FRAME = "crash_no_crate_frame"
    DID_NOT_COMPLETE_NO_RECORD = "did_not_complete_no_record"
    NO_OBSERVATION = "no_observation"


def _bare(name: str) -> str:
    return str(name).rsplit("::", 1)[-1]


def _same_unit(key: FnLocator, site: FnSite) -> bool:
    """File and function, ignoring a leading ``src/`` and the container.

    Extractor records and frontier keys disagree on that prefix. The
    container is not part of the dispatch key."""
    return (key.fn_name == site.fn_name
            and norm_src_rel(key.file) == norm_src_rel(site.file))


def _observed(key: FnLocator, sight: Sight) -> bool:
    return any(_same_unit(key, site) for site in sight.units)


@dataclass(frozen=True, slots=True)
class Observations:
    """What this sweep measured about its red tests. Built only by
    :meth:`of`, from measurement types; there is no way to hand it a
    frontier.

    `coverage` is the reading after nothing was withheld from its owners
    (positive evidence: what a cut run DID enter counts). `failed_worlds`
    maps a bare test name to the worlds that failed it. `home` names the
    capture's own world (a pair with no world prefix)."""

    coverage: CoverageSlice | None
    owners: Mapping[str, frozenset[FnSite]]
    recorded: Mapping[str, frozenset[FnLocator]]
    crash_units: Mapping[str, frozenset[FnLocator]]
    crashed: frozenset[str]
    failure_sites: Mapping[str, frozenset[FnSite]]
    failed_worlds: Mapping[str, frozenset[str]]

    @classmethod
    def of(cls, *, coverage: CoverageSlice | None,
           positive_owners: Mapping[str, frozenset[FnSite]] | None,
           recorded: Mapping[str, frozenset[FnLocator]],
           crash_sites: Iterable[CrashSite],
           failure_sites: Mapping[str, frozenset[FnSite]],
           failed_pairs: Iterable[tuple[str, str]],
           home: str) -> Observations:
        crash_units: dict[str, set[FnLocator]] = {}
        crashed: set[str] = set()
        for site in crash_sites:
            test = _bare(site.test_name or "")
            if not test:
                continue
            crashed.add(test)
            units = {FnLocator(f.file_rel, f.fn_name)
                     for f in authorable_frames(site.frames)}
            units |= {FnLocator(f.file_rel, f.fn_name)
                      for f in authorable_frames(site.origin_frames)}
            units |= set(site.culprit_locators)
            if units:
                crash_units.setdefault(test, set()).update(units)
        worlds: dict[str, set[str]] = {}
        for label, name in failed_pairs:
            world, sep, _rest = str(label).partition("::")
            worlds.setdefault(_bare(name), set()).add(world if sep else home)
        return cls(
            coverage=coverage,
            owners=dict(positive_owners if positive_owners is not None
                        else (coverage.owners if coverage is not None else {})),
            recorded={_bare(t): frozenset(v) for t, v in recorded.items()},
            crash_units={t: frozenset(v) for t, v in crash_units.items()},
            crashed=frozenset(crashed),
            failure_sites={_bare(t): frozenset(v)
                           for t, v in failure_sites.items()},
            failed_worlds={t: frozenset(v) for t, v in worlds.items()})


@dataclass(frozen=True, slots=True)
class Sight:
    """One owed red test's sight. `sources` empty means UNSEEN, with
    `reason`; `units` are the bodies the sources observed it run."""

    test: str
    sources: frozenset[SightSource] = frozenset()
    units: frozenset[FnSite] = frozenset()
    reason: UnseenReason | None = None
    detail: str = ""

    @property
    def seen(self) -> bool:
        return bool(self.sources)


@dataclass(frozen=True, slots=True)
class SightReading:
    """The sweep's sight over its owed red tests (bare names).

    `taken` False is a sweep that could not judge sight (a red build, or no
    coverage port): the observations it did make still give units
    (`units_of`), and no test is claimed unseen."""

    by_test: Mapping[str, Sight] = field(default_factory=dict)
    taken: bool = True

    @property
    def seen(self) -> frozenset[str]:
        return frozenset(t for t, s in self.by_test.items() if s.seen)

    @property
    def unseen(self) -> frozenset[str]:
        if not self.taken:
            return frozenset()
        return frozenset(t for t, s in self.by_test.items() if not s.seen)

    @property
    def all_unseen(self) -> bool:
        """Every owed red test unseen, and at least one owed: no fill this
        sweep can be aimed on evidence."""
        return self.taken and bool(self.by_test) and not self.seen

    def is_unseen(self, test: str) -> bool:
        s = self.by_test.get(_bare(test))
        return self.taken and s is not None and not s.seen

    def units_of(self, test: str) -> frozenset[FnSite]:
        s = self.by_test.get(_bare(test))
        return s.units if s is not None else frozenset()

    def describe_unseen(self, limit: int = 8) -> str:
        parts = [f"{t} ({s.reason}{': ' + s.detail if s.detail else ''})"
                 for t, s in sorted(self.by_test.items()) if not s.seen]
        more = len(parts) - limit
        return "; ".join(parts[:limit]) + (f" (+{more} more)" if more > 0 else "")

    def summary(self) -> str:
        if not self.taken:
            return (f"{len(self.by_test)} owed red test(s): sight not taken "
                    "this sweep")
        counts: dict[str, int] = {}
        for s in self.by_test.values():
            for src in s.sources:
                counts[src] = counts.get(src, 0) + 1
        seen = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        return (f"{len(self.by_test)} owed red test(s): {len(self.seen)} seen"
                + (f" ({seen})" if seen else "")
                + f", {len(self.unseen)} unseen"
                + (f": {self.describe_unseen()}" if self.unseen else ""))

    def payload(self) -> dict[str, Any]:
        if not self.taken:
            return {}
        return {t: ({"sources": sorted(s.sources), "units": len(s.units)}
                    if s.seen else
                    {"reason": str(s.reason), "detail": s.detail})
                for t, s in sorted(self.by_test.items())}

    def edges_without_unseen(
        self, frontier: Mapping[FnLocator, Collection[str]],
        *,
        keep: Callable[[FnLocator, str], bool] | None = None,
        keep_units: Collection[FnLocator] = (),
    ) -> dict[FnLocator, set[str]]:
        """Frontier edges a dispatch may aim, once this reading was taken.

        An unseen test loses every edge. A seen test keeps an edge when
        the unit is one this reading observed it run (same file and
        function; a leading ``src/`` does not make a second key), or when
        ``keep`` says the edge is one the loop added for a reason other
        than the attribution guess (a crash, a held stub, an escalation
        product). A test this reading does not judge keeps its edges. A
        unit with no test edge stays, and so does a unit in ``keep_units``
        (the still-stubbed functions) whose edges all went: a stub is
        fillable with no test aimed at it, which is what the stub union
        puts it on the frontier for.

        Nothing is added. A seen test whose every edge was a guess is left
        with no edge, and its observed units reach dispatch through the
        measured-owner admission (`escalation_lifetime.
        escalate_to_measured_owners`, whose empty cohort counts as served
        for a seen test), which applies the exhaustion, flat-grade, dead
        and pair-withholding gates. Writing the units here put every
        observed unit of every seen test on the eligible map after the
        exhaustion filter: an exhausted unit came back every iteration, the
        empty-frontier stop could never fire, and the coverage spine the
        admission gate exists to hold back was dispatched whole (2026-10-24,
        the walks of runs `9872-51b20f30` and `1947461-b4eb65c3` spun to
        the backstop).

        Sight not taken returns the same edges and ignores ``keep``: a
        sweep that could not judge sight does not refuse a guess.
        """
        if not self.taken:
            return {k: set(ts) for k, ts in frontier.items()}
        stubs = frozenset(keep_units)
        out: dict[FnLocator, set[str]] = {}
        for key, tests in frontier.items():
            kept: set[str] = set()
            for test in tests:
                if self.is_unseen(test):
                    continue
                sight = self.by_test.get(_bare(test))
                if sight is not None and sight.seen:
                    if (_observed(key, sight)
                            or (keep is not None and keep(key, test))):
                        kept.add(test)
                    continue
                kept.add(test)
            if kept or not tests or key in stubs:
                out[key] = kept
        return out


def _reason(test: str, obs: Observations) -> tuple[UnseenReason, str]:
    cov = obs.coverage
    worlds = obs.failed_worlds.get(test, frozenset())
    if cov is None:
        return UnseenReason.PROBE_UNAVAILABLE, ""
    if worlds and cov.worlds and not (worlds & cov.worlds):
        return (UnseenReason.NOT_BUILT_IN_PROBED_WORLD,
                f"failed in {', '.join(sorted(worlds))}; probed "
                f"{', '.join(sorted(cov.worlds))}")
    if matching_test_key(test, list(cov.unclaimed)) is not None:
        return (UnseenReason.UNCLAIMED,
                f"probed {', '.join(sorted(cov.worlds)) or 'home'}")
    key = matching_test_key(test, list(obs.owners))
    unmeasured = matching_test_key(test, list(cov.unmeasured_tests)) is not None
    if key is not None and not obs.owners[key] and not unmeasured:
        return UnseenReason.ENTERS_NO_BODY, ""
    if test in obs.crashed:
        return UnseenReason.CRASH_NO_CRATE_FRAME, ""
    if unmeasured:
        return UnseenReason.DID_NOT_COMPLETE_NO_RECORD, ""
    return UnseenReason.NO_OBSERVATION, ""


def sight_reading(owed_red: Iterable[str], obs: Observations, *,
                  taken: bool = True) -> SightReading:
    """The one writer of :class:`SightReading`. `owed_red` is the sweep's
    owed red set (failures less the withdrawn); `obs` is what the sweep
    measured. A new source of "seen" is added here, as a field of
    `Observations` built from a measurement, or not at all."""
    cov = obs.coverage
    unclaimed = cov.unclaimed if cov is not None else frozenset()
    unmeasured = cov.unmeasured_tests if cov is not None else frozenset()
    out: dict[str, Sight] = {}
    for name in sorted({_bare(t) for t in owed_red}):
        sources: set[SightSource] = set()
        units: set[FnSite] = set()
        key = matching_test_key(name, list(obs.owners))
        if key is not None and obs.owners[key]:
            sources.add(SightSource.MEASURED)
            units |= obs.owners[key]
        started = (matching_test_key(name, list(unmeasured)) is not None
                   and matching_test_key(name, list(unclaimed)) is None)
        rec = obs.recorded.get(name)
        if started and rec:
            sources.add(SightSource.RECORDED)
            units |= {FnSite(u.file, u.fn_name, "") for u in rec}
        crash = obs.crash_units.get(name)
        if crash:
            sources.add(SightSource.CRASH_STACK)
            units |= {FnSite(u.file, u.fn_name, "") for u in crash}
        site = obs.failure_sites.get(name)
        if site:
            sources.add(SightSource.FAILURE_SITE)
            units |= site
        if sources:
            out[name] = Sight(name, frozenset(sources), frozenset(units))
        else:
            reason, detail = _reason(name, obs)
            out[name] = Sight(name, reason=reason, detail=detail)
    return SightReading(out, taken=taken)
