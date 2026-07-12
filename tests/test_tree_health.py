"""F7 pins — tree-health classification + frozen-zero / skeleton-loss detectors."""
from __future__ import annotations

from pipeline_core.domain.models import Diagnostic
from pipeline_core.domain.run_health import HealthKind, analyze
from pipeline_core.domain.tree_health import TreeHealth, classify_tree_health


def test_classify_parse_error_is_unparseable() -> None:
    diags = [Diagnostic(code="", message="unexpected closing delimiter")]
    assert classify_tree_health(1, diags, 0, 0) is TreeHealth.UNPARSEABLE


def test_classify_type_error_is_uncompilable_not_unparseable() -> None:
    # an E-coded "expected X found Y" must NOT be misread as a parse error
    diags = [Diagnostic(code="E0308", message="expected u32, found i64")]
    assert classify_tree_health(1, diags, 0, 0) is TreeHealth.UNCOMPILABLE


def test_classify_ok_and_failing() -> None:
    assert classify_tree_health(0, [], 3, 0) is TreeHealth.OK
    assert classify_tree_health(101, [], 2, 1) is TreeHealth.TESTS_FAILING


def _iter_line(n: int, p: int, f: int) -> str:
    return f"  [phase-c/tdd] iter {n}: cargo test rc=101 pass={p} fail={f} stall=0/6"


def test_frozen_zero_tree_alerts_after_three() -> None:
    lines = [_iter_line(1, 0, 0), _iter_line(2, 0, 0), _iter_line(3, 0, 0)]
    kinds = {f.kind for f in analyze(lines, [])}
    assert HealthKind.FROZEN_ZERO_TREE in kinds


def test_no_frozen_zero_when_fails_reported() -> None:
    # a genuinely-hard cluster still reports fails → not the broken-tree signature
    lines = [_iter_line(1, 0, 5), _iter_line(2, 0, 5), _iter_line(3, 0, 5)]
    kinds = {f.kind for f in analyze(lines, [])}
    assert HealthKind.FROZEN_ZERO_TREE not in kinds


def test_skeleton_loss_warns() -> None:
    lines = ["  s0 [heal] src/error.rs: no fn was revertible — restored whole "
             "file from skeleton (7 fill(s) lost, will be refilled)."]
    finds = [f for f in analyze(lines, []) if f.kind is HealthKind.HEAL_SKELETON_LOSS]
    assert finds and "7" in finds[0].evidence
