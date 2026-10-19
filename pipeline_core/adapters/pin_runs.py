"""The two runs a pinning-test candidate is judged on (adapter).

`domain/pinning_test.judge_pin` decides; this module takes the readings it
decides on:

- on the ORIGINAL crate, through `reference_run.run_test_file_on_original`
  (the pristine worktree, removed before the call returns);
- on the REBUILT tree: a staged copy of the workspace crate under its
  `target/` (which the workspace repo ignores and every source walk skips),
  so a candidate never touches the oracle before it is admitted. The copy
  takes the manifest files, `src/` and `tests/` with symlinks resolved; its
  own target dir per world is kept across calls so dependencies build once.

The original run can also take the pristine worktree with chosen bodies
emptied (`run_original_emptied`): the bytes between the body's braces are
dropped, located by the extractor's records of that worktree, and only for
a body that returns nothing (`{}` is a body it can take). The worktree is
removed before the call returns; only the verdict and the emptied sites
leave it.

A run that did not finish, or that reported no test, is `ran=False`.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..application.reference_run import run_test_file_on_original
from ..domain.attribution import canon_site
from ..domain.installable_fns import fn_decl_shape
from ..domain.models import FnSite
from ..domain.pinning_test import TestRun
from ..domain.test_report import (
    cap_at_line_boundary,
    head_with_cut_mark,
    parse_test_output,
)
from .process import run_bounded

__all__ = ["reading_of", "run_on_original", "run_on_rebuilt", "run_original_emptied",
           "PIN_STAGE_DIR"]

PIN_STAGE_DIR = "target/pin-stage"
_MANIFEST_FILES = ("Cargo.toml", "Cargo.lock", "build.rs", "rust-toolchain.toml",
                   "rust-toolchain")
_EXCERPT = 1500


def _leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def reading_of(output: str, *, finished: bool) -> TestRun:
    """The run's verdicts by leaf name, and an excerpt for the next prompt.

    Compiler output (the process did not finish, or no test was reported)
    keeps the tail, cut on a line boundary. A run that reported tests keeps
    the head of the failure bodies, and a line the bound crosses is kept up
    to it and marked as cut, so a long panic message is shortened, never
    lost. This is the only cut: the pin pass stores the reason built on this
    excerpt whole."""
    if not finished:
        return TestRun(False, excerpt=cap_at_line_boundary(output, _EXCERPT))
    report = parse_test_output(output)
    passed = frozenset(_leaf(n) for n in report.passed)
    failed = frozenset(_leaf(f.test_name) for f in report.failures)
    if not passed and not failed:
        return TestRun(False, excerpt=cap_at_line_boundary(output, _EXCERPT))
    words = "\n".join(f"{f.test_name}: {(f.body or '').strip()}"
                      for f in report.failures)
    return TestRun(True, passed, failed, head_with_cut_mark(words, _EXCERPT))


def run_on_original(project_dir: Path, module: str, stem: str, text: str,
                    world: Any, *, cluster_env: Callable[[Any], dict[str, str]],
                    timeout: int) -> TestRun:
    finished, output = run_test_file_on_original(
        project_dir, f"tests/{stem}.rs", text, env=cluster_env(world), timeout=timeout)
    return reading_of(output, finished=finished)


def _stage(project_dir: Path) -> Path:
    tree = project_dir / PIN_STAGE_DIR / "tree"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    # Copied with fresh mtimes, never `copy2`: the target dir is kept across
    # runs and cargo judges a source fresh by mtime, so a file restored with
    # its old mtime after an emptied run would reuse the emptied build.
    for name in _MANIFEST_FILES:
        src = project_dir / name
        if src.is_file():
            shutil.copy(src, tree / name)
    for d in ("src", "tests"):
        if (project_dir / d).is_dir():
            shutil.copytree(project_dir / d, tree / d, symlinks=False,
                            copy_function=shutil.copy,
                            ignore=shutil.ignore_patterns("target", "target-*"))
    return tree


def _empty_bodies(tree: Path, sites: Sequence[FnSite],
                  fn_records: Callable[[Path], Sequence[Mapping[str, Any]]],
                  ) -> frozenset[FnSite]:
    """Drop the body text of each of `sites` that returns nothing, keeping
    the braces. Returns the sites emptied (a site with no record, or one
    whose declaration returns a value or does not parse, is left alone)."""
    want = {(c.file, c.fn_name, "".join(c.container.split())): site
            for site, c in ((s_, canon_site(s_)) for s_ in sites)}
    spans: dict[str, list[tuple[int, int]]] = {}
    done: set[FnSite] = set()
    for r in fn_records(tree):
        c = canon_site(FnSite(str(r.get("file", "")), str(r.get("fn_name", "")),
                              str(r.get("container", "") or "")))
        site = want.get((c.file, c.fn_name, "".join(c.container.split())))
        if site is None:
            continue
        shape = fn_decl_shape(str(r.get("sig_text", "")))
        if shape is None or shape[2] not in ("", "()"):
            continue
        spans.setdefault(str(r.get("file", "")), []).append(
            (int(r.get("body_byte_start", 0)), int(r.get("body_byte_end", 0))))
        done.add(site)
    for rel, cuts in spans.items():
        path = tree / rel
        raw = path.read_bytes()
        for start, end in sorted(cuts, reverse=True):
            if 0 < start <= end <= len(raw):
                raw = raw[:start] + raw[end:]
        path.write_bytes(raw)
    return frozenset(done)


def _run_staged(project_dir: Path, stem: str, text: str, world: Any, *,
                cluster_env: Callable[[Any], dict[str, str]], timeout: int,
                stub_panic_sites: Callable[[str, Path], Mapping[FnSite, set[str]]],
                ) -> tuple[TestRun, dict[FnSite, frozenset[str]]]:
    tree = _stage(project_dir)
    (tree / "tests").mkdir(exist_ok=True)
    (tree / "tests" / f"{stem}.rs").write_text(text, encoding="utf-8")
    env = cluster_env(world)
    env["CARGO_TARGET_DIR"] = str(project_dir / PIN_STAGE_DIR
                                  / f"target-{getattr(world, 'name', 'world')}")
    run = run_bounded(["cargo", "test", "--test", stem, "--", "--test-threads=1"],
                      cwd=tree, env=env, timeout=float(timeout))
    output = (run.stdout or "") + "\n" + (run.stderr or "")
    reading = reading_of(output, finished=run.failure is None)
    panics = {canon_site(s): frozenset(_leaf(t) for t in tests)
              for s, tests in stub_panic_sites(output, tree).items()}
    return reading, panics


def run_on_rebuilt(project_dir: Path, module: str, stem: str, text: str,
                   world: Any, sites: Sequence[FnSite], *,
                   cluster_env: Callable[[Any], dict[str, str]], timeout: int,
                   stub_panic_sites: Callable[[str, Path], Mapping[FnSite, set[str]]],
                   ) -> tuple[TestRun, dict[FnSite, frozenset[str]]]:
    """The candidate on a staged copy of the rebuilt crate, and per site of
    `sites` the tests whose first panic is a stub panic inside it (resolved
    against the staged tree's own records)."""
    reading, panics = _run_staged(project_dir, stem, text, world,
                                      cluster_env=cluster_env, timeout=timeout,
                                      stub_panic_sites=stub_panic_sites)
    return reading, {site: panics.get(canon_site(site), frozenset()) for site in sites}


def run_original_emptied(project_dir: Path, module: str, stem: str, text: str,
                         world: Any, sites: Sequence[FnSite], *,
                         cluster_env: Callable[[Any], dict[str, str]], timeout: int,
                         fn_records: Callable[[Path], Sequence[Mapping[str, Any]]],
                         ) -> tuple[TestRun, frozenset[FnSite]]:
    """The candidate on the ORIGINAL crate with each of `sites` that returns
    nothing emptied, and the sites that were. No site emptied is no run."""
    emptied: set[FnSite] = set()

    def _prepare(tree: Path) -> None:
        emptied.update(_empty_bodies(tree, sites, fn_records))
        if not emptied:
            raise _NothingToEmpty

    # An emptied body leaves its imports and parameters unused, and a crate
    # that denies those lints would not build: lints are not behaviour, so
    # they are capped at warn. Its own target dir, so the flag change does
    # not rebuild the plain run's artifacts.
    env = dict(cluster_env(world))
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " --cap-lints=warn").strip()
    try:
        finished, output = run_test_file_on_original(
            project_dir, f"tests/{stem}.rs", text, env=env,
            timeout=timeout, prepare=_prepare, target_name="pin-target-emptied")
    except _NothingToEmpty:
        return TestRun(False, excerpt="no body of these returns nothing"), frozenset()
    return reading_of(output, finished=finished), frozenset(emptied)


class _NothingToEmpty(Exception):
    """Raised from the worktree hook to skip the cargo run (the worktree is
    removed by the runner's own `finally`)."""
