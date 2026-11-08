"""rustc's own view of one cluster world: generic MIR and the public index (adapter).

Two commands per world, both on the cluster's toolchain and env (so the world's
`--cfg` flags and `build.rs` probes are the ones its real build sees):

- `cargo rustc --lib -- -Zunpretty=mir`: the library's generic MIR on stdout;
- `cargo rustdoc --lib -- -Zunstable-options --output-format json
  --document-hidden-items`: the items an outside crate can reach, without
  `--document-private-items`.

Both are `-Z` flags. A stable toolchain accepts them under
`RUSTC_BOOTSTRAP=<crate>`, scoped to the library crate by name: the blanket
`RUSTC_BOOTSTRAP=1` would also reach `build.rs`'s compile probes, which would
then report a nightly compiler and change which cfgs the world sets (anyhow's
`build.rs` is exactly that). Run `+nightly` for a cluster that needs it.

The build runs in its own target directory under the tree, so it never
invalidates the fingerprints the test sweeps build in.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain.cargo_toml import cargo_package_name
from ..domain.fn_reachability import WorldReading
from .process import run_bounded


def lib_crate_name(project_dir: Path) -> str:
    try:
        text = (project_dir / "Cargo.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return cargo_package_name(text).replace("-", "_")


def measure_world_reachability(
    project_dir: Path,
    cluster: Any,
    *,
    cluster_env: Callable[[Any], dict[str, str]],
    timeout: int = 900,
    test_profile_mir: bool = False,
) -> WorldReading:
    """The MIR and the public index of the library at `project_dir` under
    `cluster`, or the reason there is no reading. With `test_profile_mir`
    also the library's MIR under `--profile test` (`cfg(test)`, the in-crate
    test modules compiled), which the end gate's removal guard reads."""
    name = str(getattr(cluster, "name", "?"))
    crate = lib_crate_name(project_dir)
    if not crate:
        return WorldReading(name, reason="no package name in Cargo.toml")
    env = cluster_env(cluster)
    env["RUSTC_BOOTSTRAP"] = crate
    target = project_dir / "target" / "fn-reach" / name
    env["CARGO_TARGET_DIR"] = str(target)
    prefix = ["cargo"]
    toolchain = getattr(cluster, "toolchain", None)
    if toolchain:
        prefix.append(f"+{toolchain}")
    mir = run_bounded([*prefix, "rustc", "--lib", "--", "-Zunpretty=mir"],
                      cwd=project_dir, env=env, timeout=float(timeout))
    if mir.failure is not None:
        return WorldReading(name, reason=f"cargo rustc did not finish: {mir.failure}")
    if mir.returncode != 0:
        tail = (mir.stderr or "").strip().splitlines()[-1:] or [""]
        return WorldReading(name, reason=f"cargo rustc rc={mir.returncode}: {tail[0][:200]}")
    doc = run_bounded(
        [*prefix, "rustdoc", "--lib", "--", "-Zunstable-options", "--output-format", "json",
         "--document-hidden-items"],
        cwd=project_dir, env=env, timeout=float(timeout))
    if doc.failure is not None:
        return WorldReading(name, reason=f"cargo rustdoc did not finish: {doc.failure}")
    if doc.returncode != 0:
        tail = (doc.stderr or "").strip().splitlines()[-1:] or [""]
        return WorldReading(name, reason=f"cargo rustdoc rc={doc.returncode}: {tail[0][:200]}")
    path = target / "doc" / f"{crate}.json"
    try:
        rustdoc = path.read_text(encoding="utf-8")
    except OSError as exc:
        return WorldReading(name, reason=f"rustdoc json not written: {exc}")
    mir_test = ""
    if test_profile_mir:
        run = run_bounded([*prefix, "rustc", "--lib", "--profile", "test", "--",
                           "-Zunpretty=mir"],
                          cwd=project_dir, env=env, timeout=float(timeout))
        if run.failure is not None:
            return WorldReading(
                name, reason=f"cargo rustc --profile test did not finish: {run.failure}")
        if run.returncode != 0:
            tail = (run.stderr or "").strip().splitlines()[-1:] or [""]
            return WorldReading(
                name, reason=f"cargo rustc --profile test rc={run.returncode}: {tail[0][:200]}")
        mir_test = run.stdout
    return WorldReading(name, mir=mir.stdout, rustdoc=rustdoc, mir_test=mir_test)
