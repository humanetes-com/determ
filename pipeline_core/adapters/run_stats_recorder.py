"""RunStatsRecorder — folds this process's events into per-phase cost rows.

Registered as an observer on the event emitter (`JsonlEventEmitter.observe`)
by the composition root, started by the orchestrator once it knows the
workspace. It owns three things the pure fold cannot:

* the run's identity — `resolve_code_identity` (the code that decides what a
  run does, the pre-run stamp and HEAD as landmarks) and the configuration
  (`RunConfig.from_env`, captured at launch, not after the run);
* the run's ORIGIN — the restore point the workspace stood at when the first
  top-level phase that actually runs entered (`workspace_tree_id`). A fresh
  run starts from the restore tag; a resume starts from whatever its restore
  point left. Two runs of a phase are comparable only from the same origin:
  everything after the first model-written phase differs run to run, so a
  per-phase input tree would never match, while the origin fixes the
  distribution those phases are drawn from;
* the sweeps — every `phase_c.tdd.iter` that measured, as one
  `domain.sweep_trajectory.SweepRow` with the clock and the model calls at
  that reading. The fill phase seldom closes inside a process, so its phase
  sample cannot say how the score climbed; the sweeps can;
* the cargo boundary — a `sys.addaudithook` on `subprocess.Popen`, the one
  place every spawned process passes, which emits `cargo.spawn` (classified
  by `domain.phase_cost.classify_cargo`) in the spawning thread's context, so
  the build and test runs are charged to the phase that started them. Cargo
  is spawned from some thirty call sites; counting at each would miss the
  next one.

Never raises into the pipeline: a store failure is logged once and recording
stops.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.measurement import code_identity, combine_source_hashes
from ..domain.phase_cost import (
    CARGO_SPAWN,
    CodeIdentity,
    PhaseCostFold,
    RunConfig,
    classify_cargo,
)
from ..domain.phase_registry import PHASE_ENTER
from ..domain.sweep_trajectory import sweep_of
from ..log import emit, log
from .run_stats_store import SQLiteRunStatsStore

_EXTRACTOR_DIR = Path("tools") / "skeleton_extractor"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# ── identity ────────────────────────────────────────────────────────────────


def _extractor_entries(root: Path) -> list[tuple[str, bytes]]:
    base = root / _EXTRACTOR_DIR
    out: list[tuple[str, bytes]] = []
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if "target" in p.relative_to(base).parts or not p.is_file():
            continue
        if p.suffix not in (".rs", ".toml"):
            continue
        with suppress(OSError):
            out.append((str(p.relative_to(root)), p.read_bytes()))
    return out


def resolve_code_identity(root: Path, *, commit: str | None = None) -> CodeIdentity:
    """The comparison key is `code_identity` (pipeline Python, the observer
    library left out) folded with the skeleton extractor's sources, which
    `code_identity` does not cover and which change what the bake does."""
    key = combine_source_hashes([("python", code_identity(root).encode("utf-8")),
                                 *_extractor_entries(root)])
    stamp: str | None = None
    with suppress(OSError, IndexError):
        stamp = (root / ".pre_run_stamp").read_text(encoding="utf-8").split()[0][:12]
    return CodeIdentity(key=key, tree_stamp=stamp,
                        commit=commit if commit and commit != "unknown" else None)


def _git(project_dir: Path, *args: str) -> bytes:
    # GIT_OPTIONAL_LOCKS=0: a read must not refresh the workspace's index
    # under the pipeline's own git operations.
    return subprocess.run(
        ["git", "-C", str(project_dir), *args], capture_output=True, timeout=60,
        check=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout


def workspace_tree_id(project_dir: Path) -> str:
    """The restore point the workspace stands at: the tree of its HEAD commit,
    folded with the bytes of every TRACKED file that differs from it (a
    deleted one as a marker). Read only — nothing is written into the
    workspace's object store or index.

    Untracked files are not part of a restore point and are left out. They
    were in, and no two runs ever shared an origin: of the 28 live runs
    recorded 2026-10-04 → 10-15, 28 distinct origins, four of them
    from-scratch restarts from the same restore tag. A restore resets the
    tracked files and leaves untracked ones behind, and on `anyhow`
    (2026-10-15) 231 of them were build output under a cluster tree's
    nested `target/`, which the workspace's root-only ignore does not cover —
    different after every build, so every run compared to nothing and the
    health tab printed `not_comparable` for every phase.

    No git, or no commit yet: the `*.rs` and `Cargo.toml` files under the
    directory (`target/` out). Missing: `absent`."""
    if not project_dir.is_dir():
        return "absent"
    if (project_dir / ".git").exists():
        try:
            tree = _git(project_dir, "rev-parse", "HEAD^{tree}").decode().strip()
            changed = [f for f in _git(project_dir, "diff", "--name-only", "-z",
                                       "--no-renames", "HEAD")
                       .decode("utf-8", "replace").split("\0") if f]
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            tree = ""
        if tree:
            entries: list[tuple[str, bytes]] = [("HEAD^{tree}", tree.encode())]
            for rel in sorted(changed):
                try:
                    entries.append((rel, (project_dir / rel).read_bytes()))
                except OSError:
                    entries.append((rel, b"\0deleted"))
            return combine_source_hashes(entries)
    files = [str(p.relative_to(project_dir)) for p in project_dir.rglob("*")
             if p.is_file() and "target" not in p.relative_to(project_dir).parts
             and ".git" not in p.relative_to(project_dir).parts
             and (p.suffix == ".rs" or p.name == "Cargo.toml")]
    walked: list[tuple[str, bytes]] = []
    for rel in sorted(files):
        with suppress(OSError):
            walked.append((rel, (project_dir / rel).read_bytes()))
    return combine_source_hashes(walked) if walked else "empty"


# ── the cargo boundary ──────────────────────────────────────────────────────

_hook_lock = threading.Lock()
_hook_installed = False
_hook_target: Callable[[Sequence[str]], None] | None = None
_in_hook = threading.local()


def _argv_of(args: object) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    if isinstance(args, Sequence):
        return [os.fsdecode(a) if isinstance(a, (str, bytes, os.PathLike)) else str(a)
                for a in args]
    return []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "subprocess.Popen":
        return
    target = _hook_target
    if target is None or getattr(_in_hook, "busy", False):
        return
    _in_hook.busy = True
    try:
        target(_argv_of(args[1] if len(args) > 1 else ()))
    except Exception:
        pass  # an audit hook that raises would abort the spawn
    finally:
        _in_hook.busy = False


def emit_cargo_spawn(argv: Sequence[str]) -> None:
    """The audit target: one `cargo.spawn` per process that runs cargo."""
    kinds = classify_cargo(argv)
    if kinds:
        emit(CARGO_SPAWN, data={"kinds": [k.value for k in kinds]})


def arm_cargo_counter(target: Callable[[Sequence[str]], None] | None) -> None:
    """Point the process-wide Popen audit hook at `target` (None disarms).
    The hook is installed once per process — Python cannot remove one."""
    global _hook_installed, _hook_target
    with _hook_lock:
        _hook_target = target
        if target is not None and not _hook_installed:
            sys.addaudithook(_audit)
            _hook_installed = True


# ── the recorder ────────────────────────────────────────────────────────────


class RunStatsRecorder:
    def __init__(self, store: SQLiteRunStatsStore, *, run_id: str, crate: str,
                 identity: CodeIdentity, config: RunConfig,
                 origin_probe: Callable[[], str],
                 argv: Sequence[str] | None = None,
                 log_fn: Callable[[str], None] = log) -> None:
        self._store = store
        self._run_id = run_id
        self._crate = crate
        self._identity = identity
        self._config = config
        self._probe = origin_probe
        self._argv = list(argv) if argv is not None else None
        self._log = log_fn
        self._fold = PhaseCostFold()
        self._lock = threading.RLock()
        self._origin_fixed = False
        self._failed = False
        self._started_at = datetime.now(UTC)
        self._live = False
        self._closed = False

    @property
    def run_id(self) -> str:
        return self._run_id

    def _disable(self, where: str, exc: BaseException) -> None:
        self._live = False
        self._log(f"  [run-stats] HEALTH ‼ recording stopped ({where}): "
                  f"{type(exc).__name__}: {exc}")

    def start(self) -> None:
        try:
            self._store.open_run(
                run_id=self._run_id, crate=self._crate, code_key=self._identity.key,
                tree_stamp=self._identity.tree_stamp, commit=self._identity.commit,
                config_fp=self._config.fingerprint, config=self._config.as_dict(),
                started=self._started_at.isoformat(timespec="milliseconds"),
                argv=self._argv)
            self._live = True
        except Exception as exc:
            self._disable("open run", exc)
            return
        self._log(f"  [run-stats] recording phase cost: code={self._identity.key} "
                  f"config={self._config.fingerprint} → {self._store.path}")

    def observe(self, record: dict[str, Any]) -> None:
        """Emitter observer. Serialised: records arrive from every thread."""
        if not self._live:
            return
        with self._lock:
            try:
                self._observe(record)
            except Exception as exc:
                self._disable("observe", exc)

    def _observe(self, record: Mapping[str, Any]) -> None:
        if (record.get("event") == PHASE_ENTER and record.get("scope")
                and not record.get("parent_scope") and not self._origin_fixed):
            # Re-probed at every top-level phase until one actually runs: a
            # resume skips its early phases, and the tree a skipped phase saw
            # is not the tree the run worked from.
            self._store.set_origin(self._run_id, self._probe())
        sample = self._fold.feed(record)
        sweep = sweep_of(record, run_id=self._run_id,
                         run_started=self._started_at.isoformat(timespec="milliseconds"),
                         llm_calls=self._fold.run_work.llm_calls)
        if sweep is not None:
            self._store.add_sweep(sweep)
        if sample is None:
            return
        if sample.top_level:
            if sample.outcome != "skipped":
                self._origin_fixed = True
            if sample.outcome in ("error", "aborted"):
                self._failed = True
        if sample.outcome != "skipped":
            self._store.add_sample(self._run_id, sample)

    def finish(self) -> None:
        """Close the run row. Called at interpreter exit; a signalled process
        never gets here and its row stays unfinished."""
        with self._lock:
            if not self._live or self._closed:
                return
            self._closed = True
            ended = datetime.now(UTC)
            try:
                self._store.close_run(
                    self._run_id, ended=ended.isoformat(timespec="milliseconds"),
                    outcome="error" if self._failed else "completed",
                    wall_ms=int((ended - self._started_at).total_seconds() * 1000),
                    work=self._fold.run_work)
            except Exception as exc:
                self._disable("close run", exc)


def start_run_stats(*, store_path: Path, observe: Callable[[Callable[[dict[str, Any]], None]], None],
                    root: Path, project_dir: Path, crate: str, run_id: str,
                    commit: str | None, env: Mapping[str, str],
                    argv: Sequence[str] | None = None) -> RunStatsRecorder | None:
    """Build, register and start this run's recorder. Never raises."""
    try:
        store = SQLiteRunStatsStore(store_path)
        recorder = RunStatsRecorder(
            store, run_id=run_id, crate=crate,
            identity=resolve_code_identity(root, commit=commit),
            config=RunConfig.from_env(env),
            origin_probe=lambda: workspace_tree_id(project_dir), argv=argv)
    except Exception as exc:
        log(f"  [run-stats] HEALTH ‼ not recording phase cost: "
            f"{type(exc).__name__}: {exc}")
        return None
    recorder.start()
    observe(recorder.observe)
    arm_cargo_counter(emit_cargo_spawn)
    atexit.register(recorder.finish)
    return recorder
