"""Integration: a generated test is gated to the worlds real cargo shows it
builds and passes in.

Real cargo, the pipeline's cluster env, the real per-world runner and the real
settle step. No model.

The class, run `292191-d60a5308` (2026-10-02): the gate on a generated test
was predicted from its text. A test written under the default cluster was
shared only when it named no fn that some cluster compiles out, matched by
bare name, and a test written under any other cluster was never shared. On
that workspace 26 of 26 default-cluster files were gated (24 on the word
`fmt`), 25 of them pass in the other cluster once ungated, and the later
cluster generated 75 tests for lines already covered by tests it could not
compile.

The fix rests on cargo's behaviour, pinned by `test_premise_*`: a test target
whose crate-level `#![cfg(P)]` is false builds to an empty binary that runs
zero tests and exits 0, so gating a file out of a world cannot break that
world's run; and a test target that names an item the world compiles out
fails to build, on its own, without touching the other targets.

The crate is invented: one fn every world compiles, one compiled only under a
cfg, one whose value differs between the two worlds.

Gated on `INTEGRATION=1` (plain cargo, no dependencies);
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli, cluster_cargo_env
from pipeline_core.adapters.test_worlds import test_passes_in_world
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.application.reconstruct.cluster_membership import cluster_cfg_attr
from pipeline_core.application.test_worlds import (
    TestWorldsDeps,
    gate_line,
    own_world_gate,
    settle_test_gate,
)
from pipeline_core.domain.cluster_env import world_key
from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.test_results import parse_test_counts

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_CFG = "gadget_alt"
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "test-worlds"

_CARGO_TOML = """[package]
name = "gadgets"
version = "0.1.0"
edition = "2021"

[lints.rust]
unexpected_cfgs = { level = "allow" }
"""

_LIB = f"""pub fn size() -> u8 {{
    1
}}

#[cfg({_CFG})]
pub fn extra() -> u8 {{
    7
}}

pub fn flavour() -> u8 {{
    if cfg!({_CFG}) {{ 2 }} else {{ 1 }}
}}
"""

# Holds the words a name predicate would have matched (`fmt`, `extra` in a
# comment); calls only what every world compiles.
_SHARED = """use std::fmt;
// not the extra path
#[test]
fn sized() {
    assert_eq!(gadgets::size(), 1);
    let _ = fmt::Error;
}
"""
_NEEDS_ALT = """#[test]
fn has_extra() {
    assert_eq!(gadgets::extra(), 7);
}
"""
_BASE_VALUE = """#[test]
fn base_flavour() {
    assert_eq!(gadgets::flavour(), 1);
}
"""


def _crate(tmp_path: Path) -> Path:
    root = tmp_path / "gadgets"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LIB)
    return root


def _clusters() -> list[BuildCluster]:
    return [
        BuildCluster(name="base", env_extra={}, tarpaulin_exclude_files=[],
                     excluded_cfg=[_CFG], active_cfg=[], prompt_nightly_rule="",
                     is_default=True),
        BuildCluster(name="alt", env_extra={"RUSTFLAGS": f"--cfg {_CFG}"},
                     tarpaulin_exclude_files=[], excluded_cfg=[],
                     active_cfg=[_CFG], prompt_nightly_rule="", is_default=False),
    ]


def _deps(clusters: list[BuildCluster]) -> TestWorldsDeps:
    return TestWorldsDeps(
        clusters=lambda: clusters,
        world_gate=lambda c: cluster_cfg_attr(c, clusters),
        passes_in=functools.partial(test_passes_in_world,
                                    cluster_env=cluster_cargo_env, timeout=600),
        log=lambda _m: None, world_key=world_key)


def _write(root: Path, stem: str, body: str, generating: BuildCluster,
           deps: TestWorldsDeps) -> Path:
    f = root / "tests" / f"{stem}.rs"
    f.write_text(gate_line(own_world_gate(generating, deps)) + body)
    return f


def _tests_run(root: Path, cluster: BuildCluster, stem: str) -> tuple[int, int]:
    """(rc, tests reported) for one test target under one world's env."""
    env = cluster_cargo_env(cluster)
    env["CARGO_TARGET_DIR"] = str(_TARGET / root.parent.name / cluster.name)
    rc, out = run_cargo(CargoCli(), f"cargo test --offline --test {stem}",
                        root, env, timeout=600)
    passed, failed, ignored = parse_test_counts(out)
    return rc, passed + failed + ignored


def test_premise_a_gated_out_test_target_builds_empty_and_exits_zero(
        tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, alt = _clusters()
    deps = _deps([base, alt])
    _write(root, "extra_gen_1", _NEEDS_ALT, alt, deps)
    rc, ran = _tests_run(root, base, "extra_gen_1")
    assert (rc, ran) == (0, 0), (
        "a test target whose crate-level #![cfg] is false in this world must "
        "build to an empty binary; if cargo now fails or runs it, gating a "
        "generated test out of a world no longer protects that world's run")


def test_premise_naming_a_compiled_out_item_fails_only_that_target(
        tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, _alt = _clusters()
    (root / "tests" / "extra_gen_1.rs").write_text(_NEEDS_ALT)   # ungated
    (root / "tests" / "size_gen_1.rs").write_text(_SHARED)
    rc_bad, _ = _tests_run(root, base, "extra_gen_1")
    rc_ok, ran_ok = _tests_run(root, base, "size_gen_1")
    assert rc_bad != 0 and (rc_ok, ran_ok) == (0, 1)


def test_a_world_agnostic_test_ends_ungated_and_runs_in_both_worlds(
        tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, alt = _clusters()
    deps = _deps([base, alt])
    f = _write(root, "size_gen_1", _SHARED, base, deps)
    # NEGATIVE CONTROL: under its generating world's gate the other world
    # compiles it out — the state every generated test used to stay in.
    assert _tests_run(root, alt, "size_gen_1") == (0, 0)
    assert settle_test_gate(root, "gadgets", f, _SHARED, base, deps) == ""
    assert f.read_text() == _SHARED
    assert _tests_run(root, alt, "size_gen_1") == (0, 1)
    assert _tests_run(root, base, "size_gen_1") == (0, 1)


def test_a_test_written_under_the_cfg_world_is_shared_back(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, alt = _clusters()
    deps = _deps([base, alt])
    f = _write(root, "size_gen_2", _SHARED, alt, deps)
    assert settle_test_gate(root, "gadgets", f, _SHARED, alt, deps) == ""
    assert _tests_run(root, base, "size_gen_2") == (0, 1)


def test_a_test_that_needs_a_gated_item_stays_in_its_world(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, alt = _clusters()
    deps = _deps([base, alt])
    f = _write(root, "extra_gen_1", _NEEDS_ALT, alt, deps)
    before = f.read_text()
    gate = settle_test_gate(root, "gadgets", f, _NEEDS_ALT, alt, deps)
    assert gate == cluster_cfg_attr(alt, [base, alt]) and f.read_text() == before
    assert _tests_run(root, base, "extra_gen_1") == (0, 0)
    assert _tests_run(root, alt, "extra_gen_1") == (0, 1)


def test_a_test_whose_assertion_is_false_in_the_other_world_stays_gated(
        tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, alt = _clusters()
    deps = _deps([base, alt])
    f = _write(root, "flavour_gen_1", _BASE_VALUE, base, deps)
    gate = settle_test_gate(root, "gadgets", f, _BASE_VALUE, base, deps)
    assert gate == cluster_cfg_attr(base, [base, alt])
    assert _tests_run(root, alt, "flavour_gen_1") == (0, 0)


def test_a_block_that_gates_itself_is_not_called_passing_where_it_runs_nothing(
        tmp_path: Path) -> None:
    """A generated block can carry its own crate-level gate
    (`tests/error_gen_5.rs` on the recorded workspace). In the other world
    it builds, runs none of its tests and exits 0: not a pass there."""
    root = _crate(tmp_path)
    base, alt = _clusters()
    deps = _deps([base, alt])
    own = cluster_cfg_attr(base, [base, alt])
    body = f"#![cfg({own})]\n" + _SHARED
    f = _write(root, "size_gen_3", body, base, deps)
    assert settle_test_gate(root, "gadgets", f, body, base, deps) == own


_ALT_VALUE = """#[test]
fn alt_flavour() {
    assert_eq!(gadgets::flavour(), 2);
}
"""


def test_a_test_that_fails_where_it_was_written_has_no_world(tmp_path: Path) -> None:
    """Written under the base world, its assertion is false there and true
    under the cfg world. It must not come back ungated, or gated to the world
    it happens to pass in: it comes back None and the file is as found.
    NEGATIVE CONTROL: the other world's runner does call it passing, which is
    what a settle that trusted the generating world would have added."""
    root = _crate(tmp_path)
    base, alt = _clusters()
    deps = _deps([base, alt])
    f = _write(root, "flavour_gen_2", _ALT_VALUE, base, deps)
    before = f.read_text()
    assert settle_test_gate(root, "gadgets", f, _ALT_VALUE, base, deps) is None
    assert f.read_text() == before
    f.write_text(_ALT_VALUE)
    assert test_passes_in_world(root, "gadgets", "flavour_gen_2", _ALT_VALUE, alt,
                                cluster_env=cluster_cargo_env, timeout=600) is True


# One test passes only because the other, which sorts first, set process
# state. Run `801510-bb22799f` (2026-10-20), `tests/fmt_gen_1.rs`: a test
# asserting a captured backtrace beside one that sets `RUST_LIB_BACKTRACE`.
_ORDER_DEPENDENT = """#[test]
fn a_sets_the_mode() {
    std::env::set_var("GADGET_MODE", "1");
    assert_eq!(gadgets::size(), 1);
}

#[test]
fn b_reads_the_mode() {
    assert_eq!(std::env::var("GADGET_MODE").as_deref(), Ok("1"));
}
"""


def _file_run(root: Path, cluster: BuildCluster, stem: str,
              *extra: str) -> tuple[int, str]:
    env = cluster_cargo_env(cluster)
    env["CARGO_TARGET_DIR"] = str(_TARGET / root.parent.name / cluster.name)
    args = " ".join(extra)
    return run_cargo(CargoCli(), f"cargo test --offline --test {stem} -- "
                     f"--test-threads=1 {args}", root, env, timeout=600)


def test_premise_one_thread_runs_a_file_in_name_order_and_shares_its_process(
        tmp_path: Path) -> None:
    """The file run settles on (`--test-threads=1`) passes the order-dependent
    pair; `--exact` on the reader alone fails it. If libtest stops running a
    one-thread file in name order in one process, the alone check is no
    longer the only thing that tells the two apart."""
    root = _crate(tmp_path)
    base, _alt = _clusters()
    (root / "tests" / "mode_gen_0.rs").write_text(_ORDER_DEPENDENT)
    rc, out = _file_run(root, base, "mode_gen_0")
    assert rc == 0 and parse_test_counts(out)[:2] == (2, 0)
    rc_alone, out_alone = _file_run(root, base, "mode_gen_0",
                                    "--exact", "b_reads_the_mode")
    assert rc_alone != 0 and parse_test_counts(out_alone)[:2] == (0, 1)


def test_a_test_that_passes_only_after_another_has_no_world(tmp_path: Path) -> None:
    """The settle refuses the block and leaves the file as found.
    NEGATIVE CONTROL: the whole-file reading the settle used to stop at
    passes it (the premise above)."""
    root = _crate(tmp_path)
    base, alt = _clusters()
    logs: list[str] = []
    deps = TestWorldsDeps(
        clusters=lambda: [base, alt],
        world_gate=lambda c: cluster_cfg_attr(c, [base, alt]),
        passes_in=functools.partial(test_passes_in_world,
                                    cluster_env=cluster_cargo_env, timeout=600,
                                    log=logs.append),
        log=logs.append, world_key=world_key)
    f = _write(root, "mode_gen_1", _ORDER_DEPENDENT, base, deps)
    before = f.read_text()
    assert settle_test_gate(root, "gadgets", f, _ORDER_DEPENDENT, base, deps) is None
    assert f.read_text() == before
    assert any("b_reads_the_mode fail(s) when run alone" in m for m in logs), logs


# --- the crate's doc examples, emitted as oracle tests -----------------------
# Same reader (the per-world runner and the settle), fed by the real extractor's
# doc records. Run `292191-d60a5308`: the one assertion pinning a lost
# behaviour was a doc example, which no cluster sweep runs and no route can be
# aimed at; as a test under `tests/` it is red where the behaviour is missing.

_DOC_LIB = f"""/// The size of a gadget.
///
/// ```
/// assert_eq!(gadgets::size(), 1);
/// ```
pub fn size() -> u8 {{
    1
}}

/// Only in the alternate world.
///
/// ```
/// assert_eq!(gadgets::extra(), 7);
/// ```
#[cfg({_CFG})]
pub fn extra() -> u8 {{
    7
}}

/// Documented with an example that is not true.
///
/// ```
/// assert_eq!(gadgets::flavour(), 9);
/// ```
pub fn flavour() -> u8 {{
    1
}}
"""


def _doc_deps(clusters: list[BuildCluster]):  # noqa: ANN202
    from pipeline_core.adapters.extractor import CliExtractor
    from pipeline_core.adapters.structure import CliStructureReader
    from pipeline_core.application.doc_example_tests import DocExampleDeps
    from pipeline_core.application.test_worlds import world_is_measurable

    extractor = (Path(__file__).resolve().parents[2] / "tools" / "skeleton_extractor"
                 / "target" / "release" / "skeleton-extractor")
    assert extractor.exists(), (
        f"{extractor} missing — build it (bash pre_run.sh does) before the "
        "integration tests; they read the real extractor")
    tw = _deps(clusters)
    return DocExampleDeps(
        enumerate_doc_items=CliStructureReader(CliExtractor(extractor)).enumerate_doc_items,
        world_gate=lambda c: cluster_cfg_attr(c, clusters),
        settle=lambda p, m, f, b, c: settle_test_gate(p, m, f, b, c, tw),
        measurable=lambda c: world_is_measurable(c, tw),
        log=lambda _m: None)


def test_doc_examples_become_tests_in_the_worlds_where_the_original_passes_them(
        tmp_path: Path) -> None:
    from pipeline_core.application.doc_example_tests import emit_doc_example_tests

    root = _crate(tmp_path)
    (root / "src" / "lib.rs").write_text(_DOC_LIB)
    base, alt = _clusters()
    reading = emit_doc_example_tests(root, "gadgets", [base, alt],
                                     _doc_deps([base, alt]))
    assert reading.examples == 3
    assert len(reading.dropped) == 1 and reading.dropped[0].startswith("src/lib.rs:")
    assert len(reading.kept) == 2
    shared, gated = sorted(reading.kept)
    assert _tests_run(root, base, shared[:-3]) == (0, 1)
    assert _tests_run(root, alt, shared[:-3]) == (0, 1)
    # The example of the cfg-only item runs in its world and compiles out of
    # the other.
    assert _tests_run(root, alt, gated[:-3]) == (0, 1)
    assert _tests_run(root, base, gated[:-3]) == (0, 0)
    assert (root / "tests" / gated).read_text().startswith(
        f"#![cfg({cluster_cfg_attr(alt, [base, alt])})]\n")


def test_an_emitted_doc_example_is_red_on_a_tree_that_lost_the_behaviour(
        tmp_path: Path) -> None:
    """What the loop gains: the emitted test passes on the tree it was taken
    from and fails once a body stops doing what the example asserts.
    NEGATIVE CONTROL: on that changed tree the crate's other test target —
    all a cluster sweep ran before — has nothing to say."""
    from pipeline_core.application.doc_example_tests import emit_doc_example_tests

    root = _crate(tmp_path)
    (root / "src" / "lib.rs").write_text(_DOC_LIB)
    base, alt = _clusters()
    reading = emit_doc_example_tests(root, "gadgets", [base, alt],
                                     _doc_deps([base, alt]))
    shared = sorted(reading.kept)[0][:-3]
    assert _tests_run(root, base, shared) == (0, 1)
    (root / "src" / "lib.rs").write_text(_DOC_LIB.replace("    1\n}\n\n/// Only", "    2\n}\n\n/// Only"))
    rc, ran = _tests_run(root, base, shared)
    assert rc != 0 and ran == 1


# --- The behaviour emitter's no-verdict round, over real cargo -------------
#
# Run `15370-6a4cfd45` (2026-10-30): one capture per world whose replay did
# not compile, and the no-verdict branch withdrew every capture in the file
# (24 + 26). The fix reads rustc's `--> tests/<stem>.rs:LINE:COL` against the
# rendered capture spans, and, for a binary that compiled and died, runs the
# unreported tests alone through the real alone runner. Both rest on tool
# behaviour pinned below: rustc locates an error inside a test fn on a line of
# that fn's span, and a test binary that aborts under `--test-threads=1`
# reports the tests before it and no trailer.

def _emit_specs(names_whens: list[tuple[str, str]]) -> list:  # noqa: ANN001
    from pipeline_core.domain.behaviour_probe import CapturedProbe, captured_spec
    return [captured_spec(cfg_cluster="base",
                          probe=CapturedProbe(name=n, given="", when=w, observe="display"),
                          n=0, stdout="1", default_cluster="base")
            for n, w in names_whens]


def _emitter_run(root: Path, cluster: BuildCluster, specs: list):  # noqa: ANN001,ANN202
    from pipeline_core.adapters.test_worlds import failing_alone
    from pipeline_core.application.emit_behaviour_tests import emit_behaviour_tests

    def env_for(extra: dict[str, str]) -> dict[str, str]:
        env = cluster_cargo_env(cluster)
        env["CARGO_TARGET_DIR"] = str(_TARGET / root.parent.name / cluster.name)
        env.update(extra)
        return env

    def verify(stem: str, extra) -> tuple[bool, str]:  # noqa: ANN001
        # The production verify's shape (`composition._verify_behaviour_tests`):
        # a plain process, no isolation re-run, so a death stays a death.
        import subprocess
        run = subprocess.run(["cargo", "test", "--offline", "--test", stem, "--", "--test-threads=1"],
                             cwd=root, env=env_for(dict(extra)), capture_output=True,
                             text=True, timeout=600, check=False)
        return run.returncode == 0, (run.stdout or "") + "\n" + (run.stderr or "")

    return emit_behaviour_tests(
        root, "base", specs, crate="gadgets", verify=verify,
        failing_alone=lambda stem, names, extra: failing_alone(
            root, "gadgets", stem, names, env=env_for(dict(extra)), timeout=600),
        settle=lambda _d, body: body)


def test_premise_rustc_locates_an_error_inside_a_test_on_that_tests_lines(
        tmp_path: Path) -> None:
    from pipeline_core.application.emit_behaviour_tests import (
        capture_line_spans,
        render_behaviour_test_file,
        replay_fn_name,
    )
    from pipeline_core.domain.test_results import parse_rustc_compile_errors
    root = _crate(tmp_path)
    base, _alt = _clusters()
    specs = _emit_specs([("ok_a", "gadgets::size()"), ("broken", "gadgets::nope()"),
                         ("ok_c", "gadgets::size()")])
    body = render_behaviour_test_file(specs, crate="gadgets")
    (root / "tests" / "behaviour_base_gen_1.rs").write_text(body)
    rc, out = _file_run(root, base, "behaviour_base_gen_1")
    diags = [d for d in parse_rustc_compile_errors(out)
             if Path(d.file).name == "behaviour_base_gen_1.rs"]
    assert rc != 0 and diags, out[-800:]
    lo, hi = capture_line_spans(body, specs)[replay_fn_name(specs[1])]
    assert all(lo <= d.line <= hi for d in diags)


def test_a_capture_that_does_not_compile_is_withdrawn_alone(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, _alt = _clusters()
    specs = _emit_specs([("ok_a", "gadgets::size()"), ("broken", "gadgets::nope()"),
                         ("ok_c", "gadgets::size()")])
    dest, dropped, misses, emitted = _emitter_run(root, base, specs)
    assert dest is not None and dropped == (specs[1].behaviour_id,)
    assert misses[0]["reason"].startswith("replay did not compile: ")
    assert {s.behaviour_id for s in emitted} == {specs[0].behaviour_id, specs[2].behaviour_id}


def test_a_capture_whose_test_aborts_is_withdrawn_alone(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base, _alt = _clusters()
    specs = _emit_specs([("a_ok", "gadgets::size()"),
                         ("b_dies", "{ std::process::abort(); #[allow(unreachable_code)] 1u8 }"),
                         ("c_ok", "gadgets::size()")])
    dest, dropped, misses, _emitted = _emitter_run(root, base, specs)
    assert dest is not None and dropped == (specs[1].behaviour_id,)
    assert misses[0]["reason"].startswith("replay binary died")


def test_negative_control_without_attribution_the_file_goes_whole(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline_core.application.emit_behaviour_tests as ebt
    monkeypatch.setattr(ebt, "no_verdict_culprits", lambda *_a, **_k: [])
    root = _crate(tmp_path)
    base, _alt = _clusters()
    specs = _emit_specs([("ok_a", "gadgets::size()"), ("broken", "gadgets::nope()")])
    dest, dropped, _misses, _emitted = _emitter_run(root, base, specs)
    assert dest is None and set(dropped) == {s.behaviour_id for s in specs}
