"""Integration: a failing test whose only assertion sits in a helper fn of
its own file gets the helper's operands observed, and the per-fn refill's
first ask carries what the sweep measured.

The class, run `507656-fe50f9f1` (2026-09-24, cluster `stable`, pass 170
flat for three sweeps): 13 of the 14 failing tests called one
`#[track_caller]` helper holding `assert_eq!(actual.contains(" vs "),
expected.contains(" vs "))`. The panic pointed at the call site and printed
`left: false` / `right: true`; the assertion probe rewrote the test bodies
only, so it observed 1 of 14 tests, and the string every one of those tests
was about — the rendered message, cut short by a fixed buffer — reached no
prompt. The per-fn refill of the renderer then opened with a LAST_ERROR of
one sentence ("This body COMPILES but …") and no panic at all, while the
file-first prompt of the same iteration quoted the panic lines. The reply
resubmitted the body.

Real pieces: cargo through the pipeline's runner (`run_cargo` over
`CargoCli`), the sweep's evidence reader (`failure_evidence_by_test`,
`merge_observations`), the probe runner (`run_assert_probes`), the fill
roles and `FillOneFn`. Scripted: the model's reply. The crate is invented:
one free fn whose body compiles and cuts its label short, one test file with
a `#[track_caller]` helper, and a second test whose name has the first as a
prefix (a libtest filter is a substring match).

Gated on `INTEGRATION=1` (real cargo).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_CLUSTER = "main"
_CARGO_TOML = """[package]
name = "gauges"
version = "0.1.0"
edition = "2021"
"""
# Compiles, and cuts every label to eight bytes: the separator never survives.
_BODY = """    let mut s = format!("gauge {} / {}", n, 255);
    s.truncate(8);
    s"""
_LIB = "pub fn label(n: u8) -> String {\n" + _BODY + "\n}\n"
_TESTS = """use gauges::label;

#[track_caller]
fn check_label(n: u8, want: &'static str) {
    let got = label(n);
    assert_eq!(got.contains(" / "), want.contains(" / "));
}

#[test]
fn test_label() {
    check_label(3, "gauge 3 / 255");
}

#[test]
fn test_label_wide() {
    check_label(4, "gauge 4 / 255");
}
"""
_OBSERVED = 'got = "gauge 3 "'


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(root / "target")
    return env


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    cluster = project / ".skeleton-filled" / _CLUSTER
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text(_CARGO_TOML)
    (cluster / "src" / "lib.rs").write_text(_LIB)
    (project / "tests").mkdir()
    (project / "tests" / "gauges.rs").write_text(_TESTS)
    (cluster / "tests").symlink_to(project / "tests")
    (project / ".skeleton" / "_failures").mkdir(parents=True)
    return project


def _cargo(cmd: str, cwd: Path, env: dict[str, str],
           timeout: int = 600) -> tuple[int, str]:
    from pipeline_core.adapters.cargo import CargoCli
    from pipeline_core.application.reconstruct.cargo_runner import run_cargo
    return run_cargo(CargoCli(), cmd, cwd, env, timeout=timeout)


def _sweep_evidence(project: Path, monkeypatch: pytest.MonkeyPatch
                    ) -> tuple[dict[str, str], dict[str, str]]:
    """(the sweep's own words per failing test, the probe's rendered
    observations) — the two halves the TDD driver merges."""
    from pipeline_core.application.reconstruct.assert_probe_runner import (
        run_assert_probes,
    )
    from pipeline_core.domain.test_report import failure_evidence_by_test
    cluster = project / ".skeleton-filled" / _CLUSTER
    monkeypatch.setenv("ASSERT_PROBE_STAGE_ROOT", str(project.parent / "stage"))
    _rc, out = _cargo("cargo test --offline --no-fail-fast", cluster,
                      _env(cluster))
    evidence = failure_evidence_by_test(out)
    probed = run_assert_probes(
        project_dir=project, cluster_root=cluster, cluster_name=_CLUSTER,
        cluster_env=_env(project.parent / "stage"),
        tests_by_file={project / "tests" / "gauges.rs": ["test_label"]},
        run_cargo=_cargo, log=lambda _m: None)
    return evidence, dict(probed.rendered)


def test_premise_libtest_names_the_test_thread_after_the_test(
        tmp_path: Path) -> None:
    """The helper's prints name the test by the running thread. libtest
    spawns a thread named after the test path, including under
    `--test-threads=1`, which is how the probe runner invokes it."""
    root = tmp_path / "tn"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "tn"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / "src" / "lib.rs").write_text("")
    (root / "tests" / "t.rs").write_text(
        'fn h() { eprintln!("NAME={:?}", std::thread::current().name()); }\n'
        "#[test] fn alpha() { h(); }\n"
        "mod m { #[test] fn beta() { super::h(); } }\n")
    _rc, out = _cargo("cargo test --offline --test t -- --nocapture "
                      "--test-threads=1", root, _env(root))
    assert 'NAME=Some("alpha")' in out, out
    assert 'NAME=Some("m::beta")' in out, out


def test_the_helper_assertion_is_observed_for_the_test_that_ran_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _workspace(tmp_path)
    evidence, observed = _sweep_evidence(project, monkeypatch)
    # Premise: the sweep's own words carry the call site and two booleans,
    # never the label.
    said = next(v for k, v in evidence.items() if k.endswith("test_label"))
    assert "left: false" in said and "gauge 3" not in said, said
    assert list(observed) == ["test_label"], observed
    assert _OBSERVED in observed["test_label"]
    assert "(in `check_label`, which this test calls)" in observed["test_label"]


def test_negative_control_body_only_probe_observes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-change probe rewrote the named test bodies alone."""
    from pipeline_core.domain import assert_probe
    monkeypatch.setattr(assert_probe, "helpers_called_by", lambda *_a: ())
    project = _workspace(tmp_path)
    _evidence, observed = _sweep_evidence(project, monkeypatch)
    assert observed == {}


class _Tel:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None: ...

    def check_stop(self, context: str) -> None: ...


class _Delegate:
    def __init__(self, inner: Any, **overrides: Any) -> None:
        self._inner = inner
        for k, v in overrides.items():
            setattr(self, k, v)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _first_refill_prompt(project: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    import pipeline
    from pipeline_core.application.reconstruct.context import PerFnPassCtx
    from pipeline_core.application.reconstruct.fill_one_fn import (
        FillCaps,
        FillOneFn,
    )
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.models import FnLocator, Stub
    from pipeline_core.domain.test_report import merge_observations

    evidence, observed = _sweep_evidence(project, monkeypatch)
    cluster = project / ".skeleton-filled" / _CLUSTER
    unit = FnLocator("src/lib.rs", "label")
    frontier = FailureFrontier.build(
        {unit: {"test_label"}}, {unit: {"test_label"}},
        evidence=merge_observations(evidence, observed))
    roles = pipeline._APP.fill_roles
    prompts: list[str] = []

    def ask(prompt: str, *_a: Any, **_k: Any) -> tuple[str | None, str]:
        prompts.append(prompt)
        return _BODY, _BODY

    fill = FillOneFn(
        tel=_Tel(), mut=roles.mut, cargo=roles.cargo,
        prompt=_Delegate(roles.prompt, ask_per_fn_body=ask),
        retros=_Delegate(roles.retros,
                         diagnose=lambda **_k: {"signal": "", "guidance": ""},
                         extract_self_note=lambda *a, **k: None),
        tests=roles.tests, stall=roles.stall,
        caps=FillCaps(per_fn_fix_hard_cap=1, per_fn_fix_max_stalls=1,
                      fix_err_cap=4000))
    ctx = PerFnPassCtx(
        project_dir=project, target_src_dir=cluster, module_name="gauges",
        cluster_name=_CLUSTER, check_cmd="cargo check --offline --tests",
        cluster_env=_env(cluster), fr_path=None, tests_dir_files=[],
        macros_section="", invariants_section="", crate_type_records=[],
        src_for_macros=cluster / "src",
        failures_dir=project / ".skeleton" / "_failures",
        tdd_frontier_tests=frontier,
        tdd_test_cmd="cargo test --offline --no-fail-fast",
        splice_check_lock=threading.Lock(), retros_cache={}, idiom_blocks=[],
        pass_n=1, total=1, cluster_cfg_set=None)
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "0")
    fill.run(1, Stub(file="src/lib.rs", fn_name="label", container="",
                     sig_text="pub fn label(n: u8) -> String", refill=True),
             ctx=ctx)
    assert prompts, "the refill must ask the model at least once"
    return prompts[0]


def test_the_refill_first_ask_carries_what_the_sweep_measured(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _workspace(tmp_path)
    first = _first_refill_prompt(project, monkeypatch)
    last_error = first.split("<LAST_ERROR>", 1)[1].split("</LAST_ERROR>", 1)[0]
    assert "test_label said:" in last_error, last_error
    assert "panicked at tests/gauges.rs:" in last_error, last_error
    assert _OBSERVED in last_error, last_error


def test_negative_control_the_instruction_alone_carries_no_measurement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-change seed: the instruction sentence, whatever the sweep
    measured."""
    from pipeline_core.application.reconstruct import fill_one_fn
    from pipeline_core.application.reconstruct.fill_rules import (
        REFILL_SEED_EXCERPT,
    )
    monkeypatch.setattr(fill_one_fn, "refill_seed_excerpt",
                        lambda _said: REFILL_SEED_EXCERPT)
    project = _workspace(tmp_path)
    first = _first_refill_prompt(project, monkeypatch)
    last_error = first.split("<LAST_ERROR>", 1)[1].split("</LAST_ERROR>", 1)[0]
    assert "panicked at" not in last_error
    assert _OBSERVED not in last_error


# --- where a failing test stopped: the assertion position ------------------
#
# Run `273278-22529e44` (2026-09-29, cluster `stable`): the earlier-panic
# judgment ordered raw panic lines in any file; every site on that tree was a
# `src/` stub, and a fill that let tests reach a stub declared earlier in the
# same file was graded a regression. A position now exists only when the
# panic is a line of the failing test's own body in its binary's own file
# (`assertion_positions`). What that rests on in the real toolchain: libtest
# prints the panic path relative to the package root exactly as cargo labels
# the integration binary, and a `#[track_caller]` helper reports its caller's
# line while a plain helper and a closure report their own.

_POS_LIB = """pub fn early() -> u8 {
    unimplemented!()
}

pub fn late() -> u8 {
    early()
}
"""
_POS_TESTS = """use pos::late;

fn plain_helper(v: u8) {
    assert_eq!(v, 1);
}

#[track_caller]
fn caller_helper(v: u8) {
    assert_eq!(v, 1);
}

#[test]
fn direct() {
    assert_eq!(1, 1);
    assert_eq!(2, 3);
}

#[test]
fn via_plain() {
    plain_helper(2);
}

#[test]
fn via_caller() {
    caller_helper(2);
}

#[test]
fn via_closure() {
    let check = |v: u8| assert_eq!(v, 1);
    check(2);
}

#[test]
fn via_stub() {
    assert_eq!(late(), 1);
}
"""
_POS_OTHER = """#[test]
fn direct() {
    assert!(false);
}
"""


def _pos_crate(tmp_path: Path) -> Path:
    root = tmp_path / "pos"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "pos"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / "src" / "lib.rs").write_text(_POS_LIB)
    (root / "tests" / "a.rs").write_text(_POS_TESTS)
    (root / "tests" / "b.rs").write_text(_POS_OTHER)
    return root


def test_premise_libtest_prints_the_panic_path_as_cargo_labels_the_binary(
        tmp_path: Path) -> None:
    """The position reader matches the panic path to the `Running` label;
    if cargo or libtest ever print either differently, positions vanish
    and every earlier-assertion judgment goes silent."""
    from pipeline_core.domain.test_report import parse_test_output
    from pipeline_core.domain.test_results import binary_output_sections
    root = _pos_crate(tmp_path)
    _rc, out = _cargo("cargo test --offline --no-fail-fast", root, _env(root))
    by_label = {s.label: parse_test_output(s.text) for s in
                binary_output_sections(out)}
    a = {f.test_name: f for f in by_label["tests/a.rs"].failures}
    assert a["direct"].panic_file == "tests/a.rs", out
    assert a["direct"].panic_line == 15
    assert a["via_caller"].panic_line == 25          # the caller's line
    assert a["via_plain"].panic_line == 4            # the helper's own line
    assert a["via_stub"].panic_file == "src/lib.rs"


def test_a_position_exists_only_inside_the_failing_tests_own_body(
        tmp_path: Path) -> None:
    """Contract, on real cargo output: the direct assertion and the
    track_caller call have positions; the plain helper, the closure and the
    `src/` stub have none; `direct` in two binaries is two keys. Negative
    control: the raw sites the pre-fix judgment ordered include the stub
    line in `src/` and the helper line above its caller."""
    from pipeline_core.application.reconstruct.cargo_runner import (
        oracle_test_source,
    )
    from pipeline_core.domain.assertion_position import (
        TestKey,
        assertion_positions,
    )
    from pipeline_core.domain.test_report import parse_test_output
    root = _pos_crate(tmp_path)
    _rc, out = _cargo("cargo test --offline --no-fail-fast", root, _env(root))
    pos = assertion_positions(out, oracle_test_source(root))
    assert pos == {TestKey("tests/a.rs", "direct"): 15,
                   TestKey("tests/a.rs", "via_caller"): 25,
                   TestKey("tests/b.rs", "direct"): 3}, out
    raw = {(f.panic_file, f.panic_line)
           for f in parse_test_output(out).failures}
    assert ("src/lib.rs", 2) in raw and ("tests/a.rs", 4) in raw
