"""Pins for the t0d use-case with every collaborator faked: the phase writes
the suite, marks the phase, skips a done cluster, drops a failing probe
(negative control), fills exercises, survives a probe-stage exception, and
logs the one reading line with the three defect-observable flags computed by
the same patterns the census uses."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pipeline_core.application.extract_behaviours import harvest_behaviour_suite
from pipeline_core.application.tasks.task0d import Task0dDeps, run_task0d
from pipeline_core.domain.behaviour import BehaviourSuite, OutcomeKind
from pipeline_core.domain.behaviour_probe import ProbeResult
from pipeline_core.domain.models import BuildCluster, FnRecord

FIXTURES = Path(__file__).parent / "fixtures" / "behaviours"


def _cluster(name: str = "stable", default: bool = True) -> BuildCluster:
    return BuildCluster(name=name, env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
                        is_default=default)


class _World:
    """Recorded side effects of one run."""

    def __init__(self, *, done: set[str] = frozenset(), probe_ok: bool = True,
                 reply: str = "", exercises: dict[str, frozenset[str]] | None = None,
                 probes_raise: bool = False):
        self.logs: list[str] = []
        self.events: list[tuple[str, dict[str, object]]] = []
        self.marked: list[tuple[str, str]] = []
        self.committed: list[tuple[str, str, str]] = []
        self.saved: dict[str, BehaviourSuite] = {}
        self.probes_run: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.done = set(done)
        self.probe_ok = probe_ok
        self.reply = reply
        self.exercises = exercises or {}
        self.probes_raise = probes_raise

    def deps(self, **over: object) -> Task0dDeps:
        tests = [(f"tests/{n}", (FIXTURES / n).read_text(encoding="utf-8"))
                 for n in ("test_fmt.rs", "test_ensure.rs", "test_downcast.rs")]

        def run_probe(pd: Path, cl: BuildCluster | None, program: str, name: str) -> ProbeResult:
            if self.probes_raise:
                raise RuntimeError("cargo exploded")
            self.probes_run.append((name, program))
            return (ProbeResult(True, "printed 42\n", "") if self.probe_ok
                    else ProbeResult(False, "", "error[E0599]: no method"))

        def ask(prompt: str) -> str:
            self.prompts.append(prompt)
            return self.reply

        kw: dict[str, object] = {
            "log": self.logs.append,
            "emit": lambda ev, **f: self.events.append((ev, f)),
            "mark_phase": lambda pd, key, status, detail="": self.marked.append((key, status)),
            "phase_valid": lambda pd, key: key in self.done,
            "commit_and_mark": lambda pd, key, status, commit_message, detail="":
                self.committed.append((key, status, commit_message)),
            "read_sources": lambda sd: [
                ("lib.rs", "/// ```\n/// let e = anyhow!(\"x\");\n/// ```\npub fn anyhow() {}\n")],
            "read_test_sources": lambda pd: tests,
            "harvest": harvest_behaviour_suite,
            "save_suite": lambda pd, suite: self.saved.__setitem__(suite.cfg_cluster, suite) or pd,
            "load_suite": lambda pd, c: BehaviourSuite(cfg_cluster=c),
            "ask": ask,
            "public_surface": lambda pd, c: "pub fn unnamed_public() -> u8;",
            "symbol_inventory": lambda pd, c: "",
            "enumerate_fns": lambda sd: [
                FnRecord(file="lib.rs", fn_name="unnamed_public", is_pub=True, is_exactly_pub=True),
                FnRecord(file="lib.rs", fn_name="anyhow", is_pub=True, is_exactly_pub=True)],
            "enumerate_mods": lambda sd: [],
            "enumerate_types": lambda sd: [],
            "run_probe": run_probe,
            "exercises_by_coverage": lambda pd, cl, specs, recs: self.exercises,
        }
        kw.update(over)
        return Task0dDeps(**kw)  # type: ignore[arg-type]


_REPLY = ('<BEHAVIOUR unit="unnamed_public">\nfn main() { println!("{}", '
          'crate_x::unnamed_public()); }\n</BEHAVIOUR>')


def test_phase_writes_suite_marks_done_and_logs_reading_with_three_flags(tmp_path: Path) -> None:
    w = _World(reply=_REPLY, exercises={
        "testassert::tests/test_fmt.rs::test_altdebug::0": frozenset({"src/fmt.rs::fmt"})})
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    suite = w.saved["stable"]
    assert suite.specs and w.committed == [
        ("task0d/stable", "done", f"pipeline: task0d/stable — {len(suite.specs)} behaviour(s)")]
    reading = [ln for ln in w.logs if ln.startswith("  [task0d] cluster stable:") and "behaviours —" in ln]
    assert len(reading) == 1
    line = reading[0]
    assert "defect observables: identity yes, alt-debug yes, ensure-message yes" in line
    assert "probes admitted 1 / proposed 1" in line
    assert "exercises filled: 1/" in line
    assert "elapsed " in line and "cargo invocation(s)" in line
    assert "runtime then:" in line and "ALT_DEBUG_STRING" in line and "TYPE_IDENTITY" in line
    ev = dict(w.events)["task0d.harvest"]
    assert ev["defect_observables"] == {"identity": True, "alt-debug": True, "ensure-message": True}
    assert ev["probes_admitted"] == 1 and ev["exercises_filled"] == 1
    assert ev["cargo_invocations"] == 1  # one probe; the fake exercises map is not cargo
    probe = next(s for s in suite.specs if s.behaviour_id.startswith("probe::stable::unnamed_public::"))
    assert probe.then.kind is OutcomeKind.NORMALIZED_STDOUT and probe.then.value == "printed 42"
    filled = next(s for s in suite.specs if s.behaviour_id == "testassert::tests/test_fmt.rs::test_altdebug::0")
    assert filled.exercises == frozenset({"src/fmt.rs::fmt"})
    # the probe targeted only the public fn no spec names (`anyhow` is named by the doc-test)
    assert "unnamed_public" in w.prompts[0] and "exercise these units through PUBLIC API only: unnamed_public" in w.prompts[0]


def test_done_cluster_is_skipped(tmp_path: Path) -> None:
    w = _World(done={"task0d/stable"})
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert not w.saved and not w.committed and not w.prompts
    assert any("already harvested" in ln for ln in w.logs)


def test_failing_probe_is_dropped_negative_control(tmp_path: Path) -> None:
    w = _World(reply=_REPLY, probe_ok=False)
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    suite = w.saved["stable"]
    assert not any(s.behaviour_id.startswith("probe::") for s in suite.specs)
    assert w.probes_run, "the probe was proposed and run"
    line = next(ln for ln in w.logs if "behaviours —" in ln)
    assert "probes admitted 0 / proposed 1" in line
    assert any("dropped (failed: error[E0599]" in ln for ln in w.logs)
    assert w.committed and w.committed[0][1] == "done"


def test_probe_exception_keeps_the_harvested_suite(tmp_path: Path) -> None:
    w = _World(reply=_REPLY, probes_raise=True)
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert w.saved["stable"].specs and w.committed[0][1] == "done"
    assert any("probes failed (non-fatal" in ln and "cargo exploded" in ln for ln in w.logs)


def test_exercises_disabled_and_no_probe_budget(tmp_path: Path) -> None:
    w = _World(reply=_REPLY, exercises={"x": frozenset({"y"})})
    run_task0d(w.deps(EXERCISES_ENABLED=False, PROBE_MAX_PER_CLUSTER=0),
               tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    line = next(ln for ln in w.logs if "behaviours —" in ln)
    assert "exercises filled: 0/" in line and "probes admitted 0 / proposed 0" in line
    assert not w.prompts


def test_harvest_failure_leaves_the_phase_open(tmp_path: Path) -> None:
    w = _World()

    def boom(*a: object, **k: object) -> BehaviourSuite:
        raise RuntimeError("bad source")

    run_task0d(w.deps(harvest=boom), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert not w.committed and w.marked == [("task0d/stable", "failed")]


def test_each_cluster_gets_its_own_suite(tmp_path: Path) -> None:
    w = _World()
    run_task0d(w.deps(), tmp_path, "anyhow",
               [_cluster("stable"), _cluster("nightly", default=False)], tmp_path / "src")
    assert set(w.saved) == {"stable", "nightly"}
    assert [c[0] for c in w.committed] == ["task0d/stable", "task0d/nightly"]
    s = w.saved["nightly"].specs[0]
    assert replace(s, cfg_cluster="stable") != s and s.cfg_cluster == "nightly"
