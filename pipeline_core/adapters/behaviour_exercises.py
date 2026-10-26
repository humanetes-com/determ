"""Exercises writer — fill `BehaviourSpec.exercises` from LINE COVERAGE of
each behaviour's run against the original crate (the attribution
channel a MISMATCH escalates through).

No writer existed before 2026-08-26 (`layout_pins.py:235` says "the caller
fills it"; 30/30 specs on the anyhow suite carried it empty). This one does
ONE instrumented build (`-C instrument-coverage` layered on the cluster env,
`CARGO_INCREMENTAL=0` for the same reason `run_grcov` sets it: incremental
objects compiled without instrumentation yield no profraw), then per spec a
run with its own `LLVM_PROFILE_FILE` — a test-assertion spec through
`cargo test --test <stem> -- --exact <name>`, a probe spec by
re-materialising its program as an example — and grcov over that spec's
profraw alone (the base flags of `coverage._grcov_cmd`, which both crate
readings use), with `--binary-path` the one binary that wrote it. Cargo names
that file in its JSON messages (`domain.cargo_artifacts`): the build names each
test target's executable, `cargo run` names the example's. A directory there
makes grcov load every binary under it per spec: 43.8 s against 0.4 s per
profraw on the workspace of run `42057-641e589c`, 2026-10-25. The per-file hit lines map onto the extractor's fn records in the
domain (`behaviour_coverage.units_for_hit_lines`, the `line_to_fn_record`
contract, crate-root file paths).

Doc-test specs are SKIPPED: rustdoc builds doc-tests as separate crates and
instrumenting them needs the nightly-only `-Z doctest-xcompile`/
`RUSTDOCFLAGS=-C instrument-coverage` path, which is not the toolchain the
stable cluster runs under.
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pipeline_core.adapters.behaviour_probe import (
    Runner,
    materialized_probe,
    probe_env,
    subprocess_runner,
    t0d_target_dir,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_coverage import (
    SRC_PREFIX,
    coveralls_hit_lines,
    is_doctest_spec,
    is_test_assertion_spec,
    normalize_record_files,
    sites_for_hit_lines,
    test_command,
    test_target_of,
    units_for_hit_lines,
)
from pipeline_core.domain.behaviour_probe import is_probe_spec, program_for
from pipeline_core.domain.cargo_artifacts import executables_from_messages
from pipeline_core.domain.cargo_toml import cargo_package_name
from pipeline_core.domain.models import BuildCluster, FnRecord

# Profraw sits under the world's task 0d target directory. A path beside
# `target/` would be harvested as source; `t0d_target_dir` is the directory
# cargo was given, and grcov has to read the binaries written there.
COVERAGE_TAIL = Path("coverage") / "behaviour_exercises"


def grcov_binary() -> Path | None:
    found = shutil.which("grcov")
    if found:
        return Path(found)
    cargo_bin = Path.home() / ".cargo" / "bin" / "grcov"
    return cargo_bin if cargo_bin.exists() else None


def instrumented_env(cluster: BuildCluster | None,
                      project_dir: Path | None = None) -> dict[str, str]:
    env = probe_env(cluster, project_dir)
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -C instrument-coverage").strip()
    env["CARGO_INCREMENTAL"] = "0"
    return env


def grcov_command(grcov: Path, profraw_dir: Path, project_dir: Path,
                  out_json: Path, *, binary_path: Path | None = None) -> list[str]:
    """grcov over one profraw dir → coveralls JSON at `out_json`; the base
    flag set of `coverage._grcov_cmd`, so the hit lines agree with the crate
    readings (no cluster exclusions: a spec's hits map onto fn records, not a
    denominator). `binary_path` is the binary that wrote the profraw, or a
    directory when cargo named none; omitted, it is `target/debug` under the
    project."""
    binary = binary_path if binary_path is not None else (project_dir / "target" / "debug")
    return [
        str(grcov), str(profraw_dir),
        "--binary-path", str(binary),
        "--source-dir", str(project_dir),
        "--output-type", "coveralls",
        "--token", "unused",
        "--branch",
        "--ignore-not-existing",
        "--output-path", str(out_json),
        "--ignore", str(Path.home() / ".cargo" / "registry" / "*"),
        "--ignore", str(Path.home() / ".cargo" / "git" / "*"),
        "--ignore", str(Path.home() / ".rustup" / "*"),
        "--ignore", "*/target/*",
        "--ignore", "*/tests/*",
        "--ignore", "tests/*",
        "--ignore", "*/examples/*",
        "--ignore", "build.rs",
        "--ignore", "*.gen_tests_*.rs",
    ]


def _record_dict(r: FnRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(r, Mapping):
        return dict(r)
    return {"file": r.file, "fn_name": r.fn_name,
            "sig_byte_start": r.sig_byte_start, "body_byte_end": r.body_byte_end,
            "is_test": r.is_test}


def _crate_name(project_dir: Path) -> str:
    """The package name from the crate's `Cargo.toml`, or "" when unreadable."""
    try:
        return cargo_package_name(
            (Path(project_dir) / "Cargo.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""


def exercises_by_coverage(
    project_dir: Path,
    cluster: BuildCluster | None,
    specs: Iterable[BehaviourSpec],
    fn_records: Iterable[FnRecord | Mapping[str, Any]],
    *,
    run: Runner | None = None,
    log: Callable[[str], None] = lambda _m: None,
    max_specs: int = 200,
    grcov_bin: Path | None = None,
    timeout: int = 600,
    sites_out: dict[str, frozenset[str]] | None = None,
) -> tuple[dict[str, frozenset[str]], int]:
    """`behaviour_id → {"src/x.rs::fn", …}` plus the number of cargo/grcov
    invocations this call made (the instrumented build + per-spec run +
    per-spec grcov). A spec whose run fails, or whose coverage reaches no
    recorded fn, is absent from the map (never an empty set — "unknown"
    and "reaches nothing" must stay distinguishable).
    `fn_records` are the extractor's records for the crate's `src/`; their
    `file` may be `src/x.rs` (enumerated from the crate root) or `x.rs`
    (from `src/`) — both are joined on the crate-root shape."""
    runner = run or subprocess_runner
    project_dir = Path(project_dir)
    grcov = grcov_bin or grcov_binary()
    if grcov is None:
        log("  [behaviour-exercises] grcov not found — exercises not filled "
            "(`cargo install grcov`; `rustup component add llvm-tools-preview`).")
        return {}, 0
    all_specs = list(specs)
    n_doc = sum(1 for s in all_specs if is_doctest_spec(s))
    runnable = [s for s in all_specs
                if is_test_assertion_spec(s) or is_probe_spec(s)][:max_specs]
    if n_doc:
        log(f"  [behaviour-exercises] {n_doc} doc-test spec(s) skipped: "
            "doc-test instrumentation needs the nightly RUSTDOCFLAGS path.")
    if not runnable:
        return {}, 0
    invocations = 0

    def counted(
        cmd: list[str], cwd: Path, env: dict[str, str], timeout: int,
    ) -> tuple[int, str, str]:
        nonlocal invocations
        invocations += 1
        return runner(cmd, cwd, env, timeout)

    # The crate name for the rendered probe program's `use <crate>::*;`. A
    # capture in the constrained shape stores SETUP statements, and the setup
    # the model wrote compiled under that import — re-running it without one
    # fails to build and the spec is silently "not attributed".
    crate = _crate_name(project_dir)
    env = instrumented_env(cluster, project_dir)
    world_target = t0d_target_dir(project_dir, cluster)
    cov_root = world_target / COVERAGE_TAIL
    cov_root.mkdir(parents=True, exist_ok=True)
    build_env = dict(env)
    build_env["LLVM_PROFILE_FILE"] = str(cov_root / "build-%p-%m.profraw")
    rc, build_out, err = counted(
        ["cargo", "test", "--no-run", "--quiet", "--message-format=json"],
        project_dir, build_env, timeout)
    if rc != 0:
        log(f"  [behaviour-exercises] instrumented build failed (rc={rc}): "
            f"{err[-800:]}")
        return {}, invocations
    built = executables_from_messages(build_out)
    unnamed = 0
    recs = normalize_record_files(_record_dict(r) for r in fn_records)
    texts: dict[str, str] = {}
    out: dict[str, frozenset[str]] = {}
    for i, spec in enumerate(runnable):
        profraw_dir = cov_root / f"spec_{i:03d}"
        if profraw_dir.exists():
            shutil.rmtree(profraw_dir, ignore_errors=True)
        profraw_dir.mkdir(parents=True, exist_ok=True)
        spec_env = dict(env)
        spec_env["LLVM_PROFILE_FILE"] = str(profraw_dir / "%p-%m.profraw")
        cmd = test_command(spec)
        binary: str | None = None
        if cmd is not None:
            rc, _out, err = counted(cmd, project_dir, spec_env, timeout)
            target_of = test_target_of(spec)
            binary = built.get(("test", target_of[0])) if target_of else None
        else:
            # `program_for`, not `spec.setup`: a capture in the constrained
            # shape stores SETUP statements, not a whole program, so passing
            # `given` straight to `cargo run --example` would fail every probe
            # here with `rc != 0` -> "not attributed" -> continue, and the only
            # trace would be a log line. `program_for` returns the stored
            # program for a legacy free-form capture and renders one from
            # (given, when, kind) for the constrained shape.
            with materialized_probe(project_dir, program_for(spec, crate=crate),
                                    f"exercise_{i}") as target:
                rc, run_out, err = counted(
                    ["cargo", "run", "--quiet", "--message-format=json",
                     "--example", target],
                    project_dir, spec_env, timeout)
            binary = executables_from_messages(run_out).get(("example", target))
        if rc != 0:
            log(f"  [behaviour-exercises] {spec.behaviour_id}: run failed "
                f"(rc={rc}) — not attributed: {err[-300:].strip()}")
            continue
        if not list(profraw_dir.glob("*.profraw")):
            log(f"  [behaviour-exercises] {spec.behaviour_id}: no profraw written")
            continue
        out_json = profraw_dir / "coverage.json"
        if binary is None:
            # Correct and slow: grcov finds the binary among every one there.
            unnamed += 1
        rc, _out, err = counted(
            grcov_command(grcov, profraw_dir, project_dir, out_json,
                          binary_path=Path(binary) if binary else world_target / "debug"),
            project_dir, spec_env, 120)
        if rc != 0 or not out_json.exists():
            log(f"  [behaviour-exercises] {spec.behaviour_id}: grcov failed "
                f"(rc={rc}): {err[-300:].strip()}")
            continue
        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log(f"  [behaviour-exercises] {spec.behaviour_id}: unreadable coverage: {e}")
            continue
        hits = {rel: lines for rel, lines in coveralls_hit_lines(data).items()
                if rel.startswith(SRC_PREFIX)}
        for rel in hits:
            if rel not in texts:
                try:
                    texts[rel] = (project_dir / rel).read_text(encoding="utf-8")
                except OSError:
                    continue
        units = units_for_hit_lines(hits, texts, recs)
        if units:
            out[spec.behaviour_id] = units
        if sites_out is not None:
            # The same hits by SITE: which `fmt` of a file ran, which the
            # name-keyed `exercises` above cannot say.
            sites = sites_for_hit_lines(hits, texts, recs)
            if sites:
                sites_out[spec.behaviour_id] = sites
    log(f"  [behaviour-exercises] attributed {len(out)} / {len(runnable)} "
        f"runnable spec(s) to units by coverage ({invocations} cargo/grcov "
        "invocation(s)).")
    if unnamed:
        log(f"  [behaviour-exercises] HEALTH ‼ cargo named no binary for {unnamed} "
            f"spec(s); grcov read the whole {world_target / 'debug'} for those.")
    return out, invocations
