"""A test run of the one-tree loop answers for the crate in every build world.

Run `1680589-7a6824f8` (2026-10-19): task 2a filled one tree per cluster and
the two came out byte-identical; the per-cluster loop measured each tree in
one world and a merged tree was judged in every world only at task 3. The
one-tree loop instead runs every test command under each world, joined into
one capture (`cargo_runner.PortBoundCargoRunner.run`), and reads the
original crate's floor per world (`world_floor.judge_every_world`). Invented
clusters and captures; with one world everything is byte-identical to the
pre-change reading, which is each test's control.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pipeline_core.adapters.cargo import Process
from pipeline_core.application.reconstruct.cargo_runner import (
    PortBoundCargoRunner,
    WorldChecks,
)
from pipeline_core.domain.cfg import cluster_cfg_set
from pipeline_core.domain.cluster_env import (
    ALL_WORLDS_VAR,
    WORLD_VAR,
    judges_every_world,
    overlay_cluster_env,
    world_key,
)
from pipeline_core.domain.cluster_plan import one_tree_cluster
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.reference_floor import (
    ClusterReference,
    ReferenceMeasurement,
    judge_cluster_binaries,
)
from pipeline_core.domain.test_results import (
    join_capture,
    join_world_captures,
    parse_binary_results,
    parse_failed_tests,
    parse_test_counts,
    world_sections,
)
from pipeline_core.domain.world_floor import failed_pairs, judge_every_world


def _world(name: str, *, default: bool, active: list[str], excluded: list[str],
           flags: str = "", nightly: bool = False) -> BuildCluster:
    return BuildCluster(
        name=name, env_extra={"RUSTFLAGS": flags} if flags else {},
        tarpaulin_exclude_files=[], excluded_cfg=excluded, active_cfg=active,
        prompt_nightly_rule="", is_default=default, requires_nightly=nightly)


BASE = _world("base", default=True, active=[], excluded=["alt"])
ALT = _world("alt", default=False, active=["alt"], excluded=[],
             flags="--cfg alt", nightly=True)


def _capture(binaries: dict[str, dict[str, bool]]) -> str:
    """A `cargo test` capture: libtest's sections on stdout, cargo's
    `Running` lines on stderr, one binary per entry, in order."""
    out: list[str] = []
    err: list[str] = []
    for label, tests in binaries.items():
        stem = Path(label).stem
        err.append(f"     Running {label} (target/debug/deps/{stem}-0123456789abcdef)")
        out.append(f"\nrunning {len(tests)} test" + ("s" if len(tests) != 1 else ""))
        for name, ok in tests.items():
            out.append(f"test {name} ... {'ok' if ok else 'FAILED'}")
        failed = [n for n, ok in tests.items() if not ok]
        if failed:
            out.append("\nfailures:\n")
            for n in failed:
                out.append(f"---- {n} stdout ----\n\nthread '{n}' panicked at "
                           f"tests/{stem}.rs:3:5:\nboom\n")
            out.append("failures:\n" + "".join(f"    {n}\n" for n in failed))
        passed = len(tests) - len(failed)
        out.append(f"test result: {'FAILED' if failed else 'ok'}. {passed} passed; "
                   f"{len(failed)} failed; 0 ignored; 0 measured; 0 filtered out; "
                   "finished in 0.00s\n")
    return join_capture("\n".join(out), "\n".join(err))


BASE_RUN = _capture({"tests/a.rs": {"t_shared": True, "t_base": True}})
ALT_RUN = _capture({"tests/a.rs": {"t_shared": True},
                    "tests/compiletest.rs": {"ui": False}})


# --- the joined capture ---------------------------------------------------


def test_one_world_is_the_capture_unchanged() -> None:
    assert join_world_captures([("base", BASE_RUN)]) == BASE_RUN
    assert world_sections(BASE_RUN) == {"": BASE_RUN}


def test_two_worlds_sum_their_verdicts_and_split_back() -> None:
    joined = join_world_captures([("base", BASE_RUN), ("alt", ALT_RUN)])
    assert parse_test_counts(joined)[:2] == (3, 1)
    assert set(parse_failed_tests(joined)) == {"ui"}
    sections = world_sections(joined)
    assert sorted(sections) == ["alt", "base"]
    assert parse_test_counts(sections["base"])[:2] == (2, 0)
    assert parse_test_counts(sections["alt"])[:2] == (1, 1)
    # Each world's binaries pair with their own libtest sections.
    alt_bins = {b.label: b for b in parse_binary_results(sections["alt"])}
    assert "ui" in alt_bins["tests/compiletest.rs"].failed
    labels = [b.label for b in parse_binary_results(joined)]
    assert labels == ["tests/a.rs", "tests/a.rs", "tests/compiletest.rs"]


# --- the loop's identity ----------------------------------------------------


def test_the_loop_identity_is_the_default_worlds_tree_and_the_merged_view() -> None:
    loop = one_tree_cluster([BASE, ALT])
    assert loop is not None
    assert loop.name == "base" and loop.is_default
    assert loop.toolchain is None
    env: dict[str, str] = {}
    overlay_cluster_env(env, loop)
    assert env[WORLD_VAR] == "base" and judges_every_world(env)
    # An atom the worlds disagree on is undecided: an item of either world is
    # in scope. The default world alone decides it False (the control).
    assert cluster_cfg_set(loop).evaluate("alt") is None
    assert cluster_cfg_set(BASE).evaluate("alt") is False


def test_one_world_is_its_own_loop_identity() -> None:
    assert one_tree_cluster([]) is None
    assert one_tree_cluster([BASE]) is BASE
    env: dict[str, str] = {}
    overlay_cluster_env(env, BASE)
    assert not judges_every_world(env)


# --- the original crate's floor, per world -----------------------------------


def _reference() -> ReferenceMeasurement:
    return ReferenceMeasurement(key="k", clusters={
        "base": ClusterReference(
            cluster="base", passed=frozenset({("tests/a.rs", "t_shared"),
                                              ("tests/a.rs", "t_base")}),
            announced=2, reported=2),
        "alt": ClusterReference(
            cluster="alt", passed=frozenset({("tests/a.rs", "t_shared")}),
            failed=frozenset({("tests/compiletest.rs", "ui")}),
            announced=2, reported=2, rc=101),
    })


def test_a_failure_the_original_shares_in_one_world_is_excused_there_only() -> None:
    joined = join_world_captures([("base", BASE_RUN), ("alt", ALT_RUN)])
    v = judge_every_world(_reference(), joined, "base")
    assert v is not None and v.met
    # Control: the joined capture judged as the home world's alone reads the
    # other world's `ui` against a reference that never had it.
    control = judge_cluster_binaries(_reference(), "base",
                                     parse_binary_results(joined))
    assert control is not None and not control.met


def test_a_red_test_in_the_other_world_is_not_excused() -> None:
    alt_red = _capture({"tests/a.rs": {"t_shared": False},
                        "tests/compiletest.rs": {"ui": False}})
    joined = join_world_captures([("base", BASE_RUN), ("alt", alt_red)])
    v = judge_every_world(_reference(), joined, "base")
    assert v is not None and not v.met
    assert ("tests/a.rs", "t_shared") in v.unexcused_failures
    assert failed_pairs(joined) == {("tests/a.rs", "t_shared"),
                                    ("tests/compiletest.rs", "ui")}


def test_one_world_judges_as_before() -> None:
    v = judge_every_world(_reference(), BASE_RUN, "base")
    assert v == judge_cluster_binaries(_reference(), "base",
                                       parse_binary_results(BASE_RUN))
    assert failed_pairs(BASE_RUN) == frozenset()


def test_a_world_with_no_reference_gives_no_verdict() -> None:
    ref = replace(_reference(), clusters={"base": _reference().clusters["base"]})
    joined = join_world_captures([("base", BASE_RUN), ("alt", ALT_RUN)])
    assert judge_every_world(ref, joined, "base") is None


# --- the runner -------------------------------------------------------------


class _Cargo:
    """Answers each world with its canned capture; records every call."""

    def __init__(self, by_world: dict[str, tuple[int, str]]) -> None:
        self.by_world = by_world
        self.calls: list[tuple[str, str, str]] = []

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str],
            timeout: int = 180) -> Process:
        world = env.get(WORLD_VAR, "")
        self.calls.append((world, cmd, env.get("CARGO_TARGET_DIR", "")))
        rc, cap = self.by_world.get(world, (0, ""))
        if rc == -1:
            return Process(-1, "", "TIMEOUT")
        return Process(rc, cap, "")


def _runner(cargo: _Cargo) -> PortBoundCargoRunner:
    worlds = WorldChecks(
        clusters=lambda: [BASE, ALT],
        cluster_env=lambda c: (lambda e: (overlay_cluster_env(e, c), e)[1])({}),
        world_key=world_key, log=lambda m: None)
    return PortBoundCargoRunner(cargo, worlds=worlds)  # type: ignore[arg-type]


def _loop_env() -> dict[str, str]:
    env: dict[str, str] = {}
    overlay_cluster_env(env, one_tree_cluster([BASE, ALT]))
    return env


def test_a_loop_test_run_is_every_worlds_run(tmp_path: Path) -> None:
    cargo = _Cargo({"base": (0, BASE_RUN), "alt": (101, ALT_RUN)})
    rc, text = _runner(cargo).run("cargo test --no-fail-fast", tmp_path, _loop_env())
    assert [c[0] for c in cargo.calls] == ["base", "alt"]
    assert cargo.calls[1][1] == "cargo +nightly test --no-fail-fast"
    assert cargo.calls[1][2].endswith("target/worlds/alt")
    assert rc == 101
    assert sorted(world_sections(text)) == ["alt", "base"]


def test_a_world_that_timed_out_makes_the_run_a_timeout(tmp_path: Path) -> None:
    cargo = _Cargo({"base": (0, BASE_RUN), "alt": (-1, "")})
    rc, _text = _runner(cargo).run("cargo test", tmp_path, _loop_env())
    assert rc == -1


def test_a_run_that_does_not_judge_every_world_is_one_run(tmp_path: Path) -> None:
    """Controls: a world's own env (augmentation's), a check command, and a
    runner with no worlds bound each run once, as before."""
    cargo = _Cargo({"base": (0, BASE_RUN), "alt": (0, ALT_RUN)})
    env: dict[str, str] = {}
    overlay_cluster_env(env, BASE)
    assert _runner(cargo).run("cargo test", tmp_path, env) == (0, BASE_RUN)
    assert len(cargo.calls) == 1
    cargo.calls.clear()
    _runner(cargo).run("cargo check --all-targets", tmp_path, _loop_env())
    assert len(cargo.calls) == 1
    cargo.calls.clear()
    PortBoundCargoRunner(cargo).run("cargo test", tmp_path, _loop_env())  # type: ignore[arg-type]
    assert len(cargo.calls) == 1
    assert ALL_WORLDS_VAR in _loop_env()


def test_the_world_header_is_written_only_when_worlds_join() -> None:
    joined = join_world_captures([("base", BASE_RUN), ("alt", ALT_RUN)])
    assert "=== cfg world: base ===" in joined
    assert "=== cfg world:" not in join_world_captures([("base", BASE_RUN)])


# --- a verdict is (world, binary, test) --------------------------------------


def test_a_joined_capture_reconciles_its_counts_with_its_verdicts() -> None:
    """Run `989033-faa2ae3e` (2026-10-20), the first one-tree sweep: `VERDICT
    MISMATCH — UNEXPLAINED: 188 qualified pass(es) vs pass_count=352`. The
    two worlds run the same binaries under the same labels, so (binary,
    test) pairs collapsed across worlds while the trailer counts summed."""
    from pipeline_core.domain.measurement import Measurement, qualified_verdicts
    joined = join_world_captures([("base", BASE_RUN), ("alt", ALT_RUN)])
    bins = parse_binary_results(joined)
    assert [(b.world, b.label) for b in bins] == [
        ("base", "tests/a.rs"), ("alt", "tests/a.rs"),
        ("alt", "tests/compiletest.rs")]
    p_pairs, f_pairs, _inc = qualified_verdicts(bins)
    passed, failed = parse_test_counts(joined)[:2]
    m = Measurement(cluster="base", iteration=1, tree_sha="t",
                    oracle_size=passed + failed, pass_count=passed,
                    fail_count=failed, passed=frozenset(), failed=frozenset(),
                    passed_pairs=p_pairs, failed_pairs=f_pairs)
    assert m.reconcile().reconciled
    assert ("base::tests/a.rs", "t_shared") in p_pairs
    assert ("alt::tests/a.rs", "t_shared") in p_pairs
    # Control: the same binaries keyed by label alone lose a world's copy.
    bare = {(b.label, n) for b in bins for n in b.passed}
    assert len(bare) == passed - 1


def test_a_reading_keeps_both_worlds_copies_of_a_binary() -> None:
    from pipeline_core.domain.test_results import TestStateReading
    joined = join_world_captures([("base", BASE_RUN), ("alt", ALT_RUN)])
    reading = TestStateReading(binaries=parse_binary_results(joined))
    assert sorted(reading.by_label()) == [
        "alt::tests/a.rs", "alt::tests/compiletest.rs", "base::tests/a.rs"]
    one = TestStateReading(binaries=parse_binary_results(BASE_RUN))
    assert sorted(one.by_label()) == ["tests/a.rs"]


# --- review fixes, 2026-10-20 -------------------------------------------------


def test_no_world_pays_for_another_worlds_loss_at_the_keep() -> None:
    """The keep gate kept a body on a summed net gain: +2 in one world and -1
    in the other read `net_pass_delta=+1`, `regressed=False`. Each world is
    now judged on its own by the same rule; a world's own net gain still
    trades a red test inside that world."""
    from dataclasses import replace

    from pipeline_core.domain.test_results import TestStateReading, compare_readings
    base_base = _capture({"tests/a.rs": {"t1": True, "t2": False, "t3": False}})
    base_alt = _capture({"tests/a.rs": {"t1": True}})
    cur_base = _capture({"tests/a.rs": {"t1": True, "t2": True, "t3": True}})
    cur_alt = _capture({"tests/a.rs": {"t1": False}})

    def _reading(home: str, other: str) -> TestStateReading:
        return TestStateReading(binaries=parse_binary_results(
            join_world_captures([("base", home), ("alt", other)])))

    cmp = compare_readings(_reading(base_base, base_alt), _reading(cur_base, cur_alt))
    assert cmp.net_pass_delta == 1
    assert cmp.regressed
    assert dict(cmp.per_world)["alt"].regressed
    assert not dict(cmp.per_world)["base"].regressed
    # Control: the summed rule alone keeps it.
    assert not replace(cmp, per_world=()).regressed
    # The other world unchanged: a keep.
    assert not compare_readings(_reading(base_base, base_alt),
                                _reading(cur_base, base_alt)).regressed


def test_the_high_water_moves_only_when_no_world_lost() -> None:
    from pipeline_core.domain.world_score import WorldScore
    best = WorldScore.of_capture(join_world_captures(
        [("base", _capture({"tests/a.rs": {"t1": True, "t2": False}})),
         ("alt", _capture({"tests/a.rs": {"t1": True}}))]))
    traded = WorldScore.of_capture(join_world_captures(
        [("base", _capture({"tests/a.rs": {"t1": True, "t2": True}})),
         ("alt", _capture({"tests/a.rs": {"t1": False}}))]))
    gained = WorldScore.of_capture(join_world_captures(
        [("base", _capture({"tests/a.rs": {"t1": True, "t2": True}})),
         ("alt", _capture({"tests/a.rs": {"t1": True}}))]))
    assert traded.lost_against(best) == ("alt",)
    assert gained.lost_against(best) == ()
    assert WorldScore.of_capture(BASE_RUN).lost_against(None) == ()
    # A one-world mark carried over a resume proves no loss.
    assert gained.lost_against(WorldScore({"": 99})) == ()


def test_the_world_that_timed_out_is_the_hung_one_wherever_it_ran(tmp_path) -> None:
    """A home-world timeout followed by a completed world read as no sweep:
    only the LAST world was parsed as cut, so the home world's open section
    was a crash and no binary was hung."""
    open_section = join_capture(
        "\nrunning 2 tests\ntest t_shared ... ok\ntest t_slow ... ",
        "     Running tests/a.rs (target/debug/deps/a-0123456789abcdef)")
    joined = join_world_captures([("base", open_section), ("alt", ALT_RUN)],
                                 timed_out={"base"})
    bins = parse_binary_results(joined)
    home = [b for b in bins if b.world == "base"]
    assert home and home[0].hung and home[0].dangling == "t_slow"
    assert any(b.world == "alt" and "ui" in b.failed for b in bins)
    # Control: the last world cut is still read as hung there.
    last = join_world_captures([("alt", ALT_RUN), ("base", open_section)],
                               timed_out={"base"})
    assert [b.hung for b in parse_binary_results(last) if b.world == "base"] == [True]
    # The runner names the world by its own return code.
    cargo = _Cargo({"base": (-1, ""), "alt": (101, ALT_RUN)})
    rc, text = _runner(cargo).run("cargo test", tmp_path, _loop_env())
    assert rc == -1 and "=== cfg world: base (timed out) ===" in text


def test_a_world_qualified_failure_maps_to_its_cargo_target() -> None:
    from pipeline_core.domain.measurement import (
        cargo_target_of,
        failing_binaries_by_name,
        qualified_verdicts,
    )
    assert cargo_target_of("nightly::tests/foo.rs") == "foo"
    assert cargo_target_of("feature/x::tests/foo/main.rs") == "foo"
    assert cargo_target_of("tests/foo.rs") == "foo"
    joined = join_world_captures([("base", BASE_RUN), ("alt", ALT_RUN)])
    _p, failed, _i = qualified_verdicts(parse_binary_results(joined))
    assert failing_binaries_by_name(failed) == {"ui": frozenset({"compiletest"})}
