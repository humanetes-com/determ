"""Measure the ORIGINAL crate once, so the done-criterion is a floor the crate
itself meets rather than an ideal it does not.

The reference tree costs nothing to obtain: `workspace.py` clones the repo (or
copies a local path and commits `initial: source snapshot`) and then works on
branch `pipeline`, so the pristine upstream commit is still sitting in the same
repository on `master` — `tmp/anyhow`'s is `1dbe1862 Release 1.0.104`. A `git
worktree add` of that ref materialises it, survives the original source root
being moved or deleted, and is thrown away after the measurement.

Measured cost on `anyhow`, 2026-08-06: 0.4 s for the `stable` cluster and 1.1 s
for `nightly`, against a ~150 s model call — so this runs per pipeline run, not
per iteration, and re-runs whenever :func:`reference_key` changes.

**Where this must NOT live.** The worktree and `reference.json` are written
outside `project_dir`, because everything that builds a prompt sweeps inside it
(`collect_source_context`, `load_api_surface`, the artifact walks). `reference.json`
holds a pass/fail vector and a set of fn NAMES — no bodies, no source — but the
worktree is pristine upstream source in full, and one careless `rglob("*.rs")`
over the wrong parent turns the reconstruction into a copy. Keeping both a
sibling of `project_dir` rather than a child is what makes that mistake
structurally impossible instead of merely discouraged.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.cluster_env import overlay_cluster_env, world_env_extra
from ..domain.measurement import combine_source_hashes
from ..domain.reference_floor import (
    ClusterReference,
    ReferenceMeasurement,
    pairs_of,
)
from ..domain.test_results import find_unreported_tests, parse_binary_results
from ..log import emit, log

# Schema version of `reference.json`. Bumping it invalidates every stored
# measurement — do that when the MEANING of a field changes, not when a field is
# added, so an older file that is still correct is not thrown away for free.
_SCHEMA = 2


@dataclass(frozen=True, slots=True)
class ReferenceDeps:
    """Collaborators the measurement needs, injected by the composition root."""

    cargo_env: Callable[[], dict[str, str]]
    parse_cmd: Callable[[str], list[str]]
    # `stub_scan.scan_unimplemented_with_cfg`'s extractor seam — the syn-based
    # enumerator, so hollow bodies are found by AST and not by grepping for the
    # macro name (`src/error.rs` alone carries 6 `unimplemented!()` tokens inside
    # doc comments, none of them a hollow body).
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None]
    test_timeout: int = 900
    # Same contract as Task3Deps.cluster_env: the env for ONE named cluster.
    # Bound to cluster_cargo_env in composition. None builds it from
    # cargo_env() through the same overlay rule, for tests that do not bind it.
    cluster_env: Callable[..., dict[str, str]] | None = None


def reference_dir(project_dir: Path) -> Path:
    """The sibling directory holding the worktree and `reference.json`.

    A SIBLING, never a child: see the module docstring. `tmp/anyhow` →
    `tmp/.reference_anyhow`.

    ABSOLUTE, always. `git worktree add` runs with `cwd=project_dir` and
    resolves a relative destination against THAT, so a relative
    `tmp/.reference_anyhow/tree` checks out into
    `tmp/anyhow/tmp/.reference_anyhow/tree` — inside the project directory,
    which is the one place this must never be, and the subsequent `cargo test`
    then runs in a directory that does not exist.
    """
    resolved = project_dir.resolve()
    return resolved.parent / f".reference_{resolved.name}"


def _toolchain_id(toolchain: str | None) -> str:
    """`rustc -vV` for one cluster's toolchain, or "" when it cannot be read.

    This is the half of the key that flips `ui`. `tests/ui/*.stderr` are
    snapshots of rustc's own diagnostics, and upstream regenerates them per
    nightly — `anyhow`'s history reads "Update ui test suite to
    nightly-2025-09-07", "…2025-06-18", "…2024-03-31". A toolchain bump changes
    what the reference itself scores on those tests, so a measurement taken
    under another rustc must never be reused.
    """
    cmd = ["rustc"] + ([f"+{toolchain}"] if toolchain else []) + ["-vV"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


def _tests_fingerprint(tree: Path) -> str:
    """Content hash of the reference tree's own test files.

    `source_ref` already implies it for a git ref, but a `--project` run can be
    pointed at a working tree whose tests were edited in place, and a reference
    whose oracle silently changed underneath a cached measurement is the exact
    failure mode `reference.json` exists to prevent.
    """
    entries: list[tuple[str, bytes]] = []
    tests = tree / "tests"
    if tests.is_dir():
        for p in sorted(tests.rglob("*")):
            if p.is_file():
                try:
                    entries.append((str(p.relative_to(tree)), p.read_bytes()))
                except OSError:
                    continue
    return combine_source_hashes(entries)


def reference_key(clusters: Sequence[Any], source_ref: str,
                  tests_fingerprint: str) -> str:
    """The identity of a reference measurement. Any change re-measures."""
    h = hashlib.sha256()
    h.update(f"v{_SCHEMA}\0{source_ref}\0{tests_fingerprint}\0".encode())
    for c in sorted(clusters, key=lambda x: str(x.name)):
        h.update(str(c.name).encode())
        h.update(b"\0")
        h.update(_toolchain_id(getattr(c, "toolchain", None)).encode())
        h.update(b"\0")
        # The env the cluster's cargo actually runs under, not the stored
        # `env_extra`: a change to what the overlay delivers (the cfg reaching
        # rustdoc, 2026-10-02) changes what every doc test of the reference
        # measured while `env_extra` stays byte-identical, and a key that
        # missed it would reuse the stale vector.
        h.update(json.dumps(world_env_extra(getattr(c, "env_extra", None)),
                            sort_keys=True).encode())
        h.update(b"\0")
    return h.hexdigest()[:16]


def pristine_ref(project_dir: Path) -> str:
    """The commit the workspace was cloned from, still reachable in-repo.

    `setup_workspace` leaves upstream on `master` and moves work to `pipeline`,
    so `master` IS the pristine tree. Falls back to the remote head, then to the
    root commit — a `--project` workspace built by `initial: source snapshot`
    has exactly one commit before the pipeline's own.
    """
    for ref in ("master", "main", "origin/HEAD", "origin/master", "origin/main"):
        try:
            out = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                                 cwd=project_dir, capture_output=True, text=True, errors="replace",
                                 timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
        if out.returncode == 0 and (out.stdout or "").strip():
            return ref
    try:
        out = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"],
                             cwd=project_dir, capture_output=True, text=True, errors="replace",
                             timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (out.stdout or "").strip().splitlines()
    return first[0] if out.returncode == 0 and first else ""


def _add_worktree(project_dir: Path, ref: str, dest: Path) -> bool:
    if dest.exists():
        _remove_worktree(project_dir, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        out = subprocess.run(
            ["git", "worktree", "add", "--detach", str(dest), ref],
            cwd=project_dir, capture_output=True, text=True, errors="replace", timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  [reference] worktree add failed: {exc}")
        return False
    if out.returncode != 0:
        log(f"  [reference] worktree add failed: "
            f"{(out.stderr or out.stdout or '').strip().splitlines()[:1]}")
        return False
    return True


def _remove_worktree(project_dir: Path, dest: Path) -> None:
    """Drop the worktree AND its source. A pristine copy of the crate left on
    disk next to a reconstruction is the one artefact that can quietly turn this
    pipeline into a copier, so removal is unconditional and never deferred."""
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["git", "worktree", "remove", "--force", str(dest)],
                       cwd=project_dir, capture_output=True, text=True, errors="replace", timeout=120)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["git", "worktree", "prune"], cwd=project_dir,
                       capture_output=True, text=True, errors="replace", timeout=120)


def _measure_cluster(tree: Path, cluster: Any, deps: ReferenceDeps) -> ClusterReference:
    cmd_parts = ["cargo"]
    if getattr(cluster, "toolchain", None):
        cmd_parts.append(f"+{cluster.toolchain}")
    cmd_parts += ["test", "--no-fail-fast"]
    env = deps.cluster_env(cluster) if deps.cluster_env is not None else deps.cargo_env()
    if deps.cluster_env is None:
        overlay_cluster_env(env, cluster)
    t0 = time.monotonic()
    try:
        res = subprocess.run(deps.parse_cmd(" ".join(cmd_parts)), cwd=tree,
                             capture_output=True, text=True, errors="replace",
                             timeout=deps.test_timeout, env=env)
        rc = res.returncode
        output = (res.stdout or "") + "\n" + (res.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  [reference/{cluster.name}] did not run: {exc}")
        return ClusterReference(cluster=str(cluster.name), rc=-1)
    gap = find_unreported_tests(output)
    passed, failed, ignored, incomplete = pairs_of(parse_binary_results(output))
    ref = ClusterReference(
        cluster=str(cluster.name), passed=passed, failed=failed, ignored=ignored,
        incomplete=incomplete, announced=gap.announced, reported=gap.reported, rc=rc)
    log(f"  [reference/{cluster.name}] {len(passed)} passing, {len(failed)} failing, "
        f"{len(ignored)} ignored (rc={rc}, {time.monotonic() - t0:.1f}s)")
    return ref


def _hollow_fns(tree: Path, deps: ReferenceDeps) -> frozenset[tuple[str, str]]:
    """`(file, fn_name)` the ORIGINAL leaves `unimplemented!()`/`todo!()`.

    On `anyhow` this is `src/nightly.rs`'s `Debug::fmt` and `Display::fmt` for
    the build probe's `MyError` — upstream writes the stub itself, and the
    reconstruction reproduces both byte-for-byte. A gate that cannot see this
    fails a run over two functions it got exactly right.
    """
    from .reconstruct.stub_scan import scan_stub_inventory
    src = tree / "src"
    if not src.is_dir():
        return frozenset()
    return frozenset(
        (loc.file, loc.fn_name)
        for loc in scan_stub_inventory(
            src, enumerate_fns=deps.enumerate_fns).units)


def _store_path(project_dir: Path) -> Path:
    return reference_dir(project_dir) / "reference.json"


def _to_json(m: ReferenceMeasurement) -> dict[str, Any]:
    return {
        "schema": _SCHEMA, "key": m.key, "source_ref": m.source_ref,
        "hollow": sorted([f, n] for f, n in m.hollow),
        "clusters": {
            name: {
                "passed": sorted([a, b] for a, b in c.passed),
                "failed": sorted([a, b] for a, b in c.failed),
                "ignored": sorted([a, b] for a, b in c.ignored),
                "incomplete": sorted(c.incomplete),
                "announced": c.announced, "reported": c.reported, "rc": c.rc,
            } for name, c in sorted(m.clusters.items())
        },
    }


def _from_json(raw: dict[str, Any]) -> ReferenceMeasurement | None:
    if int(raw.get("schema", 0)) != _SCHEMA:
        return None
    clusters: dict[str, ClusterReference] = {}
    for name, c in (raw.get("clusters") or {}).items():
        clusters[str(name)] = ClusterReference(
            cluster=str(name),
            passed=frozenset((str(a), str(b)) for a, b in c.get("passed", [])),
            failed=frozenset((str(a), str(b)) for a, b in c.get("failed", [])),
            ignored=frozenset((str(a), str(b)) for a, b in c.get("ignored", [])),
            incomplete=frozenset(str(x) for x in c.get("incomplete", [])),
            announced=int(c.get("announced", 0)),
            reported=int(c.get("reported", 0)), rc=int(c.get("rc", 0)))
    return ReferenceMeasurement(
        key=str(raw.get("key", "")), clusters=clusters,
        hollow=frozenset((str(f), str(n)) for f, n in raw.get("hollow", [])),
        source_ref=str(raw.get("source_ref", "")))


def load_reference(project_dir: Path) -> ReferenceMeasurement | None:
    path = _store_path(project_dir)
    if not path.is_file():
        return None
    try:
        return _from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def measure_reference(project_dir: Path, clusters: Sequence[Any],
                      deps: ReferenceDeps, *,
                      force: bool = False) -> ReferenceMeasurement | None:
    """Measure (or reuse) the pristine tree's verdict vector for every baked
    cluster.

    Returns None when no reference can be had — no git repo, no pristine ref, a
    worktree that will not check out, a tree that will not build here. The
    caller then falls back to the absolute criterion and SAYS SO in the log: a
    silently-missing reference and a silently-broken one both lower the bar
    invisibly, and that is the one way this mechanism can do harm.
    """
    if not clusters:
        return None
    ref_name = pristine_ref(project_dir)
    if not ref_name:
        log("  [reference] no pristine ref in the workspace repo — the done-"
            "criterion stays absolute (every test passes, no reachable stub).")
        return None
    dest = reference_dir(project_dir) / "tree"
    if not _add_worktree(project_dir, ref_name, dest):
        log("  [reference] could not materialise the pristine tree — the done-"
            "criterion stays absolute.")
        return None
    try:
        key = reference_key(clusters, ref_name, _tests_fingerprint(dest))
        cached = load_reference(project_dir)
        if cached is not None and cached.key == key and not force:
            log(f"  [reference] reusing measurement {key} of '{ref_name}' "
                f"({len(cached.clusters)} cluster(s)).")
            return cached
        log(f"  [reference] measuring pristine '{ref_name}' under "
            f"{len(clusters)} baked cluster(s) — key {key}.")
        measured = ReferenceMeasurement(
            key=key, source_ref=ref_name,
            clusters={str(c.name): _measure_cluster(dest, c, deps)
                      for c in sorted(clusters, key=lambda x: str(x.name))},
            hollow=_hollow_fns(dest, deps))
    finally:
        _remove_worktree(project_dir, dest)

    why = measured.unhealthy_reason
    if why:
        log(f"  [reference] UNUSABLE as a floor — {why}. Falling back to the "
            "absolute criterion (every test passes, no reachable stub). The "
            "run's verdict is unaffected; it is only stricter than it needs "
            "to be.")
        emit("reference.unusable", data={"key": key, "reason": why})
        return None

    path = _store_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_to_json(measured), indent=2), encoding="utf-8")
    for name, c in sorted(measured.clusters.items()):
        log(f"  [reference/{name}] floor: {len(c.passed)} test(s) must pass; "
            f"{len(c.neutral)} neutral (the original fails or skips them)"
            + (f"; neutral: {_names(c.failed)}" if c.failed else ""))
    if measured.hollow:
        log(f"  [reference] the original leaves {len(measured.hollow)} fn(s) "
            f"hollow: {', '.join(f'{f}::{n}' for f, n in sorted(measured.hollow))}")
    emit("reference.measured", data={
        "key": key, "source_ref": ref_name,
        "hollow": sorted(f"{f}::{n}" for f, n in measured.hollow),
        "clusters": {n: {"passed": len(c.passed), "failed": len(c.failed),
                         "ignored": len(c.ignored), "announced": c.announced}
                     for n, c in measured.clusters.items()}})
    return measured


def _names(pairs: Iterable[tuple[str, str]], cap: int = 8) -> str:
    shown = sorted(f"{a}::{b}" for a, b in pairs)
    return ", ".join(shown[:cap]) + (f", … (+{len(shown) - cap})"
                                     if len(shown) > cap else "")


def run_test_file_on_original(project_dir: Path, rel_test: str, text: str, *,
                              env: dict[str, str], timeout: int,
                              prepare: Callable[[Path], object] | None = None,
                              target_name: str = "pin-target",
                              then: Callable[[Path, dict[str, str], str], object]
                              | None = None,
                              ) -> tuple[bool, str]:
    """Run ONE test file against the pristine crate: materialise the
    worktree, write `rel_test` into it, `cargo test --test <stem>` under
    `env`, and remove the worktree whatever happens. Returns (the run
    finished, its output). Only the verdict leaves this function: the
    worktree, its source and the file are gone before it returns.

    The end gate's pinning pass (`application/pinning_tests`) asks the
    original whether what a candidate asserts is the crate's behaviour. The
    target dir is a sibling of the worktree, outside `project_dir`, and kept
    across calls: the original's dependencies build once per world.

    `prepare(worktree)` runs after the file is written and before cargo: the
    pass's observation check empties a body there (the checkout gives every
    file a fresh mtime, so the crate is rebuilt from what is on disk).

    `then(worktree, env, output)` runs after a finished run, in the same
    worktree and env, before it is removed: a further reading of the same
    file (each test alone, `adapters.pin_runs.run_on_original`) without
    materialising the tree twice."""
    ref_name = pristine_ref(project_dir)
    if not ref_name:
        return False, "no pristine ref in the workspace repo"
    dest = reference_dir(project_dir) / "pin-tree"
    if not _add_worktree(project_dir, ref_name, dest):
        return False, "the pristine tree could not be materialised"
    try:
        target = dest / rel_test
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if prepare is not None:
            prepare(dest)
        run_env = dict(env)
        run_env["CARGO_TARGET_DIR"] = str(reference_dir(project_dir) / target_name)
        try:
            res = subprocess.run(
                ["cargo", "test", "--test", Path(rel_test).stem, "--",
                 "--test-threads=1"],
                cwd=dest, capture_output=True, text=True, errors="replace",
                timeout=timeout, env=run_env)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"did not run: {exc}"
        output = (res.stdout or "") + "\n" + (res.stderr or "")
        if then is not None:
            then(dest, run_env, output)
        return True, output
    finally:
        _remove_worktree(project_dir, dest)
