"""Typed ports for the TDD loop's two readers of a sweep it does not write:
which units a failing test is attributed to, and which bodies each failing
test executed.

Both were `Callable[..., X]` fields of `PhaseCTddDeps`, so mypy could not
see a keyword the bound implementation does not take. The driver added one
keyword to each on 2026-10-24 (the withdrawn tests to attribution, the
world-qualified failures to the owner probe); typed here, every driver call
is read against the real signature.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from pipeline_core.domain.attribution import AttributionResult
from pipeline_core.domain.coverage_slice import CoverageSlice
from pipeline_core.domain.models import FnLocator

__all__ = ["AttributeFailuresPort", "MeasureCoveragePort"]


class AttributeFailuresPort(Protocol):
    """`application.reconstruct.attribution.attribute_failures` with its
    deps bound. `unrepairable_tests` are bare test names no body rewrite can
    satisfy (the loop passes the tests the original crate fails the same
    way); every path drops them."""

    def __call__(
        self,
        test_output: str,
        src_dir: Path,
        recently_filled: list[FnLocator],
        test_reverse_map: dict[str, set[FnLocator]],
        cluster_name: str,
        /,
        *,
        test_texts: dict[str, str] | None = ...,
        extra_failed: frozenset[str] = ...,
        unrepairable_tests: frozenset[str] = ...,
    ) -> AttributionResult: ...


class MeasureCoveragePort(Protocol):
    """The owner probe as composition binds it: the caller passes its home
    `cluster_env` and the port probes every build world the sweep judges
    (`adapters.coverage_owner.measure_coverage_slice`). `failed_pairs` are
    the sweep's (binary key, name) failures, world-qualified where the
    capture joined worlds; `whole_suite` False asks owners only."""

    def __call__(
        self,
        project_dir: Path,
        cluster: str,
        /,
        *,
        cluster_env: Mapping[str, str] | None = ...,
        failing_tests: Iterable[str] = ...,
        failed_pairs: Iterable[tuple[str, str]] = ...,
        whole_suite: bool = ...,
        timeout: int | None = ...,
        hung: Mapping[str, str] | None = ...,
    ) -> CoverageSlice | None: ...
