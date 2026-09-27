"""Integration: the schedule's `attributed` input, derived the way the loop does.

Real cargo and libtest (the pipeline's runner, with isolation), the real syn
extractor for fn records, and the real attribution chain
(`attribution.attribute_failures`). No model: the LLM classifier is the
fourth path and is scripted empty, so the deterministic behaviour path has to
carry the attribution on its own.

Why it exists. The fill schedule reads six measured inputs, and on
2026-09-27 (run `118933-877c43bd`) two of the crash-reading ones turned out
to have been dead for eight days with every test green, because every test
of the schedule fed it hand-written inputs. `attributed` decides the
coverage-split rule: a run aimed at a leftover test that writes only units
the test merely executes through waits behind the run that writes the
units the test's source names. This module derives that set from what real
cargo prints and asserts the decision it feeds; `full_pre_run.sh` runs it.

The crate is invented: `triple` is the body the test names and gets wrong,
`helper` is a unit it executes through (another file), and `unrelated` is
named by no failing test.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    attribute_failures,
)
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, schedule
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.oracle_ledger import bare_test_name
from pipeline_core.domain.rust_fn_headers import locate_fn
from pipeline_core.domain.test_results import parse_failed_tests

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "attribution"
_CMD = "cargo test --offline --no-fail-fast -- --test-threads=1"

_LIB = """mod util;

pub fn triple(n: u32) -> u32 {
    util::helper(n) * 2
}

pub fn unrelated() -> u32 {
    7
}
"""

_UTIL = """pub fn helper(n: u32) -> u32 {
    n
}
"""

_TESTS = """use nums::triple;

#[test]
fn triples() {
    assert_eq!(triple(2), 6);
}

#[test]
fn passes() {
    assert_eq!(1 + 1, 2);
}
"""

TRIPLE = FnLocator("src/lib.rs", "triple")
HELPER = FnLocator("src/util.rs", "helper")


def _crate(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "nums"\nversion = "0.1.0"\nedition = "2021"\n'
        "\n[lib]\ndoctest = false\n")
    (root / "src" / "lib.rs").write_text(_LIB)
    (root / "src" / "util.rs").write_text(_UTIL)
    (root / "tests" / "t.rs").write_text(_TESTS)
    return root


def _attributed(root: Path) -> tuple[frozenset[FnLocator], frozenset[str]]:
    """The loop's derivation: the sweep's capture, `attribute_failures` over
    the cluster root with the test texts in memory, then the units owning a
    leftover test (`phase_c_tdd`: `_units_for_owners` over the leftovers)."""
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    _rc, out = run_cargo(CargoCli(), _CMD, root, env, timeout=600)
    failed = frozenset(bare_test_name(t) for t in parse_failed_tests(out))
    deps = AttributionDeps(
        structure=CliStructureReader(CliExtractor(_EXTRACTOR)),
        classify=lambda *_a, **_k: {},
        locate_fn=locate_fn,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda cmd: cmd,
        is_gen_tests_file=lambda _p: False,
        log=lambda _m: None)
    attr = attribute_failures(
        deps, out, root, [], {}, "main",
        test_texts={"tests/t.rs": _TESTS})
    owners = frozenset(
        loc for loc, tests in attr.frontier.items()
        if {bare_test_name(t) for t in tests} & failed)
    return owners, failed


def test_the_failing_tests_named_body_is_the_attributed_write(
        tmp_path: Path) -> None:
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"
    owners, failed = _attributed(_crate(tmp_path / "c"))
    assert failed == {"triples"}, "premise: exactly the wrong body's test fails"
    assert TRIPLE in owners, owners
    assert HELPER not in owners and FnLocator("src/lib.rs", "unrelated") not in owners


def test_the_split_run_waits_behind_the_attributed_write(tmp_path: Path) -> None:
    owners, _ = _attributed(_crate(tmp_path / "c"))
    runs = [
        RouteRun(RouteKind.FILE, "spine", frozenset({HELPER}),
                 target_tests=frozenset({"triples"}), seq=0),
        RouteRun(RouteKind.PER_FN, "named", frozenset({TRIPLE}),
                 target_tests=frozenset({"triples"}), seq=1),
    ]
    s = schedule(runs, [], oracle_fp="o", hollow={}, attributed=owners)
    assert [r.run.label for r in s.ordered] == ["named"]
    assert [(r.run.label, r.reason) for r in s.deferred] == [
        ("spine", "coverage_split")]


def test_negative_control_with_no_attribution_the_split_run_opens(
        tmp_path: Path) -> None:
    # The same runs when the input comes back empty (what a reader that went
    # blind would hand the schedule): nothing is a split and both open.
    runs = [
        RouteRun(RouteKind.FILE, "spine", frozenset({HELPER}),
                 target_tests=frozenset({"triples"}), seq=0),
        RouteRun(RouteKind.PER_FN, "named", frozenset({TRIPLE}),
                 target_tests=frozenset({"triples"}), seq=1),
    ]
    s = schedule(runs, [], oracle_fp="o", hollow={}, attributed=frozenset())
    assert sorted(r.run.label for r in s.ordered) == ["named", "spine"]
