"""Skeleton compile-check for the t1.5 layout gate.

t1.5 accepts a model-authored private layout ONLY if the rewritten skeleton
still compiles with ``unimplemented!()`` bodies — the exact invariant the baked
skeleton holds. This runs ``cargo check`` (lib only, no test targets) over the
per-cluster skeleton crate.

CONSERVATIVE BY DESIGN: any error, timeout, or non-zero exit returns ``False``.
A false negative merely leaves a component frozen (no-op, safe); a false
positive would splice a broken scaffold into t2a — so we never guess ``True``.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.domain.cluster_env import world_key
from pipeline_core.domain.layout_probe import (
    FIELD_PROBE_MARKERS,
    PIN_PROBE_MARKERS,
    install_probe_region,
    strip_probe_region,
)
from pipeline_core.domain.models import BuildCluster

_SKELETON_DIR = ".skeleton"


def skeleton_dir_for(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / cfg_cluster


def skeleton_src_dir_for(project_dir: Path, cfg_cluster: str) -> Path:
    return skeleton_dir_for(project_dir, cfg_cluster) / "src"


def _ensure_crate_manifest(project_dir: Path, skel_dir: Path) -> bool:
    """The per-cluster skeleton is just ``src/`` — give it the crate's
    ``Cargo.toml`` so cargo can build it as a lib. Idempotent."""
    dst = skel_dir / "Cargo.toml"
    if dst.exists():
        return True
    src = project_dir / "Cargo.toml"
    if not src.exists():
        return False
    try:
        shutil.copy(src, dst)
        return True
    except OSError:
        return False


_ERROR_EXCERPT_CHARS = 5000


def _check_lib(crate_dir: Path, env: dict[str, str], timeout: int) -> tuple[bool, str]:
    """``cargo check --lib`` in `crate_dir` under `env`; (ok, error excerpt)."""
    try:
        proc = subprocess.run(
            ["cargo", "check", "--lib", "--quiet"],
            cwd=crate_dir, env=env, capture_output=True, text=True,
            errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cargo check did not run: {exc}"
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or "")[:_ERROR_EXCERPT_CHARS]


def other_worlds(cluster: BuildCluster,
                 worlds: Sequence[BuildCluster]) -> list[BuildCluster]:
    """The worlds in `worlds` whose build of a crate differs from `cluster`'s
    (`world_key`): a world with the same flags and toolchain is the same
    build, not a second reading."""
    own = world_key(cluster)
    return [w for w in worlds if w.name != cluster.name and world_key(w) != own]


def _world_env(world: BuildCluster, crate_dir: Path) -> dict[str, str]:
    """`world`'s cargo env with its own target dir: another world's flags and
    toolchain in the home target would invalidate every fingerprint the home
    build relies on."""
    env = cluster_cargo_env(world)
    env["CARGO_TARGET_DIR"] = str(crate_dir / "target" / "worlds" / world.name)
    return env


def _world_failure(home: str, world: str, err: str) -> str:
    return (f"The crate compiles in cfg world `{home}` and FAILS in cfg world "
            f"`{world}` — one source, every world builds it:\n{err}")

def write_pin_probe(
    project_dir: Path, cfg_cluster: str, source: str | None,
) -> None:
    """Install (`source`) or remove (`None`) the layout-pin probe in the
    skeleton's crate root, so ``cargo check --lib`` decides the pins.

    The probe rides in ``lib.rs`` because it must be part of the LIB target —
    the pins are compile-time facts about crate-root types, and the gate never
    builds test targets. Install is strip-then-append, so it is idempotent and
    always removable regardless of what the authoring splice did to the file in
    between.

    The member-path probe does NOT come through here: it has to sit in the file
    that declares the type it borrows through, or a private field is out of its
    view (see :class:`LayoutGate`). Its region carries its own marker pair, so
    the two coexist in ``lib.rs`` when that is where the type is declared."""
    root = skeleton_src_dir_for(project_dir, cfg_cluster) / "lib.rs"
    try:
        current = root.read_text(encoding="utf-8")
    except OSError:
        return
    new = (strip_probe_region(current, PIN_PROBE_MARKERS) if source is None
           else install_probe_region(current, source, PIN_PROBE_MARKERS))
    if new != current:
        try:
            root.write_text(new, encoding="utf-8")
        except OSError:
            return


def compile_skeleton_check(
    project_dir: Path,
    cfg_cluster: str,
    *,
    cluster: BuildCluster | None,
    worlds: Sequence[BuildCluster] = (),
    timeout: int = 240,
) -> tuple[bool, str]:
    """Does the per-cluster skeleton crate pass ``cargo check`` (lib only) in
    its own world and in every other world of `worlds`? Plus
    a bounded compiler-error excerpt, so a failed layout-authoring attempt can
    feed the model WHAT failed instead of a bare "did not compile" (feedback
    fidelity). rustc puts the primary diagnostics first — keep the head.

    Static layout pins (repr, size, NPO, autotraits) are compile-gated by
    installing a probe into ``lib.rs`` (`write_pin_probe` /
    `domain.layout_probe`) BEFORE this check runs, so ``--lib`` is the
    owner. They are not a test-target compile: the pins live in ``tests/``
    as authored integration tests, and an absolute ``--all-targets`` gate
    would reject every stub skeleton. A probe that fails calibration on the
    frozen scaffold disables pin gating (fail-open); see
    `author_layout._calibrate_pin_probe`.

    Takes the whole :class:`BuildCluster`, never a loose env mapping: a dict of
    env vars cannot express "this cluster needs the nightly toolchain", so a
    mapping-shaped parameter silently drops it. Compiling anyhow's nightly
    cluster with its ``RUSTFLAGS=--cfg error_generic_member_access`` but the
    default toolchain switches the nightly-only blocks ON and then builds them
    on stable — E0554 at the crate root, every component, every attempt,
    whatever the model wrote. ``cluster_cargo_env`` is the one place that knows
    both halves; going through it keeps this gate compiling under the same
    conditions as the real build."""
    skel_dir = skeleton_dir_for(project_dir, cfg_cluster)
    if not (skel_dir / "src").is_dir():
        return False, "skeleton src/ missing"
    if not _ensure_crate_manifest(project_dir, skel_dir):
        return False, "crate manifest missing"
    if cluster is None:
        # Conservative + loud: guessing a toolchain here is what this signature
        # exists to prevent, and False merely leaves the component frozen.
        return False, (f"no baked cluster {cfg_cluster!r} — cannot determine the "
                       "toolchain/cfg the layout gate must compile under")
    ok, err = _check_lib(skel_dir, cluster_cargo_env(cluster), timeout)
    others = other_worlds(cluster, worlds)
    if not ok or not others:
        return ok, err
    # Every other world builds the layout too. A definition this gate
    # accepted after compiling one world is how two worlds ended with two
    # definitions of one private type and a registry to arbitrate them
    # (2026-08-06, `pass=0 fail=112`). The probe regions hold THIS world's
    # pins and member paths, which another world's cfg may not satisfy, so
    # they are out of the source for these builds and put back after; each
    # world checks its own pins when its own pass authors or adopts the
    # component.
    files = sorted((skel_dir / "src").rglob("*.rs"))
    saved: dict[Path, str] = {}
    try:
        for f in files:
            text = f.read_text(encoding="utf-8")
            bare = strip_probe_region(
                strip_probe_region(text, PIN_PROBE_MARKERS), FIELD_PROBE_MARKERS)
            if bare != text:
                saved[f] = text
                f.write_text(bare, encoding="utf-8")
        for world in others:
            w_ok, w_err = _check_lib(skel_dir, _world_env(world, skel_dir), timeout)
            if not w_ok:
                return False, _world_failure(cluster.name, world.name, w_err)
    except OSError as exc:
        return False, f"probe regions could not be set aside: {exc}"
    finally:
        for f, text in saved.items():
            f.write_text(text, encoding="utf-8")
    return True, ""


_FILLED_DIR = ".skeleton-filled"


def filled_dir_for(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _FILLED_DIR / cfg_cluster


def compile_filled_check(
    project_dir: Path,
    cluster: BuildCluster,
    *,
    worlds: Sequence[BuildCluster] = (),
    timeout: int = 240,
) -> tuple[bool, str]:
    """Does the cluster's FILLED tree pass ``cargo check`` (lib only) in its
    own world and in every other world of `worlds`?

    The same gate as :func:`compile_skeleton_check`, pointed at the tree the
    loop actually builds and scores (`.skeleton-filled/<cluster>`), and it
    exists for the proven-body ledger: a re-seed is the one write in this
    pipeline made of code nobody just wrote, so it is the one that has to prove
    the tree still builds before the sweep reads a 0 it cannot attribute to any
    unit (`nightly 0/124 for 8.7 h`, from a seed defect).

    ``--lib`` deliberately: the re-seed puts bodies back into the crate, and
    building the test targets as well would cost minutes per check inside a
    bisect. A body that compiles in the lib and breaks a test binary is caught
    by the very next sweep, which is a normal verdict move rather than a tree
    with no signal.

    Conservative in the same direction as its sibling: any error, timeout or
    non-zero exit returns False, which costs a re-seeded body and never a
    broken tree."""
    crate_dir = filled_dir_for(project_dir, cluster.name)
    if not (crate_dir / "src").is_dir():
        return False, "filled src/ missing"
    if not (crate_dir / "Cargo.toml").exists() and not _ensure_crate_manifest(
            Path(project_dir), crate_dir):
        return False, "crate manifest missing"
    ok, err = _check_lib(crate_dir, cluster_cargo_env(cluster), timeout)
    if not ok:
        return ok, err
    # The one tree is every world's crate: a re-seeded body that builds only
    # in the home world would be read by the next sweep as a broken world.
    for world in other_worlds(cluster, worlds):
        w_ok, w_err = _check_lib(crate_dir, _world_env(world, crate_dir), timeout)
        if not w_ok:
            return False, _world_failure(cluster.name, world.name, w_err)
    return True, ""
