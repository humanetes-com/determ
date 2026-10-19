"""The end gate's verdict on a green tree: may the run claim success, and if
not, which bodies block it.

A tree whose every oracle test passes can still be unfinished: a body the
crate's public API reaches is still `unimplemented!()` because no test in the
oracle reaches it in the reconstruction. Run `1680589-7a6824f8` (2026-10-19)
ended every cluster green, passed task 3 (403 tests) and then exited 1 on the
two `impl StdError for ContextError` `provide` bodies, under nightly cfg: the
merged tree's `ErrorImpl::provide` hands the request to the wrapped object, so
`request_ref::<Backtrace>` on a context error reaches the stub, and no oracle
test does that. The gate was a process exit with no loop around it; this value
is what replaces the exit, so the module loop can treat a blocking body like
a red test (a retro, then t2a again).

`blocking` is the end gate's `orphaned` bucket (`orchestrator.
classify_remaining_stubs`) and nothing else: the excused buckets
(cfg-unreachable, hollow upstream, unreached by tests, dead in the tree) are
already reported where they are decided and do not block.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.fn_execution import site_label
from pipeline_core.domain.models import FnLocator, FnSite

__all__ = ["EndVerdict"]


@dataclass(frozen=True, slots=True)
class EndVerdict:
    # By site: two impls of one trait method on one file are two bodies.
    blocking: tuple[FnSite, ...] = ()
    # The blocking bodies the original's suite executed at augmentation: the
    # ones a public route provably reaches in the original crate.
    executed_upstream: frozenset[FnSite] = frozenset()

    @property
    def clean(self) -> bool:
        return not self.blocking

    def labels(self) -> tuple[str, ...]:
        return tuple(site_label(s) for s in self.blocking)

    def locators(self) -> tuple[FnLocator, ...]:
        return tuple(dict.fromkeys(FnLocator(s.file, s.fn_name) for s in self.blocking))

    def report(self) -> str:
        """The block a failed outcome carries as its evidence text."""
        if self.clean:
            return ""
        lines = ["--- end gate ---",
                 f"{len(self.blocking)} body(ies) reachable under a baked cfg-set "
                 "are still unimplemented!() and no oracle test reaches them:"]
        for site in self.blocking:
            mark = (" (the original's suite executed it)"
                    if site in self.executed_upstream else "")
            lines.append(f"  {site_label(site)}{mark}")
        return "\n".join(lines) + "\n"
