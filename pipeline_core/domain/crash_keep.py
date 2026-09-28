"""A compiling change that is still the same crash is not a keep.

Run `371055-1a407b3c` (2026-09-27), cluster `stable`. Two session turns
compiled, every aimed test was still the stack overflow, and the written
units were still that overflow's culprits. The non-regression keep held
them. The later one-member fix of the third member then cleared twelve
tests and left the thirteenth aborting on the two-member ring those keeps
had rotated into. Reverting only those two keeps, on a scratch copy of the
tree the fill left, cleared the thirteenth test as well.

The question is asked of the written units that were culprits when the
fill started, and of no other written unit. A formatter body written beside
a rotated culprit neither saves the rotation nor condemns it: the first cut
of this rule asked "is every written unit still a culprit", and one
innocent body in the write set (the file route lands five at once, the
impl and pair routes name whole blocks) turned the refusal off.

One meaning for "the crash was not localized after the change": the
written culprits were not shown to have left it. Whether that is because
no localizer was wired or because the localizer found no in-crate frames
is the same fact. It can only refuse a change that wrote a baseline
culprit, so it cannot revert an innocent body.
"""
from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_results import (
    native_crashes,
    parse_binary_results,
)

#: (cargo test output) -> the culprits of the crashes still in it.
Localize = Callable[[str], Collection[FnLocator]]


def native_crash_leaves(output: str) -> frozenset[str]:
    """Bare test names whose own process is still a native crash in
    `output`. Empty when the capture shows none."""
    leaves: set[str] = set()
    for _binary, test in native_crashes(parse_binary_results(output or "")):
        name = test or _binary.dangling or ""
        leaf = name.rsplit("::", 1)[-1]
        if leaf:
            leaves.add(leaf)
    return frozenset(leaves)


def aimed_all_still_native_crash(
    aimed: Collection[str], output: str,
) -> bool:
    """Every aimed test's own process is still a native crash.

    A test that passed, or that failed with a verdict, is not this.
    Empty `aimed` is not this: the run named no test to still be crashing.
    """
    leaves = {a.rsplit("::", 1)[-1] for a in aimed if a}
    if not leaves:
        return False
    return leaves <= native_crash_leaves(output)


@dataclass(frozen=True, slots=True)
class SameCrashJudge:
    """The keep sites' one question: did this change leave the crash it
    was written against?

    `culprits_before` is the fill's baseline blame (the sweep's
    `crash_blamed`): a required field, so a keep site cannot ask without
    saying which units the crash held. `localize` names the culprits of a
    capture after the change; None is "not localized", the same answer as
    a localizer that found nothing."""

    culprits_before: frozenset[FnLocator]
    localize: Localize | None

    def written_culprits(
        self, written: Collection[FnLocator],
    ) -> frozenset[FnLocator]:
        """The written units the crash held when the fill started."""
        return frozenset(written) & self.culprits_before

    def culprits_after(self, output: str) -> frozenset[FnLocator]:
        if self.localize is None:
            return frozenset()
        try:
            return frozenset(self.localize(output))
        except Exception:
            return frozenset()

    def refuses(
        self, *, output: str, aimed: Collection[str],
        written: Collection[FnLocator],
    ) -> bool:
        """True when the change must be reverted, not kept.

        Not this question when the change wrote no baseline culprit, or
        when some aimed test is no longer a native crash. Otherwise the
        keep stands only if some written baseline culprit is no longer a
        culprit after the change; an unlocalized capture has not shown
        that."""
        held = self.written_culprits(written)
        if not held or not aimed_all_still_native_crash(aimed, output):
            return False
        after = self.culprits_after(output)
        return not after or held <= after
