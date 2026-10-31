"""Pins for the t0d use-case with every collaborator faked: the phase writes
the suite, marks the phase, skips a done cluster, drops a failing probe
(negative control), fills exercises, survives a probe-stage exception, and
logs the one reading line with the three defect-observable flags computed by
the same patterns the census uses."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pipeline_core.application.behaviour_book_build import BookBudget
from pipeline_core.application.extract_behaviours import harvest_behaviour_suite
from pipeline_core.application.tasks.task0d import Task0dDeps, run_task0d
from pipeline_core.domain.behaviour import BehaviourSuite, OutcomeKind
from pipeline_core.domain.behaviour_probe import ProbeResult
from pipeline_core.domain.models import BuildCluster, FnRecord

FIXTURES = Path(__file__).parent / "fixtures" / "behaviours"


def _cluster(name: str = "stable", default: bool = True) -> BuildCluster:
    return BuildCluster(
        name=name,
        env_extra={},
        tarpaulin_exclude_files=[],
        excluded_cfg=[],
        active_cfg=[],
        prompt_nightly_rule="",
        is_default=default,
    )


class _World:
    """Recorded side effects of one run."""

    def __init__(
        self,
        *,
        done: set[str] = frozenset(),
        probe_ok: bool = True,
        reply: str = "",
        exercises: dict[str, frozenset[str]] | None = None,
        probes_raise: bool = False,
    ):
        self.logs: list[str] = []
        self.events: list[tuple[str, dict[str, object]]] = []
        self.marked: list[tuple[str, str]] = []
        self.committed: list[tuple[str, str, str]] = []
        self.saved: dict[str, BehaviourSuite] = {}
        self.probes_run: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.misses: list[tuple[str, list[dict[str, str]]]] = []
        self.features: dict[str, dict[str, str]] = {}
        self.done = set(done)
        self.probe_ok = probe_ok
        self.reply = reply
        self.exercises = exercises or {}
        self.sites: dict[str, frozenset[str]] = {}
        self.probes_raise = probes_raise

    def deps(self, **over: object) -> Task0dDeps:
        tests = [
            (f"tests/{n}", (FIXTURES / n).read_text(encoding="utf-8"))
            for n in ("test_fmt.rs", "test_ensure.rs", "test_downcast.rs")
        ]

        def run_probe(
            pd: Path, cl: BuildCluster | None, program: str, name: str, extra_env: object = None, **_k: object
        ) -> ProbeResult:
            if self.probes_raise:
                raise RuntimeError("cargo exploded")
            self.probes_run.append((name, program))
            return (
                ProbeResult(True, "printed 42\n", "")
                if self.probe_ok
                else ProbeResult(False, "", "error[E0599]: no method")
            )

        def ask(prompt: str) -> str:
            self.prompts.append(prompt)
            return self.reply

        def exercises(pd: Path, cl: object, specs: object, recs: object,
                      sites_out: dict[str, frozenset[str]] | None = None) -> object:
            # The same hits by site: a spec the test maps in `self.sites`.
            if sites_out is not None:
                ids = {getattr(x, "behaviour_id", "") for x in specs}  # type: ignore[attr-defined]
                sites_out.update({k: v for k, v in self.sites.items() if k in ids})
            return self.exercises

        kw: dict[str, object] = {
            "log": self.logs.append,
            "emit": lambda ev, **f: self.events.append((ev, f)),
            "mark_phase": lambda pd, key, status, detail="": self.marked.append((key, status)),
            "phase_valid": lambda pd, key: key in self.done,
            "commit_and_mark": lambda pd, key, status, commit_message, detail="": self.committed.append(
                (key, status, commit_message)
            ),
            "read_sources": lambda sd: [
                ("lib.rs", '/// ```\n/// let e = anyhow!("x");\n/// ```\npub fn anyhow() {}\n')
            ],
            "read_test_sources": lambda pd: tests,
            "harvest": harvest_behaviour_suite,
            "save_suite": lambda pd, suite: self.saved.__setitem__(suite.cfg_cluster, suite) or pd,
            "load_suite": lambda pd, c: BehaviourSuite(cfg_cluster=c),
            "ask": ask,
            "public_surface": lambda pd, c: "pub fn unnamed_public() -> u8;",
            "symbol_inventory": lambda pd, c: "",
            "enumerate_fns": lambda sd: [
                FnRecord(file="lib.rs", fn_name="unnamed_public", is_pub=True, is_exactly_pub=True),
                FnRecord(file="lib.rs", fn_name="anyhow", is_pub=True, is_exactly_pub=True),
            ],
            "enumerate_mods": lambda sd: [],
            "enumerate_types": lambda sd: [],
            "run_probe": run_probe,
            "exercises_by_coverage": exercises,
            "record_misses": lambda pd, c, m: self.misses.append((c, list(m))),
            "tests_failing_alone": lambda *_a, **_k: (),
            "settle_test_gate": lambda *_a, **_k: "kept",
            # The book's rule calls are their own stage with their own knob;
            # these pins are about the probe stage, so the book is built
            # without sentences (no model call) unless a test asks for it.
            "BOOK_BUDGET": BookBudget(rules=False),
            "write_features": lambda pd, c, files: self.features.__setitem__(c, dict(files)),
        }
        kw.update(over)
        return Task0dDeps(**kw)  # type: ignore[arg-type]


# The constrained shape the synthesis prompt now asks for: setup statements, a
# single expression, and the render kind. The harness prints the expression to
# capture and asserts it to replay, from these same three parts.
_REPLY = (
    '<BEHAVIOUR name="unnamed_public">\n'
    "<GIVEN>\n    let v = crate_x::unnamed_public();\n</GIVEN>\n"
    "<WHEN>v</WHEN>\n<OBSERVE>debug</OBSERVE>\n</BEHAVIOUR>"
)


def test_phase_writes_suite_marks_done_and_logs_reading_with_three_flags(tmp_path: Path) -> None:
    w = _World(
        reply=_REPLY, exercises={"testassert::tests/test_fmt.rs::test_altdebug::0": frozenset({"src/fmt.rs::fmt"})}
    )
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    suite = w.saved["stable"]
    assert suite.specs and w.committed == [
        ("task0d/stable", "done", f"pipeline: task0d/stable — {len(suite.specs)} behaviour(s)"),
        ("task0d/anyhow/book", "done",
         "pipeline: task0d/anyhow/book — 1 world(s) booked (stable), rule probes on none"),
    ]
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
    # TWO runs per admitted probe, not one: the capture is re-run and the output
    # must match. A single run cannot tell a deterministic observable from a
    # lucky one, and a Then that does not reproduce becomes a flaky test in the
    # oracle for the life of the project. (The fake exercises map is not cargo.)
    assert ev["cargo_invocations"] == 2
    probe = next(s for s in suite.specs if s.behaviour_id.startswith("probe::stable::unnamed_public::"))
    # The constrained capture stores a TYPED kind, not a stdout blob: that is
    # what keeps `classified()` from returning OTHER, which t1.5's cap discards
    # for ranking last and which no generated test can pin.
    assert probe.then.kind is OutcomeKind.DEBUG_STRING
    assert probe.then.value == "printed 42"
    filled = next(s for s in suite.specs if s.behaviour_id == "testassert::tests/test_fmt.rs::test_altdebug::0")
    assert filled.exercises == frozenset({"src/fmt.rs::fmt"})
    # the probe targeted only the public fn no spec names (`anyhow` is named by the doc-test)
    # The request is now by CLASS — behaviours are transversal, so a unit-shaped
    # ask cannot reach one — but the unit that no spec names is still offered as
    # a callable name, because a class says what to observe, not what to call.
    assert "unnamed_public" in w.prompts[0]
    assert "Public units available to call" in w.prompts[0]
    assert "write ONE behaviour for each of these classes" in w.prompts[0]


def test_done_cluster_and_done_book_stage_are_skipped(tmp_path: Path) -> None:
    w = _World(done={"task0d/stable", "task0d/anyhow/book"})
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert not w.saved and not w.committed and not w.marked and not w.prompts
    assert any("already harvested" in ln for ln in w.logs)
    assert any("book stage already done" in ln for ln in w.logs)


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
    run_task0d(
        w.deps(EXERCISES_ENABLED=False, PROBE_MAX_PER_CLUSTER=0), tmp_path, "anyhow", [_cluster()], tmp_path / "src"
    )
    line = next(ln for ln in w.logs if "behaviours —" in ln)
    assert "exercises filled: 0/" in line and "probes admitted 0 / proposed 0" in line
    assert not w.prompts


def test_harvest_failure_leaves_the_phase_open(tmp_path: Path) -> None:
    w = _World()

    def boom(*a: object, **k: object) -> BehaviourSuite:
        raise RuntimeError("bad source")

    run_task0d(w.deps(harvest=boom), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert w.marked == [("task0d/stable", "failed")]
    # Nothing on disk to book: the stage is done with nothing, and the tag
    # plan still refuses on the failed harvest.
    assert w.committed == [("task0d/anyhow/book", "done", "pipeline: task0d/anyhow/book — no suite to book")]


def test_each_cluster_gets_its_own_suite(tmp_path: Path) -> None:
    w = _World()
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster("stable"), _cluster("nightly", default=False)], tmp_path / "src")
    assert set(w.saved) == {"stable", "nightly"}
    assert [c[0] for c in w.committed] == [
        "task0d/stable", "task0d/nightly", "task0d/anyhow/book"]
    s = w.saved["nightly"].specs[0]
    assert replace(s, cfg_cluster="stable") != s and s.cfg_cluster == "nightly"


# --------------------------------------------------------------------------
# The class axis is the REQUEST, and the unit axis is no longer the on-switch.
# --------------------------------------------------------------------------


def _named_fn(name: str) -> FnRecord:
    return FnRecord(file="lib.rs", fn_name=name, is_pub=True, is_exactly_pub=True)


def test_probes_still_ask_by_class_when_no_public_unit_is_left_over(tmp_path: Path) -> None:
    """`probe_targets` keeps public fns no harvested spec names. Returning
    early when that set is empty left the UNIT axis as the on-switch of the
    mechanism built to leave it: a crate whose harvest already names every
    public fn — harvest SUCCEEDING — would never ask by class at all."""
    w = _World(reply=_REPLY)
    # `anyhow` is named by the doc-test the fixture harvests, so nothing is
    # left over on the unit axis.
    run_task0d(
        w.deps(enumerate_fns=lambda sd: [_named_fn("anyhow")]), tmp_path, "anyhow", [_cluster()], tmp_path / "src"
    )
    assert len(w.prompts) == 1
    assert "write ONE behaviour for each of these classes" in w.prompts[0]
    assert w.saved["stable"].specs


def test_the_probe_budget_at_zero_is_still_the_kill_switch(tmp_path: Path) -> None:
    """NEGATIVE CONTROL for the test above: `TASK0D_PROBE_MAX=0` documents
    "no probes", and asking by class must not smuggle a model call past it."""
    w = _World(reply=_REPLY)
    run_task0d(
        w.deps(PROBE_MAX_PER_CLUSTER=0, enumerate_fns=lambda sd: [_named_fn("anyhow")]),
        tmp_path,
        "anyhow",
        [_cluster()],
        tmp_path / "src",
    )
    assert w.prompts == [] and w.probes_run == []


_DISPLAY_REPLY = (
    '<BEHAVIOUR name="unnamed_public">\n'
    "<GIVEN>\n    let v = crate_x::unnamed_public();\n</GIVEN>\n"
    "<WHEN>v</WHEN>\n<OBSERVE>display</OBSERVE>\n</BEHAVIOUR>"
)


def test_pin_env_debug_off_admits_display_and_does_not_recapture(tmp_path: Path) -> None:
    """BEHAVIOUR_PIN_ENV_DEBUG=0 restores the pre-2026-09-02 contract: harvest
    Display covers EXACT_FORMAT so the Debug-only miss does not fire, and the
    emitter is not given a recapture so a mover is still withdrawn."""
    seen: dict[str, object] = {}

    def emit_tests(*_a: object, **kw: object) -> tuple[object, tuple[str, ...], list[dict[str, str]], tuple[object, ...]]:
        seen.update(kw)
        return None, (), [], ()

    w = _World(reply=_DISPLAY_REPLY)
    run_task0d(
        w.deps(PIN_ENV_DEBUG=False, emit_tests=emit_tests),
        tmp_path,
        "anyhow",
        [_cluster()],
        tmp_path / "src",
    )
    assert seen.get("recapture") is None
    assert w.probes_run  # Display was not refused before cargo
    reasons = [m["reason"] for _c, ms in w.misses for m in ms]
    assert not any("EXACT_FORMAT ask requires" in r for r in reasons)
    assert "never Display" not in w.prompts[0]


def test_pin_env_debug_on_passes_recapture(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def emit_tests(*_a: object, **kw: object) -> tuple[object, tuple[str, ...], list[dict[str, str]], tuple[object, ...]]:
        seen.update(kw)
        return None, (), [], ()

    w = _World(reply=_REPLY)
    run_task0d(w.deps(emit_tests=emit_tests), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert seen.get("recapture") is not None
    assert callable(seen["recapture"])


def test_the_recapture_builds_the_original_the_way_it_is_asked(tmp_path: Path) -> None:
    """The emitter asks for one recapture per build of the original; the
    closure task 0d hands it passes that build and the perturbed env to the
    probe runner, so the no-symbols build is the one that runs."""
    import dataclasses

    from pipeline_core.domain.behaviour_probe import CapturedProbe, ProbeBuild, captured_spec

    seen: dict[str, object] = {}
    runs: list[tuple[object, object]] = []

    def emit_tests(*_a: object, **kw: object) -> tuple[object, tuple[str, ...], list[dict[str, str]], tuple[object, ...]]:
        seen.update(kw)
        return (), (), [], ()

    def run_probe(pd: Path, cl: object, program: str, name: str, **kw: object) -> ProbeResult:
        runs.append((kw.get("build"), kw.get("extra_env")))
        return ProbeResult(True, "printed 42\n", "")

    w = _World(reply=_REPLY)
    deps = dataclasses.replace(w.deps(emit_tests=emit_tests), run_probe=run_probe)
    run_task0d(deps, tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    recapture = seen["recapture"]
    assert callable(recapture)
    one = captured_spec(cfg_cluster="stable", probe=CapturedProbe(
        name="p", given="", when="anyhow::Error::msg(\"x\")", observe="debug"),
        n=0, stdout="x", default_cluster="stable")
    runs.clear()
    assert recapture(one, {"RUST_BACKTRACE": "1"}, ProbeBuild.WITHOUT_SYMBOLS) == "printed 42\n"
    assert runs == [(ProbeBuild.WITHOUT_SYMBOLS, {"RUST_BACKTRACE": "1"})]


def test_two_worlds_share_one_probe_ask_and_each_runs_its_own_cargo(tmp_path: Path) -> None:
    """One synthesis prompt over both worlds. Cargo, and the stamp on the
    suite, stay that world's. Identical surface text is not marked."""
    w = _World(reply=_REPLY)
    run_task0d(
        w.deps(), tmp_path, "anyhow",
        [_cluster("stable"), _cluster("nightly", default=False)], tmp_path / "src")
    synthesis = [p for p in w.prompts if "write ONE behaviour" in p]
    assert len(synthesis) == 1
    assert "Cluster: stable+nightly" in synthesis[0]
    assert "unnamed_public" in synthesis[0]
    assert "// stable" not in synthesis[0] and "// nightly" not in synthesis[0]
    names = [n for n, _prog in w.probes_run]
    assert any(n.startswith("stable_") for n in names)
    assert any(n.startswith("nightly_") for n in names)
    assert w.saved["stable"].cfg_cluster == "stable"
    assert w.saved["nightly"].cfg_cluster == "nightly"
    assert all(s.cfg_cluster == "nightly" for s in w.saved["nightly"].specs)


def test_worlds_whose_surface_differs_are_marked_in_the_one_ask(tmp_path: Path) -> None:
    w = _World(reply=_REPLY)

    def surface(_pd: Path, cluster: str) -> str:
        if cluster == "nightly":
            return "pub fn unnamed_public() -> u8;\npub fn only_nightly() -> u8;"
        return "pub fn unnamed_public() -> u8;"

    run_task0d(
        w.deps(public_surface=surface), tmp_path, "anyhow",
        [_cluster("stable"), _cluster("nightly", default=False)], tmp_path / "src")
    synthesis = [p for p in w.prompts if "write ONE behaviour" in p]
    assert len(synthesis) == 1
    assert "// stable\n" in synthesis[0] and "// nightly\n" in synthesis[0]
    assert "only_nightly" in synthesis[0]


def test_a_copied_stable_stamp_is_not_a_nightly_cfg_world() -> None:
    """The cfg-world class reads the stamp. The same example text stamped
    with the default cluster is not that class, so copying one world's suite
    into the other's file would drop the class."""
    from pipeline_core.domain.behaviour import BehaviourClass, BehaviourSpec, classified

    spec = BehaviourSpec(
        behaviour_id="testassert::tests/gate.rs::gated::0",
        setup='#[cfg(feature = "extra")]\nlet v = 1;',
        when="v",
        cfg_cluster="nightly",
    )
    assert classified(spec, default_cluster="stable").klass is BehaviourClass.CFG_WORLD
    copied = classified(replace(spec, cfg_cluster="stable"), default_cluster="stable")
    assert copied.klass is not BehaviourClass.CFG_WORLD


def test_the_class_asks_are_sent_once_not_once_per_unit_batch(tmp_path: Path) -> None:
    """The same four class asks went out with every leftover-unit batch: at a
    cap of 12 and a batch of 6 that was two identical requests and twice the
    GPU for one cohort."""
    from pipeline_core.domain.behaviour_probe import PROBE_BATCH_SIZE, batches

    many = [_named_fn(f"public_{i}") for i in range(12)]
    w = _World(reply=_REPLY)
    run_task0d(w.deps(enumerate_fns=lambda sd: many), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert len(list(batches(tuple(f"public_{i}" for i in range(12)), PROBE_BATCH_SIZE))) == 2  # what the old loop sent
    assert len(w.prompts) == 1  # what it sends now
    assert "public_0" in w.prompts[0] and "public_11" in w.prompts[0]


# --------------------------------------------------------------------------
# A reply that produces nothing is a MISS, not silence.
# --------------------------------------------------------------------------

_LEGACY_REPLY = (
    '<BEHAVIOUR unit="unnamed_public">\nfn main() { println!("{}", crate_x::unnamed_public()); }\n</BEHAVIOUR>'
)


def test_a_reply_in_the_old_shape_is_recorded_rather_than_dropped(tmp_path: Path) -> None:
    """No <WHEN> means no block, and `proposed` simply stayed 0 — no log, no
    event, no miss. That is the answer-dropped class: the model did answer."""
    from pipeline_core.domain.behaviour_probe import parse_behaviour_specs

    assert parse_behaviour_specs(_LEGACY_REPLY) == []  # nothing else notices
    w = _World(reply=_LEGACY_REPLY)
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    reasons = [m["reason"] for _c, ms in w.misses for m in ms]
    assert reasons == ["reply arrived in the legacy free-form shape (no <WHEN>)"]
    assert w.probes_run == []


def test_an_unparseable_reply_is_recorded_with_its_own_reason(tmp_path: Path) -> None:
    w = _World(reply="I would rather not.")
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    reasons = [m["reason"] for _c, ms in w.misses for m in ms]
    assert reasons == ["reply carried no parseable <BEHAVIOUR> block"]


def test_the_miss_record_is_written_even_when_nothing_was_missed(tmp_path: Path) -> None:
    """A miss file states what THIS pass could not capture. Writing it only
    when the list is non-empty leaves the previous pass's file standing as if
    it were current."""
    w = _World(reply=_REPLY)
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert w.misses == [("stable", [])]


def test_a_probe_that_fails_to_build_is_not_re_run_for_a_reproducibility_check(tmp_path: Path) -> None:
    """Re-running a program that failed to COMPILE pays the most expensive
    cargo call there is for an answer already known."""
    w = _World(reply=_REPLY, probe_ok=False)
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert len(w.probes_run) == 1
    ev = dict(w.events)["task0d.harvest"]
    assert ev["cargo_invocations"] == 1
    assert [m["reason"][:6] for _c, ms in w.misses for m in ms] == ["failed"]


def test_blocks_past_the_per_reply_cap_are_recorded_not_silently_sliced_off(tmp_path: Path) -> None:
    """The cap bounds the WORK — every block costs its own compile-and-run —
    but a block proposed and never run is still something the model offered
    and the pipeline dropped."""
    block = (
        '<BEHAVIOUR name="b{i}">\n<GIVEN>\n    let v = 1;\n</GIVEN>\n'
        "<WHEN>v</WHEN>\n<OBSERVE>debug</OBSERVE>\n</BEHAVIOUR>"
    )
    w = _World(reply="\n".join(block.format(i=i) for i in range(5)))
    run_task0d(w.deps(PROBE_BLOCKS_PER_REPLY=2), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert len(w.probes_run) == 4  # 2 blocks x (capture + confirm)
    reasons = [m["reason"] for _c, ms in w.misses for m in ms]
    assert reasons == ["over the blocks-per-reply cap: 3 block(s) past 2 not run"]


def test_a_capture_the_emitter_withdraws_leaves_the_suite(tmp_path: Path) -> None:
    """A value that cannot be replayed is not a specification. Leaving it in
    the suite would hand t1.5 a MUST no tree can satisfy — the same silent
    divergence the capture route exists to remove, from the other side."""
    w = _World(reply=_REPLY)

    def emit_tests(pd, cl, specs, *, crate, **kw):
        probe = next(s for s in specs if s.behaviour_id.startswith("probe::"))
        return None, (probe.behaviour_id,), [{"unit": probe.behaviour_id, "reason": "environment-dependent"}], ()

    run_task0d(w.deps(emit_tests=emit_tests), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert not [s for s in w.saved["stable"].specs if s.behaviour_id.startswith("probe::")]
    assert dict(w.events)["task0d.harvest"]["probes_admitted"] == 0
    assert [m["reason"] for _c, ms in w.misses for m in ms] == ["environment-dependent"]


def test_an_emitter_that_raises_is_non_fatal_and_the_suite_is_still_saved(tmp_path: Path) -> None:
    def boom(*_a: object, **_k: object) -> tuple[object, tuple[str, ...], list[object]]:
        raise RuntimeError("cargo exploded")

    w = _World(reply=_REPLY)
    run_task0d(w.deps(emit_tests=boom), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert w.saved["stable"].specs
    assert any("behaviour-test emission failed" in m for m in w.logs)
    assert w.committed and w.committed[0][1] == "done"


def test_a_held_out_capture_reaches_no_emitted_test_no_saved_suite_and_no_book(tmp_path: Path) -> None:
    """The split is applied when the capture is taken: before exercises,
    emission and the book, so nothing downstream receives it."""
    w = _World(reply=_REPLY)
    held: dict[str, BehaviourSuite] = {}
    emitted: list[tuple[str, ...]] = []

    def emit_tests(pd: Path, c: str, specs: object, **_k: object) -> tuple[None, tuple[str, ...], list[dict[str, str]], tuple[()]]:
        emitted.append(tuple(s.behaviour_id for s in specs))
        return None, (), [], ()

    over: dict[str, object] = {
        "HELD_OUT_SHARE": 100,
        "save_held_out": lambda pd, suite: held.__setitem__(suite.cfg_cluster, suite),
        "emit_tests": emit_tests,
    }
    run_task0d(w.deps(**over), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    probe_ids = {s.behaviour_id for s in held["stable"].specs}
    assert probe_ids and all(i.startswith("probe::stable::") for i in probe_ids)
    suite = w.saved["stable"]
    assert not probe_ids & {s.behaviour_id for s in suite.specs}
    assert not probe_ids & {i for ids in emitted for i in ids}
    assert not probe_ids & {i for f in suite.features for i in f.example_ids}
    assert any("held out of the suite" in ln for ln in w.logs)
    assert dict(w.events)["task0d.held_out"] == {"cluster": "stable", "held": 1, "share": 100}

    # NEGATIVE CONTROL: share 0 (the pipeline before the split) emits the capture.
    w0 = _World(reply=_REPLY)
    emitted.clear()
    run_task0d(w0.deps(emit_tests=emit_tests), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert probe_ids <= {i for ids in emitted for i in ids}
    assert probe_ids <= {s.behaviour_id for s in w0.saved["stable"].specs}


# --------------------------------------------------------------------------
# The book stage has its own state key, `task0d/<module>/book`.
# --------------------------------------------------------------------------


def _on_disk(w: _World, tmp_path: Path, names: tuple[str, ...]) -> dict[str, BehaviourSuite]:
    """Each world's harvested suite, as an earlier launch saved it."""
    d = w.deps()
    return {n: d.harvest(d.read_sources(tmp_path / "src"), cfg_cluster=n,
                         test_sources=d.read_test_sources(tmp_path), default_cluster="stable")
            for n in names}


def _probed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import pipeline_core.application.tasks.task0d as t0d
    seen: list[str] = []

    def record(deps: object, pd: Path, mod: str, cluster: BuildCluster, book: BehaviourSuite,
               **_k: object) -> BehaviourSuite:
        seen.append(cluster.name)
        return book

    monkeypatch.setattr(t0d, "run_rule_probes", record)
    return seen


def test_a_launch_that_died_in_the_book_resumes_only_the_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every cluster harvested in an earlier launch, the book stage not done
    (the launch died in its 93-125 min): the book runs over the suites on
    disk, rule probes run for every world (no completed book stage covers
    any), and the stage is committed and marked. No harvest, no probe.
    Negative control: the pre-change rule (`if passed:`) built nothing."""
    w = _World(done={"task0d/stable", "task0d/nightly"})
    disk = _on_disk(w, tmp_path, ("stable", "nightly"))
    probed = _probed(monkeypatch)

    def no_harvest(*_a: object, **_k: object) -> BehaviourSuite:
        raise AssertionError("a done cluster was harvested again")

    run_task0d(w.deps(harvest=no_harvest, load_suite=lambda pd, c: disk[c], RULE_PROBES_MAX=1),
               tmp_path, "anyhow", [_cluster("stable"), _cluster("nightly", default=False)],
               tmp_path / "src")
    assert set(w.saved) == {"stable", "nightly"} and all(s.book_worlds for s in w.saved.values())
    assert probed == ["stable", "nightly"]
    assert w.committed == [("task0d/anyhow/book", "done",
                            "pipeline: task0d/anyhow/book — 2 world(s) booked (nightly+stable), "
                            "rule probes on nightly+stable")]
    assert not w.probes_run and not w.marked
    assert any("no cluster harvested in this launch" in ln for ln in w.logs)


def test_re_harvesting_a_cluster_reopens_a_done_book_stage_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The book key goes back to pending before the harvest starts, so dying
    between the harvest and the rebuilt book cannot leave the old book marked
    done over a new suite. Rule probes run for the re-harvested world only:
    the done stage already probed the other."""
    w = _World(done={"task0d/stable", "task0d/anyhow/book"})
    disk = _on_disk(w, tmp_path, ("stable",))
    probed = _probed(monkeypatch)
    run_task0d(w.deps(load_suite=lambda pd, c: disk.get(c, BehaviourSuite(cfg_cluster=c)),
                      RULE_PROBES_MAX=1),
               tmp_path, "anyhow", [_cluster("stable"), _cluster("nightly", default=False)],
               tmp_path / "src")
    assert w.marked == [("task0d/anyhow/book", "pending")]
    assert [c[0] for c in w.committed] == [
        "task0d/nightly", "task0d/anyhow/book"]
    assert probed == ["nightly"]
    # The world already harvested is not in this launch's ask.
    synthesis = [p for p in w.prompts if "write ONE behaviour" in p]
    assert len(synthesis) == 1 and "Cluster: nightly" in synthesis[0]
    assert "stable+nightly" not in synthesis[0]


def test_a_failed_book_marks_the_stage_failed_and_commits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline_core.application.tasks.task0d as t0d

    def boom(*_a: object, **_k: object) -> dict[str, BehaviourSuite]:
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(t0d, "build_book_from_source", boom)
    w = _World()
    run_task0d(w.deps(), tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    assert [c[0] for c in w.committed] == ["task0d/stable"]
    assert w.marked == [("task0d/anyhow/book", "failed")]


def _phase_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    import pipeline_core.log as plog
    from pipeline_core.domain.phase_registry import PHASE_ENTER, PHASE_EXIT

    seen: list[dict[str, object]] = []
    monkeypatch.setattr(plog, "_emit_sink", lambda event, **f: seen.append(
        {"event": event, **f}) if event in (PHASE_ENTER, PHASE_EXIT) else None)
    return seen


def _overlapping_world_scopes(events: list[dict[str, object]]) -> list[tuple[str, str]]:
    """Pairs of t0d scopes of DIFFERENT worlds that were open at once."""
    from pipeline_core.domain.phase_registry import PHASE_ENTER

    open_now: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for e in events:
        if e.get("phase") != "t0d" or not e.get("cluster"):
            continue
        sid, cl = str(e["scope"]), str(e["cluster"])
        if e["event"] == PHASE_ENTER:
            pairs += [(other, cl) for other in open_now.values() if other != cl]
            open_now[sid] = cl
        else:
            open_now.pop(sid, None)
    return pairs


def test_each_world_is_committed_before_the_next_world_runs_cargo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A world's probes, exercises, reach and emission run and its key is
    committed before the next world's cargo starts, and no two worlds'
    scopes are open at once, so a process that dies in nightly's hours keeps
    stable, and each world's step times are its own. The probe ask stays one
    prompt over both worlds."""
    events = _phase_events(monkeypatch)
    timeline: list[str] = []
    w = _World(reply=_REPLY)

    def exercises(pd: Path, cl: BuildCluster, specs: object, recs: object,
                  sites_out: object = None) -> object:
        timeline.append(f"exercises {cl.name}")
        return {}

    def commit(pd: Path, key: str, status: str, commit_message: str, detail: str = "") -> None:
        timeline.append(f"commit {key}")
        w.committed.append((key, status, commit_message))

    run_task0d(w.deps(exercises_by_coverage=exercises, commit_and_mark=commit),
               tmp_path, "anyhow", [_cluster("stable"), _cluster("nightly", default=False)],
               tmp_path / "src")
    assert timeline[:4] == ["exercises stable", "commit task0d/stable",
                            "exercises nightly", "commit task0d/nightly"]
    assert _overlapping_world_scopes(events) == []
    synthesis = [p for p in w.prompts if "write ONE behaviour" in p]
    assert len(synthesis) == 1 and "Cluster: stable+nightly" in synthesis[0]
    # The shared ask sits under the module's scope, not under a world's.
    probe_steps = [e for e in events if e.get("phase") == "t0d.probes"
                   and e["event"] == "phase.enter"]
    assert [e.get("cluster") for e in probe_steps] == [None, "stable", "nightly"]


def test_negative_control_the_interleaved_schedule_overlaps() -> None:
    """The schedule the one-ask commit shipped with (both worlds' scopes
    opened at harvest, each step called world after world) is what the
    overlap reader flags."""
    from pipeline_core.domain.phase_registry import PHASE_ENTER, PHASE_EXIT

    interleaved = [
        {"event": PHASE_ENTER, "phase": "t0d", "cluster": "stable", "scope": "1"},
        {"event": PHASE_ENTER, "phase": "t0d", "cluster": "nightly", "scope": "2"},
        {"event": PHASE_EXIT, "phase": "t0d", "cluster": "stable", "scope": "1"},
        {"event": PHASE_EXIT, "phase": "t0d", "cluster": "nightly", "scope": "2"},
    ]
    assert _overlapping_world_scopes(interleaved) == [("stable", "nightly")]


def test_an_exact_format_refusal_applies_only_to_a_round_that_asked_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A world that asked EXACT_FORMAT refuses a format-less block in the
    round that asked it, and not in its composition round, which asks no
    class. Reading the world's whole class set refused both."""
    import pipeline_core.application.tasks.task0d as t0d
    from pipeline_core.domain.behaviour_probe import CapturedProbe
    from pipeline_core.domain.use_cases import UseCaseEdge

    bare = CapturedProbe(name="unnamed_public", given="let v = crate_x::unnamed_public();",
                         when="v", observe="")
    monkeypatch.setattr(t0d, "parse_behaviour_specs", lambda _r: [bare])
    monkeypatch.setattr(t0d, "other_observes", lambda _p: ())
    w = _World()
    deps = w.deps(PIN_ENV_DEBUG=True)
    edge = UseCaseEdge(consumer="b", producer="a", via="T")
    plan = t0d._ProbePlan(_cluster(), (), (("EXACT_FORMAT", "ask"),), (edge,), {},
                          "", "", "", (1, 1))
    class_round = t0d._AskedProbe((("EXACT_FORMAT", "ask"),), (), (), "r", frozenset({"stable"}))
    edge_round = t0d._AskedProbe((), (), (edge,), "r", frozenset({"stable"}))

    def refused(rounds: list[object]) -> int:
        _a, _n, _p, _c, misses, _e = t0d._admit_probe_rounds(
            deps, tmp_path, "anyhow", plan, rounds, "stable")  # type: ignore[arg-type]
        return sum(1 for m in misses if "EXACT_FORMAT ask requires" in m["reason"])

    assert refused([class_round]) == 1
    assert refused([edge_round]) == 0
