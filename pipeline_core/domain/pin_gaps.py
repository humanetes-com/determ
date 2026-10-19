"""Blocking bodies a pin pass did not pin, kept until a body or a pin exists.

The end gate names every body that blocks. One candidate can pin only the
body its rebuilt panic lands in, and the file count still re-enters t2a
(run `1680589-7a6824f8`, 2026-10-19: two `provide` impls, one pin). The
body the candidate missed is still blocking. It is asked again on the next
iteration, including one whose suite is red because of the pin that landed,
because that is when the tree may have changed which body a call panics in.
A body that is no longer `unimplemented!()` has left the set: the fill
wrote it, and another pin would be a second test for a body that exists.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from pipeline_core.domain.attribution import canon_site
from pipeline_core.domain.models import FnSite

__all__ = ["gaps_left", "gaps_still_unimplemented"]


def gaps_left(blocking: Sequence[FnSite],
              pinned: Iterable[FnSite]) -> tuple[FnSite, ...]:
    """The blocking bodies `pinned` does not include, in blocking order."""
    got = {canon_site(s) for s in pinned}
    return tuple(s for s in blocking if canon_site(s) not in got)


def gaps_still_unimplemented(pending: Sequence[FnSite],
                             unimplemented: Iterable[FnSite],
                             ) -> tuple[FnSite, ...]:
    """Pending gaps that are still `unimplemented!()` on the tree."""
    live = {canon_site(s) for s in unimplemented}
    return tuple(s for s in pending if canon_site(s) in live)
