"""The held-out captures and the agreement reading.

The class: a tree green on its own oracle and wrong where no test of its own
looks (the 2026-08-25 comparison: 37 of 37 pipeline-authored tests green on a
tree wrong in three public behaviours). These tests pin the contract that
makes the reading a measurement: a held-out capture reaches no test, no
prompt-facing suite and no book; the split is by program text; the reading
is taken on a tree with failures left.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from pipeline_core.adapters.behaviour_store import load_behaviour_suite, save_behaviour_suite
from pipeline_core.adapters.held_out_store import (
    held_out_path,
    load_held_out,
    record_agreement_rows,
    save_held_out,
    scratch_tree,
)
from pipeline_core.adapters.oracle_fingerprint import _is_oracle_path, workspace_oracle_files
from pipeline_core.application.behaviour_agreement import AgreementDeps, judge_capture, measure_agreement
from pipeline_core.application.run_behaviour_oracle import run_behaviour_oracle
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    BehaviourVerdict,
    ObservableOutcome,
    held_out_file_name,
    suite_cluster_of,
    union_cluster_suites,
)
from pipeline_core.domain.behaviour_probe import ProbeResult
from pipeline_core.domain.held_out import (
    Agreement,
    capture_key,
    is_held_out,
    split_held_out,
)


def _probe(n: int, when: str, value: str = "x", *, unit: str = "u", cluster: str = "stable") -> BehaviourSpec:
    return BehaviourSpec(f"probe::{cluster}::{unit}::{n}", f"let v = make({n});", when,
                         ObservableOutcome.display(value), subkind=BehaviourSubKind.OUTCOME)


def _harvested(n: int) -> BehaviourSpec:
    return BehaviourSpec(f"testassert::tests/t.rs::t::{n}", "", f"f({n}).to_string()",
                         ObservableOutcome.display("x"), subkind=BehaviourSubKind.OUTCOME)


def _many(k: int = 40) -> list[BehaviourSpec]:
    return [_probe(i, f"v.render({i}).to_string()") for i in range(k)]


def test_the_split_is_by_program_text_never_by_id_and_never_takes_a_harvested_example() -> None:
    specs = _many()
    kept, held = split_held_out(specs + [_harvested(i) for i in range(40)], 25)
    assert held and all(s.behaviour_id.startswith("probe::") for s in held)
    assert 3 <= len(held) <= 20                          # a share, not all or none
    assert {s.behaviour_id for s in kept} | {s.behaviour_id for s in held} \
        == {s.behaviour_id for s in specs} | {f"testassert::tests/t.rs::t::{i}" for i in range(40)}
    # The same program under another reply order or another cluster keeps its side.
    for s in specs:
        renamed = replace(s, behaviour_id=f"probe::nightly::other::{991}")
        assert capture_key(renamed) == capture_key(s)
        assert is_held_out(renamed, 25) == is_held_out(s, 25)

    # NEGATIVE CONTROL: a split hashed on the id moves programs across it
    # when only the reply order changes.
    import hashlib

    def by_id(s: BehaviourSpec) -> bool:
        return int.from_bytes(hashlib.sha256(s.behaviour_id.encode()).digest()[:8], "big") % 100 < 25
    moved = [s for n, s in enumerate(specs)
             if by_id(s) != by_id(replace(s, behaviour_id=f"probe::stable::u::{n + 100}"))]
    assert moved
    assert split_held_out(specs, 0) == (tuple(specs), ())


def test_a_held_out_file_is_sealed_and_is_no_cluster_s_suite(tmp_path: Path) -> None:
    kept, held = split_held_out(_many(), 25)
    save_behaviour_suite(tmp_path, BehaviourSuite("stable", kept))
    save_held_out(tmp_path, BehaviourSuite("stable", held))
    # what the fix loop loads as prompt-visible behaviours: every cluster suite on disk
    names = sorted(filter(None, (suite_cluster_of(p.name) for p in (tmp_path / ".skeleton").iterdir())))
    assert names == ["stable"]
    visible = union_cluster_suites([load_behaviour_suite(tmp_path, n) for n in names])
    held_ids = {s.behaviour_id for s in held}
    assert held_ids and not held_ids & {s.behaviour_id for s in visible}
    assert suite_cluster_of(held_out_file_name("stable")) is None
    # ... and it is inside the oracle seal, under its own path
    rel = held_out_path(tmp_path, "stable").relative_to(tmp_path).as_posix()
    assert rel in workspace_oracle_files(tmp_path) and _is_oracle_path(rel)
    assert {s.behaviour_id for s in load_held_out(tmp_path, "stable").specs} == held_ids
    assert load_held_out(tmp_path, "nightly").specs == ()

    # NEGATIVE CONTROL: the same captures stored under the suite prefix (a
    # second `_behaviours_*.json`) are a cluster to that enumeration, and
    # their programs and Thens reach the prompt-visible set.
    leaked = tmp_path / ".skeleton" / "_behaviours_heldout_stable.json"
    leaked.write_text(json.dumps(BehaviourSuite("heldout_stable", held).to_dict()), encoding="utf-8")
    names = sorted(filter(None, (suite_cluster_of(p.name) for p in (tmp_path / ".skeleton").iterdir())))
    assert "heldout_stable" in names
    assert held_ids <= {s.behaviour_id
                        for s in union_cluster_suites([load_behaviour_suite(tmp_path, n) for n in names])}


def _deps(suite: BehaviourSuite, answers: dict[str, ProbeResult], logs: list[str],
          rows: list[object], ran_in: list[Path]) -> AgreementDeps:
    @contextmanager
    def scratch(tree: Path):  # type: ignore[no-untyped-def]
        ran_in.append(tree)
        yield tree / "scratch"

    def run_probe(crate: Path, cluster: object, program: str, name: str, **kw: object) -> ProbeResult:
        assert crate.name == "scratch"                  # never the measured tree itself
        return next(v for k, v in answers.items() if k in program)

    return AgreementDeps(
        load_held_out=lambda _p, _c: suite, scratch_tree=scratch, run_probe=run_probe,
        record_rows=lambda m, c, w, r: rows.extend(r), log=logs.append,
        emit=lambda *a, **k: rows.append(("event", a, k)))


def test_the_reading_counts_agree_differ_and_not_run_on_a_tree_with_failures_left(tmp_path: Path) -> None:
    held = BehaviourSuite("stable", (
        _probe(0, "a.to_string()", "same"), _probe(1, "b.to_string()", "original"),
        _probe(2, "c.to_string()", "never")))
    answers = {"a.to_string()": ProbeResult(True, "same\n", ""),
               "b.to_string()": ProbeResult(True, "reconstructed\n", ""),
               "c.to_string()": ProbeResult(False, "", "error[E0599]: no method")}
    logs: list[str] = []
    rows: list[object] = []
    ran: list[Path] = []
    reading = measure_agreement(
        _deps(held, answers, logs, rows, ran), project_dir=tmp_path, tree_dir=tmp_path / "tree",
        module="krate", cluster="stable", where="task2a", oracle_pass=98, oracle_total=105, stubs=3)
    assert reading is not None
    assert (reading.agree, reading.differ, reading.not_run, reading.held) == (1, 1, 1, 3)
    assert reading.line() == ("behaviour-agreement: 1 of 3 held-out capture(s) agree, 1 differ, "
                              "1 did not run (oracle 98/105, 3 stub(s) left)")
    assert any(reading.line() in ln for ln in logs) and ran == [tmp_path / "tree"]
    outcomes = [r.outcome for r in rows if hasattr(r, "outcome")]
    assert outcomes == [Agreement.AGREES, Agreement.DIFFERS, Agreement.NOT_RUN]
    differ = next(r for r in rows if getattr(r, "outcome", None) is Agreement.DIFFERS)
    assert (differ.expected, differ.observed) == ("original", "reconstructed")
    event = next(r for r in rows if isinstance(r, tuple))
    assert event[1] == ("behaviour.agreement",) and event[2]["oracle_pass"] == 98
    assert set(event[2]) == {"cluster", "where", "held", "agree", "differ", "not_run",
                             "oracle_pass", "oracle_total", "stubs"}       # counts only

    # NEGATIVE CONTROL: the oracle runner's cost gate, left where a failing
    # tree sets it, runs nothing — no reading on the tree the class was
    # measured on (98 pass / 7 fail).
    gated = run_behaviour_oracle(held, unit_tests_green=False,
                                 run_spec=lambda s: (BehaviourVerdict.HOLD, ""))
    assert gated.outcomes == ()


def test_no_held_out_capture_is_no_reading_and_a_failure_to_measure_is_logged(tmp_path: Path) -> None:
    logs: list[str] = []
    empty = _deps(BehaviourSuite("stable"), {}, logs, [], [])
    assert measure_agreement(empty, project_dir=tmp_path, tree_dir=tmp_path, module="k",
                             cluster="stable", where="task3") is None
    assert "no held-out capture" in logs[-1]
    broken = replace(empty, load_held_out=lambda _p, _c: 1 / 0)  # type: ignore[arg-type]
    assert measure_agreement(broken, project_dir=tmp_path, tree_dir=tmp_path, module="k",
                             cluster="stable", where="task3") is None
    assert "reading not taken" in logs[-1]
    spec = _probe(0, "a.is::<T>()")
    assert judge_capture(spec, ProbeResult(True, "  \n", ""))[0] is BehaviourVerdict.NOT_RUN


def test_the_scratch_tree_follows_links_and_leaves_build_output_behind(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "lib.rs").write_text("pub fn f() {}", encoding="utf-8")
    (tree / "target").mkdir()
    (tree / "target" / "big").write_text("x", encoding="utf-8")
    for name in (".skeleton-filled", ".git", "target-asan", ".cargo"):
        (tree / name).mkdir()
        (tree / name / "f").write_text("x", encoding="utf-8")
    oracle = tmp_path / "oracle_tests"
    oracle.mkdir()
    (oracle / "t.rs").write_text("// oracle", encoding="utf-8")
    (tree / "tests").symlink_to(oracle)
    with scratch_tree(tree) as crate:
        assert (crate / "src" / "lib.rs").is_file() and not (crate / "target").exists()
        assert not (crate / "tests").is_symlink()
        assert sorted(p.name for p in crate.iterdir()) == [".cargo", "src", "tests"]
        (crate / "tests" / "t.rs").write_text("edited", encoding="utf-8")
        (crate / "examples").mkdir()
        gone = crate
    assert (oracle / "t.rs").read_text(encoding="utf-8") == "// oracle"   # a write did not go through
    assert not gone.exists() and not (tree / "examples").exists()
    p = record_agreement_rows(tmp_path, "krate", "stable", "task3", [])
    assert p.read_text(encoding="utf-8") == "[]"
