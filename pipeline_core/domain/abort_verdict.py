"""One rule for a test that a judged write killed without a panic.

WHY. Every keep read "any silent abort refuses, whatever was gained"
(`assertion_position.TestOutcomes.refuses`). The rule was written from run
`2157238-210637e6` (2026-10-11): five self-calling `construct_from_*` bodies
were kept as non-regression, and from the next sweep 26 binaries died of
stack exhaustion and 86-95 tests had no owner reading. That write gained
nothing, and its tests lost every reading.

The opposite shape is run `42057-641e589c` (2026-10-26), fills 5 and 6. A
session's turn measured net +20 with nothing newly red. Its only target was
already red and now died with SIGSEGV, and the crash localized to a crate
body. The gate threw the state away (log: `turn 6's state (+20, 0 newly red)
is one the keep gate refuses`). Nine judged turns of +8 to +20 went the same
way. The aborted tests were already failing, every other test of their binary
was re-run in its own process (each newly passing test is a reading of its
own), and each abort carried a crate site, which the red-test sight already counts
as seeing the test (`test_sight`, `CRASH_STACK`).

THE PARTITION. A silent abort of a judged test is exactly one of:

- ``was_green`` — it passed before. A lost verdict: refuses, no exception.
- ``red_unlocated`` — it was failing before and no crash site of it names an
  authorable crate frame (nothing localized, or the origin named nobody).
  Its reading is gone: refuses, no exception.
- ``red_located`` — it was failing before and a crash site of it names an
  authorable crate frame (the same frames the sight reads:
  :func:`crash_units_by_test`). Its reading is replaced, not destroyed.
  Forgiven only beside a strict gain and with no previously-passing test
  failing or aborting.

What the rule does NOT decide: whose fault the crash is. A written body that
returns a dangling pointer has returned before the bad read, so its absence
from the stacks proves nothing (fill 5's write `object_downcast` is on none of
the six archived sanitizer reports of that session). The fault question goes
to the next fill, with the crash in its prompt (`Settlement.render`) and the
crash's frames on the frontier.

THE INPUT. The sites are a typed argument (`CrashSite`, from
`localize_crashes` through the settlement or the settler's read-only
`localize`), never a boolean: a caller cannot claim "located" without a site,
and a caller with no sites reads every red abort as unlocated, which is the
rule before this module.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from pipeline_core.domain.crash_site import CrashSite, authorable_frames
from pipeline_core.domain.models import FnLocator

__all__ = ["AbortPartition", "crash_units_by_test", "located_tests"]


def _bare(name: str) -> str:
    return str(name).rsplit("::", 1)[-1]


def crash_units_by_test(
    sites: Iterable[CrashSite],
) -> dict[str, frozenset[FnLocator]]:
    """Bare test name -> the crate units its crash sites name: the
    authorable faulting and origin frames, and the culprit locators. The one
    reading of "this crash names a body the loop can rewrite", shared by the
    sight (`test_sight.Observations`) and the abort partition."""
    out: dict[str, set[FnLocator]] = {}
    for site in sites:
        test = _bare(site.test_name or "")
        if not test:
            continue
        units = {FnLocator(f.file_rel, f.fn_name)
                 for f in authorable_frames(site.frames)}
        units |= {FnLocator(f.file_rel, f.fn_name)
                  for f in authorable_frames(site.origin_frames)}
        units |= set(site.culprit_locators)
        if units:
            out.setdefault(test, set()).update(units)
    return {t: frozenset(v) for t, v in out.items()}


def located_tests(sites: Sequence[CrashSite]) -> frozenset[str]:
    """Bare names of the tests a crash site of which names a crate unit."""
    return frozenset(crash_units_by_test(sites))


@dataclass(frozen=True, slots=True)
class AbortPartition:
    """The silent aborts of one judged write, by what each still tells."""

    was_green: frozenset[str] = frozenset()
    red_unlocated: frozenset[str] = frozenset()
    red_located: frozenset[str] = frozenset()

    @classmethod
    def of(cls, aborted: Collection[str], *, green_before: Collection[str],
           located: Collection[str]) -> AbortPartition:
        """`aborted` (bare names) split by `green_before` and `located`
        (:func:`located_tests` of the write's crash sites)."""
        names = {_bare(n) for n in aborted}
        green = names & {_bare(n) for n in green_before}
        loc = {_bare(n) for n in located}
        red = names - green
        return cls(was_green=frozenset(green),
                   red_unlocated=frozenset(red - loc),
                   red_located=frozenset(red & loc))

    @property
    def all(self) -> frozenset[str]:
        return self.was_green | self.red_unlocated | self.red_located

    def refuses(self, *, strict_gain: bool, lost: bool) -> bool:
        """Whether these aborts refuse the write. `strict_gain` is the gain
        the caller's keep is judged on (the session's suite net, a route's
        aimed tests turned green); `lost` is whether any previously-passing
        test fails or aborts beside them."""
        if self.was_green or self.red_unlocated:
            return True
        if self.red_located:
            return not strict_gain or lost
        return False

    def forgiven(self, *, strict_gain: bool, lost: bool) -> frozenset[str]:
        """The aborts the rule lets stand, for the log and the ledger delta."""
        return (frozenset() if self.refuses(strict_gain=strict_gain, lost=lost)
                else self.red_located)

