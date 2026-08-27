"""Exercises writer — fill `BehaviourSpec.exercises` from LINE COVERAGE of
each behaviour's run against the original crate (F10, the attribution
channel a MISMATCH escalates through).

No writer existed before 2026-08-26 (`layout_pins.py:235` says "the caller
fills it"; 30/30 specs on the anyhow suite carried it empty). This one does
ONE instrumented build (`-C instrument-coverage` layered on the cluster env,
`CARGO_INCREMENTAL=0` for the same reason `run_grcov` sets it: incremental
objects compiled without instrumentation yield no profraw), then per spec a
run with its own `LLVM_PROFILE_FILE` — a test-assertion spec through
`cargo test --test <stem> -- --exact <name>`, a probe spec by
re-materialising its program as an example — and grcov over that spec's
profraw alone (the same invocation `coverage.quick_file_coverage_grcov`
uses). The per-file hit lines map onto the extractor's fn records in the
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
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_coverage import (
    SRC_PREFIX,
    coveralls_hit_lines,
    is_doctest_spec,
    is_test_assertion_spec,
    normalize_record_files,
    test_command,
    units_for_hit_lines,
)
from pipeline_core.domain.behaviour_probe import is_probe_spec
from pipeline_core.domain.models import BuildCluster, FnRecord

COVERAGE_SUBDIR = Path("target") / "coverage" / "behaviour_exercises"


def grcov_binary() -> Path | None:
    found = shutil.which("grcov")
    if found:
        return Path(found)
    cargo_bin = Path.home() / ".cargo" / "bin" / "grcov"
    return cargo_bin if cargo_bin.exists() else None


def instrumented_env(cluster: BuildCluster | None) -> dict[str, str]:
    env = probe_env(cluster)
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -C instrument-coverage").strip()
    env["CARGO_INCREMENTAL"] = "0"
    return env


def grcov_command(grcov: Path, profraw_dir: Path, project_dir: Path,
                  out_json: Path) -> list[str]:
    """grcov over one profraw dir → coveralls JSON at `out_json`; the flag set
    `quick_file_coverage_grcov` uses, so the two readings agree."""
    return [
        str(grcov), str(profraw_dir),
        "--binary-path", str(project_dir / "target" / "debug"),
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

    env = instrumented_env(cluster)
    cov_root = project_dir / COVERAGE_SUBDIR
    cov_root.mkdir(parents=True, exist_ok=True)
    build_env = dict(env)
    build_env["LLVM_PROFILE_FILE"] = str(cov_root / "build-%p-%m.profraw")
    rc, _out, err = counted(["cargo", "test", "--no-run", "--quiet"],
                            project_dir, build_env, timeout)
    if rc != 0:
        log(f"  [behaviour-exercises] instrumented build failed (rc={rc}): "
            f"{err[-800:]}")
        return {}, invocations
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
        if cmd is not None:
            rc, _out, err = counted(cmd, project_dir, spec_env, timeout)
        else:
            with materialized_probe(project_dir, spec.given, f"exercise_{i}") as target:
                rc, _out, err = counted(
                    ["cargo", "run", "--quiet", "--example", target],
                    project_dir, spec_env, timeout)
        if rc != 0:
            log(f"  [behaviour-exercises] {spec.behaviour_id}: run failed "
                f"(rc={rc}) — not attributed: {err[-300:].strip()}")
            continue
        if not list(profraw_dir.glob("*.profraw")):
            log(f"  [behaviour-exercises] {spec.behaviour_id}: no profraw written")
            continue
        out_json = profraw_dir / "coverage.json"
        rc, _out, err = counted(grcov_command(grcov, profraw_dir, project_dir, out_json),
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
    log(f"  [behaviour-exercises] attributed {len(out)} / {len(runnable)} "
        f"runnable spec(s) to units by coverage ({invocations} cargo/grcov "
        "invocation(s)).")
    return out, invocations
