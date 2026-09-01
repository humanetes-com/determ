"""A failing test whose measured entry set excludes the followed implementer
is attributed to the dispatcher.

The format-implementer hop's theory — "the implementer is the body the
verdict actually pins" — is refuted on any tree whose dispatcher delegates
only on an arm the failing test does not take. Run `358833-aa15d93c`
(2026-09-01, cluster `stable`, the 135/137 plateau) printed the refutation
every sweep: the attribution to `src/fmt.rs::debug` two lines above
`red-dead … executed by no FAILING test: src/fmt.rs::debug`, while the
refuted owner ate three file-first opens and the per-impl fix loop.

Fixtures are the RECORDED inputs, not hand-made shapes:

- `tests/fixtures/formatter_pair_358833.json` — the extractor's fn records
  over the live `.skeleton-filled/stable` tree (the real `impl Debug for
  Error::fmt` body, whose delegation is `f.alternate()`-gated) plus the
  verbatim failing test sources (`tests/fmt_gen_3.rs`, `tests/test_fmt.rs`);
- `tests/fixtures/owner_map_358833-aa15d93c.json` — the
  `phase_c.coverage.owner_map` emission of that run's final sweep;
- `tests/fixtures/owner_map_test_altdebug.json` — run `114436-9cb17382`'s
  recorded map, where `test_altdebug`'s `{:#?}` measurably ENTERS the
  implementer (the negative control).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    behavior_frontier_from_texts,
)
from pipeline_core.domain import coverage_slice as cov
from pipeline_core.domain.attribution import FormatterPair
from pipeline_core.domain.coverage_slice import CoverageSlice, unreached_by_test
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import locate_fn

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_PAIR_FX = json.loads(
    (_FIXTURES / "formatter_pair_358833.json").read_text(encoding="utf-8"))
_MAP_358833 = json.loads(
    (_FIXTURES / "owner_map_358833-aa15d93c.json").read_text(encoding="utf-8"))
_MAP_ALTDEBUG = json.loads(
    (_FIXTURES / "owner_map_test_altdebug.json").read_text(encoding="utf-8"))

_REDS = ("test_debug_with_captured_backtrace_simple",
         "test_debug_with_captured_backtrace_and_cause")
_OUT = "".join(f"test {t} ... FAILED\n" for t in _REDS)


class _Structure:
    def enumerate_fns(self, _src: Path) -> list[dict[str, Any]]:
        return list(_PAIR_FX["fn_records"])


def _deps() -> tuple[AttributionDeps, list[str]]:
    lines: list[str] = []
    return AttributionDeps(
        structure=_Structure(),
        classify=lambda **_k: {},
        locate_fn=locate_fn,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lines.append,
    ), lines


def _slice_of(rec: dict[str, Any]) -> CoverageSlice:
    owners = {t: frozenset(FnLocator(*u.rsplit("::", 1)) for u in us)
              for t, us in rec["owners"].items()}
    return CoverageSlice(owners=owners, failing=frozenset(rec["failing"]))


@pytest.fixture(autouse=True)
def _unbind() -> Any:
    yield
    cov.bind(None)


def _owners_of(frontier: dict, test: str) -> set[str]:
    return {f"{k.file}::{k.fn_name}"
            for k, v in frontier.items() if test in v}


def test_a_measured_unentered_implementer_yields_to_the_dispatcher() -> None:
    """The plan's sentence: a failing test whose measured entry set excludes
    the followed implementer is attributed to the dispatcher — the body the
    coverage shows the test DOES pin."""
    cov.bind(_slice_of(_MAP_358833))
    deps, lines = _deps()
    pairs: set[FormatterPair] = set()
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, _OUT, {"tests/fmt_gen_3.rs": _PAIR_FX["test_files"][
            "tests/fmt_gen_3.rs"]}, Path("/x"), pairs_out=pairs)
    for t in _REDS:
        owners = _owners_of(frontier, t)
        assert "src/error.rs::fmt" in owners, (t, owners)
        assert "src/fmt.rs::debug" not in owners, (t, owners)
    assert any("REDIRECTED to the dispatcher" in ln for ln in lines)
    # The hop still records the pair it resolved — the redirect changes the
    # attributed owner, never the pair the cross-file route dispatches.
    assert any(p.dispatcher.file == "src/error.rs"
               and p.implementer.file == "src/fmt.rs"
               and p.cross_file for p in pairs), pairs


def test_negative_control_an_entered_implementer_is_unchanged() -> None:
    """`test_altdebug`'s `{:#?}` measurably enters `src/fmt.rs::debug`
    (recorded map, run `114436-9cb17382`): the hop's choice is exactly
    today's."""
    cov.bind(_slice_of(_MAP_ALTDEBUG))
    deps, lines = _deps()
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, "test test_altdebug ... FAILED\n",
        {"tests/test_fmt.rs": _PAIR_FX["test_files"]["tests/test_fmt.rs"]},
        Path("/x"))
    owners = _owners_of(frontier, "test_altdebug")
    assert "src/fmt.rs::debug" in owners, owners
    assert not any("REDIRECTED" in ln for ln in lines)


def test_negative_control_no_reading_bound_is_todays_behavior() -> None:
    """Unbound (no sweep measured, a crashed binary, a fresh process): the
    hop behaves exactly as before the reading existed — the implementer."""
    cov.bind(None)
    deps, lines = _deps()
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, _OUT, {"tests/fmt_gen_3.rs": _PAIR_FX["test_files"][
            "tests/fmt_gen_3.rs"]}, Path("/x"))
    for t in _REDS:
        owners = _owners_of(frontier, t)
        assert "src/fmt.rs::debug" in owners, (t, owners)
        assert "src/error.rs::fmt" not in owners, (t, owners)
    assert not any("REDIRECTED" in ln for ln in lines)


def test_unreached_by_test_reconciles_names_and_declines_on_ambiguity() -> None:
    unit = FnLocator("src/fmt.rs", "debug")
    cov.bind(_slice_of(_MAP_358833))
    # Exact key.
    assert unreached_by_test(unit, _REDS[0]) is True
    # A module-qualified alias of a measured leaf reconciles to it.
    assert unreached_by_test(unit, f"fmt_gen_3::{_REDS[0]}") is True
    # An unmeasured test is ignorance, not evidence.
    assert unreached_by_test(unit, "test_never_measured") is False
    # A measured-entered unit refuses.
    assert unreached_by_test(FnLocator("src/error.rs", "fmt"),
                             _REDS[0]) is False
    # Two measured tests sharing a leaf make the alias ambiguous: decline.
    two = _slice_of(_MAP_358833)
    owners = dict(two.owners)
    owners[f"other_mod::{_REDS[0]}"] = owners[_REDS[0]]
    cov.bind(CoverageSlice(owners=owners, failing=two.failing))
    assert unreached_by_test(unit, f"third::{_REDS[0]}") is False
