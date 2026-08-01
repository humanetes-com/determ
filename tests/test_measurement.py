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


def test_qualified_verdicts_reconcile_a_crashed_binary() -> None:
    # The live shape on `anyhow/nightly`, at every sweep since 07-30 20:09:
    # pass_count=93 against len(passed)=94, fail_count=30 against
    # len(failed)=26. The pinned handover tells every session to rebuild score
    # rows from exactly these fields, so every delta quoted as a fact was +-3.
    from pipeline_core.domain.measurement import qualified_verdicts

    class _B:
        def __init__(self, label, passed, failed, completed):
            self.label, self.passed = label, frozenset(passed)
            self.failed, self.completed = frozenset(failed), completed

    bins = [
        _B("tests/test_chain.rs", {"test_drop", "test_iter"}, set(), True),
        # Crashed: printed `ok` for two tests, never printed a trailer. One of
        # them (`test_drop`) shares its BARE name with the surviving binary.
        _B("tests/test_downcast.rs", {"test_as_ref", "test_drop"}, set(), False),
    ]
    passed, failed, incomplete = qualified_verdicts(bins)
    # The shared bare name is two DISTINCT verdicts once qualified — which is
    # exactly what the bare-name set could never represent.
    assert ("tests/test_chain.rs", "test_drop") in passed
    assert ("tests/test_downcast.rs", "test_drop") in passed
    assert len(passed) == 4 and not failed
    assert incomplete == frozenset({"tests/test_downcast.rs"})

    # The counter saw only the completed binary's trailer: 2, not 4.
    m = Measurement(
        cluster="nightly", iteration=1, tree_sha="abc", oracle_size=4,
        pass_count=2, fail_count=2, passed=frozenset(), failed=frozenset(),
        passed_pairs=passed, failed_pairs=failed, incomplete=incomplete)
    rec = m.reconcile()
    # A gap is EXPECTED here, and it is explained rather than silently absorbed.
    assert rec.reconciled is True and rec.pass_gap == 2
    assert "trailerless" in rec.note and "tests/test_downcast.rs" in rec.note

    # With every binary complete the same gap is a real parser defect.
    m2 = Measurement(
        cluster="nightly", iteration=1, tree_sha="abc", oracle_size=4,
        pass_count=2, fail_count=2, passed=frozenset(), failed=frozenset(),
        passed_pairs=passed, failed_pairs=failed, incomplete=frozenset())
    assert m2.reconcile().reconciled is False
    assert "UNEXPLAINED" in m2.reconcile().note


def test_a_sweep_with_no_qualified_verdicts_is_not_reported_as_a_mismatch() -> None:
    # Events written before 93.6 carry no pairs. "No pairs" is UN-ASSESSABLE,
    # not a mismatch — treating it as one floods a historical replay with
    # alarms about a check that could not run.
    m = Measurement(
        cluster="stable", iteration=1, tree_sha="abc", oracle_size=121,
        pass_count=112, fail_count=9,
        passed=frozenset({"a"}), failed=frozenset())
    assert m.reconcile().reconciled is True
    assert m.reconcile().note == ""
