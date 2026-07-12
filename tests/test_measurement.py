"""F6 pins — measurement provenance VO + oracle-drift detector."""
from __future__ import annotations

from pipeline_core.domain.measurement import Measurement, tree_content_sha
from pipeline_core.domain.run_health import HealthKind, analyze


def test_measurement_to_event_carries_provenance() -> None:
    m = Measurement(
        cluster="stable", iteration=3, tree_sha="abc123",
        oracle_size=68, pass_count=22, fail_count=46,
        passed=frozenset({"t_a", "t_b"}), failed=frozenset({"t_c"}))
    e = m.to_event(kind="continue", rc=101, frontier_size=5)
    assert e["cluster"] == "stable" and e["iter"] == 3
    assert e["tree_sha"] == "abc123" and e["oracle_size"] == 68
    assert e["pass_count"] == 22 and e["fail_count"] == 46
    assert e["passed"] == ["t_a", "t_b"] and e["failed"] == ["t_c"]


def test_measurement_name_cap() -> None:
    m = Measurement("c", 1, "s", 300, 300, 0,
                    frozenset(f"t{i}" for i in range(300)), frozenset())
    e = m.to_event(kind="k", rc=0, frontier_size=0, name_cap=10)
    assert len(e["passed"]) == 10   # type: ignore[arg-type]


def test_tree_content_sha_deterministic_and_sensitive(tmp_path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text("fn a() {}", encoding="utf-8")
    h1 = tree_content_sha(src)
    assert h1 == tree_content_sha(src)          # deterministic
    (src / "a.rs").write_text("fn a() { 1 }", encoding="utf-8")
    assert tree_content_sha(src) != h1          # content-sensitive


def test_tree_content_sha_missing_is_sentinel(tmp_path) -> None:
    assert tree_content_sha(tmp_path / "nope") == "0" * 16


def _iter(n, p, f, oracle) -> str:
    return (f"  [phase-c/tdd] iter {n}: cargo test rc=101 pass={p} fail={f} "
            f"oracle={oracle} (improved=False, stall=0/6)")


def test_oracle_drift_warns() -> None:
    lines = [_iter(1, 22, 39, 61), _iter(2, 14, 54, 68)]
    finds = [x for x in analyze(lines, []) if x.kind is HealthKind.ORACLE_DRIFT]
    assert finds and "61" in finds[0].evidence and "68" in finds[0].evidence


def test_no_oracle_drift_when_stable() -> None:
    lines = [_iter(1, 20, 48, 68), _iter(2, 22, 46, 68)]
    kinds = {x.kind for x in analyze(lines, [])}
    assert HealthKind.ORACLE_DRIFT not in kinds
