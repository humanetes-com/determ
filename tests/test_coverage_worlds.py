"""The owner probe in every build world the sweep judges, merged
(`coverage_owner.measure_coverage_slice`, `coverage_slice.merge_world_bodies`,
`CoverageSlice.unclaimed`).

Run `1947461-b4eb65c3` (2026-10-24) judged one tree in `stable` and
`nightly` and probed `stable` only. Its last owed red test, gated on a cfg
only `nightly` sets, was in neither `owners` nor `unmeasured_tests` for 14
iterations. The shapes below are invented.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters import coverage_owner as cov
from pipeline_core.adapters.coverage_owner import ProbeWorld, _WorldReading
from pipeline_core.domain.coverage_slice import (
    CountedBody,
    CoverageSlice,
    merge_world_bodies,
    withhold_unmeasured,
)
from pipeline_core.domain.measurement import failing_binaries_by_world
from pipeline_core.domain.models import FnLocator, FnSite

_A = FnSite("src/a.rs", "alpha", "")
_B = FnSite("src/b.rs", "beta", "")


def test_a_body_one_world_never_compiled_is_judged_by_the_world_that_did() -> None:
    home = [CountedBody(_A, 1, 0, (2, 3), 3)]
    other = [CountedBody(_A, 1, 4, (3,), 3), CountedBody(_B, 9, 0, (10,), 2)]
    merged = {(b.locator, b.start): b for b in merge_world_bodies([home, other])}
    assert merged[(_A, 1)].count == 4
    # Lines no compiling world reached: one source text, one numbering.
    assert merged[(_A, 1)].zero_lines == (3,)
    # Only the other world compiled beta, and it counted 0: dead.
    assert merged[(_B, 9)].count == 0


def test_an_asked_name_nothing_answered_is_unclaimed_and_withholds_dead() -> None:
    reading = CoverageSlice(dead=frozenset({FnLocator("src/b.rs", "beta")}),
                            owners={"seen": frozenset({_A})},
                            failing=frozenset({"seen", "nowhere"}))
    assert reading.unclaimed == {"nowhere"}
    out, withheld = withhold_unmeasured(reading)
    assert out.dead == frozenset() and withheld == {FnLocator("src/b.rs", "beta")}
    # Not a run that started: no recorded set may stand in for it.
    assert "nowhere" not in out.unmeasured_tests
    # Negative control: everything asked was answered, nothing withheld.
    whole = CoverageSlice(dead=reading.dead, owners={"seen": frozenset({_A})},
                          failing=frozenset({"seen"}))
    assert withhold_unmeasured(whole)[0].dead == reading.dead


def test_failures_split_by_world_with_the_home_world_unprefixed() -> None:
    pairs = [("other::tests/g.rs", "t"), ("tests/h.rs", "u"),
             ("home::tests/h.rs", "v")]
    assert failing_binaries_by_world(pairs, "home") == {
        "other": {"t": frozenset({"g"})},
        "home": {"u": frozenset({"h"}), "v": frozenset({"h"})}}


def _fake_world(calls: list[tuple[str, tuple[str, ...]]]) -> Any:
    def measure(project_dir: Path, cluster: str, world: ProbeWorld, *,
                home: str, failing_tests: Any = (), **_k: Any) -> _WorldReading:
        names = tuple(failing_tests)
        calls.append((world.name, names))
        site = _A if world.name == "home" else _B
        return _WorldReading(
            world=world.name, bodies=[CountedBody(site, 1, 1)],
            owners={n: frozenset({site}) for n in names}, rendered={},
            unmeasured_tests=frozenset(), unmeasured_binaries=(),
            hung_tests=frozenset())
    return measure


def test_each_world_is_asked_the_names_it_failed(monkeypatch: Any,
                                                 tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(cov, "_measure_world", _fake_world(calls))
    worlds = [ProbeWorld("home"), ProbeWorld("other")]
    reading = cov.measure_coverage_slice(
        tmp_path, "c", worlds=worlds, failing_tests=["t", "u", "v"],
        failed_pairs=[("other::tests/g.rs", "t"), ("home::tests/h.rs", "u")],
        enumerate_fns=lambda _p: [])
    assert reading is not None
    assert dict(calls) == {"home": ("u", "v"), "other": ("t",)}
    assert reading.owners["t"] == {_B} and reading.owners["u"] == {_A}
    assert reading.worlds == {"home", "other"} and not reading.unclaimed


def test_negative_control_the_home_world_alone_leaves_the_name_unclaimed(
        monkeypatch: Any, tmp_path: Path) -> None:
    """The pre-change reading: one world probed. The name failed only in the
    other world is asked nowhere and reads unclaimed, never silently absent."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(cov, "_measure_world", _fake_world(calls))
    reading = cov.measure_coverage_slice(
        tmp_path, "c", worlds=[ProbeWorld("home")], failing_tests=["t"],
        failed_pairs=[("other::tests/g.rs", "t")], enumerate_fns=lambda _p: [])
    assert reading is not None
    assert reading.unclaimed == {"t"} and "t" not in reading.owners


def test_an_owners_only_probe_runs_only_the_worlds_its_names_failed_in(
        monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(cov, "_measure_world", _fake_world(calls))
    reading = cov.measure_coverage_slice(
        tmp_path, "c", worlds=[ProbeWorld("home"), ProbeWorld("other")],
        failing_tests=["t"], failed_pairs=[("other::tests/g.rs", "t")],
        whole_suite=False, enumerate_fns=lambda _p: [])
    assert reading is not None
    assert [w for w, _ in calls] == ["other"]
    assert reading.dead == frozenset()


def test_worlds_are_required() -> None:
    with pytest.raises(ValueError):
        cov.measure_coverage_slice(Path("."), "c", worlds=[],
                                   enumerate_fns=lambda _p: [])
