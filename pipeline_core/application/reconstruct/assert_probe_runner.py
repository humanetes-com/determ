"""Run the assertion probe against the cluster tree in a scratch crate.

`domain.assert_probe` derives, from a test file, a copy in which the named
tests print their assertions' operands instead of asserting. This module
puts that copy where cargo can build it without touching the oracle: a
persistent scratch crate outside the workspace (`<tmpdir>/determ_probe/
<project>/<cluster>`) holding a copy of the cluster's `Cargo.toml`,
`Cargo.lock`, `src/` and the helper modules the test file pulls in, its own
target dir so the incremental build stays warm across sweeps and turns, and
one `tests/<probe>.rs` per test file. `cargo test --test <probe>` builds the
library and that one target only.

The oracle's `tests/` directory is never written (the cluster tree's
`tests` is a symlink into the project's write-protected oracle, and a
leftover probe file there would be a new test binary on the next sweep).

A probe that does not compile — an operand whose type is not `Debug` — is
retried once with the points rustc blamed removed; a probe that still
fails yields no observations and one log line, never an exception.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.assert_probe import (
    Observation,
    parse_probe_output,
    render_observations,
    rewrite_tests_for_probe,
    without_probe_points,
)
from pipeline_core.domain.oracle_ledger import bare_test_name

RunCargo = Callable[..., "tuple[int, str]"]

_MOD_LINE = re.compile(r"^\s*(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", re.M)
_ERR_LINE = re.compile(r"-->\s+tests/([A-Za-z0-9_]+)\.rs:(\d+):\d+")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    observations: Mapping[str, tuple[Observation, ...]]
    rendered: Mapping[str, str]
    note: str = ""


def probe_stage_dir(project_dir: Path, cluster: str) -> Path:
    root = Path(os.environ.get("ASSERT_PROBE_STAGE_ROOT") or tempfile.gettempdir())
    return root / "determ_probe" / project_dir.name / cluster


def _stage_tree(cluster_root: Path, project_dir: Path, stage: Path) -> None:
    """Refresh the scratch crate from the cluster tree: manifest, lock,
    toolchain file and `src/` copied whole (small); `tests/` starts empty
    and receives only probe files and the helper modules they name."""
    stage.mkdir(parents=True, exist_ok=True)
    for name in ("Cargo.toml", "Cargo.lock"):
        src = cluster_root / name
        if src.exists():
            shutil.copy2(src, stage / name)
    for name in ("rust-toolchain.toml", "rust-toolchain"):
        src = project_dir / name
        if src.exists():
            shutil.copy2(src, stage / name)
    dst_src = stage / "src"
    if dst_src.exists():
        shutil.rmtree(dst_src)
    shutil.copytree(cluster_root / "src", dst_src, symlinks=False)
    tests_dir = stage / "tests"
    tests_dir.mkdir(exist_ok=True)
    for old in tests_dir.glob("__probe_*.rs"):
        old.unlink()


def _copy_helper_modules(test_text: str, oracle_tests: Path, stage_tests: Path) -> None:
    """`mod common;` in a test file needs `tests/common.rs` or
    `tests/common/mod.rs` beside the probe."""
    for name in _MOD_LINE.findall(test_text):
        for cand in (oracle_tests / f"{name}.rs", oracle_tests / name):
            if cand.is_file():
                shutil.copy2(cand, stage_tests / cand.name)
            elif cand.is_dir():
                shutil.copytree(cand, stage_tests / name, dirs_exist_ok=True,
                                symlinks=False)


def _probe_env(cluster_env: Mapping[str, str] | None, stage: Path) -> dict[str, str]:
    env = dict(os.environ)
    if cluster_env:
        env.update(cluster_env)
    env["CARGO_TARGET_DIR"] = str(stage / "target")
    return env


def run_assert_probes(
    *,
    project_dir: Path,
    cluster_root: Path,
    cluster_name: str,
    cluster_env: Mapping[str, str] | None,
    tests_by_file: Mapping[Path, Sequence[str]],
    run_cargo: RunCargo,
    log: Callable[[str], None],
    timeout: int = 240,
) -> ProbeResult:
    """Observe the named tests' assertion operands on the tree as it is now.

    `tests_by_file` maps an oracle test FILE to the test fn names in it to
    probe. Returns the observations per test and their rendered
    `observed:` lines; a file whose probe cannot build contributes nothing.
    """
    if not tests_by_file:
        return ProbeResult({}, {})
    stage = probe_stage_dir(project_dir, cluster_name)
    try:
        _stage_tree(cluster_root, project_dir, stage)
    except OSError as exc:
        log(f"  [probe] staging failed ({type(exc).__name__}: {exc}) — no observations")
        return ProbeResult({}, {}, note=f"staging failed: {exc}")
    env = _probe_env(cluster_env, stage)
    observations: dict[str, tuple[Observation, ...]] = {}
    notes: list[str] = []
    for test_file, names in tests_by_file.items():
        names = [n for n in names if n]
        if not names:
            continue
        try:
            text = test_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stem = f"__probe_{test_file.stem}"
        _copy_helper_modules(text, test_file.parent, stage / "tests")
        excluded: set[tuple[str, int]] = set()
        for attempt in range(2):
            rewritten, points = rewrite_tests_for_probe(text, names)
            if excluded:
                rewritten, points = without_probe_points(text, names, excluded)
            if not points:
                break
            (stage / "tests" / f"{stem}.rs").write_text(rewritten, encoding="utf-8")
            cmd = ("cargo test --offline --test " + shlex.quote(stem)
                   + " -- --nocapture --test-threads=1 "
                   + " ".join(shlex.quote(n) for n in names))
            rc, out = run_cargo(cmd, stage, env, timeout)
            # A libtest filter is a substring match, so `test_as` also runs
            # `test_as_ref`; a helper's prints name whichever test ran it.
            # Only the tests asked about are observations of this sweep.
            wanted = {n.rsplit("::", 1)[-1] for n in names}
            found = {t: obs for t, obs in parse_probe_output(out).items()
                     if t.rsplit("::", 1)[-1] in wanted}
            if found:
                for t, obs in found.items():
                    observations[t] = tuple(obs)
                break
            blamed = _blamed_lines(out, stem)
            if attempt == 0 and blamed:
                excluded = {(p.test, p.line) for p in points if p.line in blamed}
                if excluded:
                    notes.append(f"{test_file.name}: {len(excluded)} probe point(s) "
                                 "dropped after rustc blamed them; rebuilt once")
                    continue
            notes.append(f"{test_file.name}: probe produced no observation "
                         f"(rc={rc})")
            break
    rendered = {t: render_observations(obs) for t, obs in observations.items()}
    if notes:
        log("  [probe] " + "; ".join(notes))
    return ProbeResult(observations, rendered, note="; ".join(notes))


def _blamed_lines(output: str, stem: str) -> set[int]:
    return {int(m.group(2)) for m in _ERR_LINE.finditer(output)
            if m.group(1) == stem}


def tests_by_file(names: Collection[str], test_files: Mapping[str, str],
                  project_dir: Path) -> dict[Path, list[str]]:
    """``names`` grouped under the oracle file that declares each (``test_files``:
    test name → file, as the oracle inventory indexed it), bare names, files
    that exist only. A name the index does not hold is left out."""
    out: dict[Path, list[str]] = {}
    for name in sorted(names):
        rel = test_files.get(name) or test_files.get(bare_test_name(name))
        if not rel:
            continue
        path = Path(rel)
        if not path.is_absolute():
            path = project_dir / path
        if path.is_file():
            out.setdefault(path, []).append(bare_test_name(name))
    return out


#: Observes the named tests' assertion operands on the cluster tree as it
#: stands, rendered per test (`ProbeResult.rendered`): bound by the TDD loop
#: for the fill, read by every regression gate (`cargo_runner.
#: test_regression_gate`) while a write it is about to revert is still on
#: the tree.
WriteProbe = Callable[[Collection[str]], Mapping[str, str]]
_bound: WriteProbe | None = None


def bind(probe: WriteProbe | None) -> None:
    """Bind (or, with ``None``, clear) the fill's write probe."""
    global _bound
    _bound = probe


def bound() -> WriteProbe | None:
    return _bound
