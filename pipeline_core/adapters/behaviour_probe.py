"""Behaviour-probe runner — compile-and-run one model-authored program against
the ORIGINAL crate, in the pre-strip window, and capture its stdout (the
adapter half of `domain/behaviour_probe.py`).

Until 2026-08-26 there was no compile-and-run adapter in production:
`run_behaviour_oracle` takes an injected `run_spec` and the synthesis prompt
had no caller, so tier 3 could not exist. This runs a probe as a cargo
EXAMPLE: the program is written to `examples/__behaviour_probe_<name>.rs`,
`cargo run --quiet --example …` builds it against the crate under the
cluster's env/toolchain (`cluster_cargo_env`, the same mapping every other
cargo call here goes through), stdout is captured, and the
file is ALWAYS removed — `examples/` too when it did not exist before — so
the pre-strip tree the bake reads is byte-identical to what it was. A crate
with `autoexamples = false` will not discover the file; the run then fails
and the probe is dropped, which is the correct (self-filtering) outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.adapters.process import run_bounded
from pipeline_core.domain.behaviour_probe import ProbeResult
from pipeline_core.domain.enums import ProcessFailure
from pipeline_core.domain.models import BuildCluster

PROBE_PREFIX = "__behaviour_probe_"

# (cmd, cwd, env, timeout) → (returncode, stdout, stderr). Injected in tests;
# `subprocess_runner` in production.
Runner = Callable[[list[str], Path, dict[str, str], int], tuple[int, str, str]]


def subprocess_runner(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> tuple[int, str, str]:
    """The production :data:`Runner`: rc −1 on timeout / missing binary, with
    the partial output kept (it names what was running when the clock ran
    out). Timeout kills the process group (`cargo run --example` is the
    child; the example binary is the grandchild)."""
    r = run_bounded(cmd, cwd=cwd, env=env, timeout=float(timeout))
    if r.failure is ProcessFailure.BINARY_MISSING:
        return -1, "", r.stderr or "cargo did not run"
    return r.returncode, r.stdout, r.stderr


def safe_probe_name(name: str) -> str:
    """`name` reduced to `[A-Za-z0-9_]` so it is a valid example target."""
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    return cleaned or "probe"


def t0d_target_dir(project_dir: Path, cluster: BuildCluster | None) -> Path:
    """Cargo output for one task 0d world: `<project>/target/t0d-<world>`.

    The directory sits under `target`, whose name `discovery` skips exactly,
    so the next harvest does not read build output as source. A sibling
    named `target-t0d-<world>` matches the git exclude and is still walked.
    This is not set on `cluster_cargo_env`: task 2a reads that function and
    chooses its own target directory."""
    name = cluster.name if cluster is not None and cluster.name else "default"
    return Path(project_dir) / "target" / f"t0d-{name}"


def probe_env(cluster: BuildCluster | None, project_dir: Path | None = None) -> dict[str, str]:
    """`cluster_cargo_env`, plus this world's task 0d target directory when
    `project_dir` is given. Callers that build or run the original crate
    during task 0d pass the project; a caller that only wants the toolchain
    env leaves it out and shares no directory it does not already share."""
    env = cluster_cargo_env(cluster)
    if project_dir is not None:
        env["CARGO_TARGET_DIR"] = str(t0d_target_dir(project_dir, cluster))
    return env


@contextmanager
def materialized_probe(project_dir: Path, program: str, name: str) -> Iterator[str]:
    """Write `program` as the example `__behaviour_probe_<name>` under
    `project_dir/examples/` for the duration of the block, yielding the
    example target name. Removal is unconditional (try/finally); an
    `examples/` dir this call created is removed again when it is left
    empty."""
    examples = Path(project_dir) / "examples"
    existed = examples.is_dir()
    examples.mkdir(parents=True, exist_ok=True)
    target = f"{PROBE_PREFIX}{safe_probe_name(name)}"
    path = examples / f"{target}.rs"
    path.write_text(program, encoding="utf-8")
    try:
        yield target
    finally:
        with suppress(OSError):
            path.unlink()
        if not existed:
            with suppress(OSError):
                examples.rmdir()


def run_probe(
    project_dir: Path,
    cluster: BuildCluster | None,
    program: str,
    name: str,
    *,
    timeout: int = 180,
    run: Runner | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> ProbeResult:
    """Build + run `program` as a cargo example of the crate at `project_dir`
    under `cluster`'s env; `ok` iff the process exited 0. stdout is returned
    raw — the domain's `normalize_stdout` is applied where the Then is
    recorded, so the raw text stays available for the log."""
    runner = run or subprocess_runner
    env = probe_env(cluster, project_dir)
    if extra_env:
        env.update(extra_env)
    with materialized_probe(project_dir, program, name) as target:
        rc, out, err = runner(["cargo", "run", "--quiet", "--example", target], Path(project_dir), env, timeout)
    return ProbeResult(ok=(rc == 0), stdout=out, stderr=err)
