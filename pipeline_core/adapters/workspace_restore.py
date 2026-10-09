"""Restore a workspace to one of its recorded restore points.

Two restore points, one family:

* ``--restart-from-augmented`` → :class:`RestoreFromAugmented`: the POST-task0c
  point, tag ``task0c-augmented-<module>``. The generated tests come back from
  the tag; strip / task1+ re-run against them.
* ``--restart-from-pre-augment`` → :class:`RestoreFromPreAugment`: the
  PRE-task0c point. Nothing task0c generated survives and task0c itself
  re-runs — the lever for a changed test generator or admission rule, which
  before this needed ``pipeline.py --repo <URL> --clean``: a re-clone that
  bypasses continue.sh's pre-run gate and discards the task0a/0b outputs along
  with everything else. The commit is the behaviour restore point
  ``task0d-done-<module>`` (after task0d, which is slow and reads nothing
  task0c writes) when the workspace has one; ``--reharvest-behaviours``, or a
  workspace without it, goes back to ``task0c-pre-<module>`` (before task0d).

Both archive the reconstruction directories before the hard reset, describe
what the reset is about to discard, reset, wipe the artefacts the tag's tree
does not describe, and rewrite pipeline_state.md. The shared mechanics live on
:class:`_WorkspaceRestore`; each subclass owns its tag pattern, its wipe list
and its phase-keep rule (:mod:`pipeline_core.domain.workspace_layout`). The
composition root injects the state read/write port and the log sink.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ..domain.workspace_layout import (
    BEHAVIOUR_TAG_PREFIX,
    CLUSTER_AUGMENTED_DIR,
    FAILURE_LOG_NAME,
    MODULE_AUGMENTED_DIR,
    MODULE_STRIPPED_DIR,
    PRE_AUGMENT_TAG_PREFIX,
    RECONSTRUCTION_DIRS,
    TASK0C_ATTEMPTS_LOG_NAME,
    drop_iteration_phases,
    drop_pre_augment_phases,
)

# Archives land OUTSIDE project_dir, as a sibling. Two reasons, both learned:
# `git_commit`'s `git add -A` would sweep an in-workspace archive into the next
# task0c/task1 commit, and a later `--clean` re-clone is `shutil.rmtree(dest)`
# over the workspace. The reference floor already survives by the same trick
# (`tmp/.reference_anyhow/` is a sibling of project_dir).
ARCHIVE_DIR_SUFFIX = ".pre_restart"

AUGMENTED_TAG_PREFIX = "task0c-augmented-"


def list_git_tags(project_dir: Path, pattern: str) -> list[str]:
    """The workspace's tags matching a `git tag -l` glob, in git's order.
    Raises RuntimeError when the listing itself fails (not a git repo, …)."""
    try:
        result = subprocess.run(
            ["git", "tag", "-l", pattern],
            cwd=project_dir, capture_output=True, text=True, errors="replace", check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git tag listing failed: {e}") from e
    return [t for t in result.stdout.strip().splitlines() if t]


def _commit_of(project_dir: Path, ref: str) -> str:
    """The full commit sha a tag (lightweight or annotated) resolves to."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=project_dir, capture_output=True, text=True, errors="replace", check=True,
    )
    return result.stdout.strip()


def _iteration_artifact_paths(project_dir: Path, module_names: Iterable[str]) -> list[Path]:
    """The strip / task1+ artefacts both restores wipe — the ones the tag's
    tree does not describe and the pipeline would otherwise read as done."""
    paths: list[Path] = [
        project_dir / MODULE_STRIPPED_DIR,         # strip sentinel
        project_dir / FAILURE_LOG_NAME,
        project_dir / ".task1_partials",
    ]
    paths += [project_dir / f"functional_requirements_{m}.md" for m in module_names]
    paths += list(project_dir.glob(".generated_files_*.txt"))
    paths += list(project_dir.glob("tests_output_*.txt"))
    return paths


def _group_by_top_level(paths: Iterable[str]) -> str:
    """`.phase_c_logs/ (312), src/ (14), .task0c_attempts.jsonl` — one term per
    top-level entry, directories with their file count, largest first."""
    counts: Counter[str] = Counter()
    for p in paths:
        head, sep, _ = p.partition("/")
        counts[f"{head}/" if sep else head] += 1
    terms = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{name} ({n})" if name.endswith("/") else name for name, n in terms)


@dataclass(frozen=True, slots=True)
class _WorkspaceRestore:
    """The mechanics shared by every restore point: tag lookup, reconstruction
    archive, the described hard reset, explicit wipes and the state rewrite.
    Subclasses set `label` (the log prefix, which is also the `current` the
    state file is left with) and compose `run` from these."""

    label: ClassVar[str] = "restore"

    read_state: Callable[[Path], dict[str, Any]]
    write_state: Callable[[Path, dict[str, Any]], None]
    log: Callable[[str], None]

    def _archive_reconstruction(self, project_dir: Path) -> Path | None:
        """Move the reconstruction directories aside before the hard reset, and
        return where they went (None when there was nothing to archive).

        `git reset --hard <tag>` is a WHOLE-TREE rollback, and
        `.skeleton-filled`, `.skeleton` and `.best_tree` are tracked while the
        tag predates every fill — so the reset deletes them rather than leaving
        them alone. On 2026-08-08 that removed 5,137 files, 5,067 of them
        `.skeleton-filled`, taking a tree that had reached `pass=95 fail=15
        oracle=110` off disk between one run and the next. (The tree itself was
        still in the object store at the task2a checkpoint commit, but nobody
        knew: the checkpoint's message names a score and its artefact sits under
        `.skeleton-filled/<cluster>/src`, not `src/`.)

        Contrast `_wipe_paths` below, which is explicit — it names its paths
        and logs every deletion. The reset was removing a thousand times as
        much and logging one line.

        A rename, not a copy: same filesystem, so it is instant regardless of
        the ~361 MB of build output under `.skeleton-filled`, and it cannot fail
        half-way and leave a partial archive standing in for a real one.
        Deliberately never pruned — an unbounded archive costs disk, and pruning
        is the class of operation this whole item exists because of."""
        present = [d for d in RECONSTRUCTION_DIRS if (project_dir / d).is_dir()]
        if not present:
            return None
        # The destination must be EMPTY, not merely created. `shutil.move` into
        # an existing directory moves the source INSIDE it, so a second restart
        # landing on the same timestamp would produce
        # `<stamp>/.skeleton-filled/.skeleton-filled` and leave the first
        # archive's tree buried under the second's. Two restarts inside one
        # second is exactly what a crash-relaunch loop does.
        root = project_dir.parent / f"{project_dir.name}{ARCHIVE_DIR_SUFFIX}"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest, n = root / stamp, 1
        while dest.exists():
            n += 1
            dest = root / f"{stamp}-{n}"
        dest.mkdir(parents=True)
        for name in present:
            shutil.move(str(project_dir / name), str(dest / name))
        self.log(f"  [{self.label}] ARCHIVED {', '.join(present)} -> {dest}")
        self.log("    the reset below would have deleted these; the reconstruction "
                 "is there, and .skeleton/<cluster> + .skeleton-filled/<cluster> "
                 "must be restored as a PAIR (t1.5 re-authors the layout per run, "
                 "so bodies from one run do not compile against another's skeleton).")
        return dest

    def _describe_reset(self, project_dir: Path, tag: str) -> None:
        """Log what `git reset --hard <tag>` is about to do to TRACKED files.
        The reset itself prints nothing per file, and in a workspace whose
        .gitignore is `/target/` + `/Cargo.lock` everything the pipeline writes
        is tracked — so without this line the record of a restart says
        "resetting to <tag>" and nothing about the `.phase_c_logs/`, the
        `src/*.gen_tests_*.rs` or the `pipeline_knowledge.sqlite` it removed.
        `git diff --name-status <tag>` compares the tag with the WORKING TREE,
        which is exactly the set the reset discards: `A` = present here, absent
        at the tag (deleted); `D` = the reverse (restored); anything else is
        reverted in place."""
        try:
            out = subprocess.run(
                ["git", "diff", "--name-status", tag],
                cwd=project_dir, capture_output=True, text=True, errors="replace", check=True,
            ).stdout
        except subprocess.CalledProcessError as e:
            self.log(f"  [{self.label}] could not describe the reset to {tag}: {e}")
            return
        removed: list[str] = []
        restored: list[str] = []
        reverted: list[str] = []
        for line in out.splitlines():
            fields = line.split("\t")
            status = fields[0][:1] if fields else ""
            if status in ("R", "C") and len(fields) >= 3:   # rename/copy: old restored, new removed
                restored.append(fields[1])
                removed.append(fields[2])
            elif status == "A":
                removed.append(fields[-1])
            elif status == "D":
                restored.append(fields[-1])
            elif status:
                reverted.append(fields[-1])
        if removed:
            self.log(f"  [{self.label}] the reset to {tag} deletes {len(removed)} tracked "
                     f"file(s) the tag lacks: {_group_by_top_level(removed)}")
        if reverted:
            self.log(f"  [{self.label}] and reverts {len(reverted)} file(s): "
                     f"{_group_by_top_level(reverted)}")
        if restored:
            self.log(f"  [{self.label}] and restores {len(restored)} file(s): "
                     f"{_group_by_top_level(restored)}")

    def _reset_hard(self, project_dir: Path, tag: str) -> None:
        self._describe_reset(project_dir, tag)
        self.log(f"  [{self.label}] resetting workspace to {tag}")
        subprocess.run(["git", "reset", "--hard", tag],
                       cwd=project_dir, check=True, capture_output=True)

    def _wipe_paths(self, root: Path, paths: Iterable[Path]) -> None:
        for p in paths:
            shown = p.relative_to(root) if p.is_relative_to(root) else p
            if p.is_dir():
                shutil.rmtree(p)
                self.log(f"  Deleted {shown}/")
            elif p.exists():
                p.unlink()
                self.log(f"  Deleted {shown}")

    def _rewrite_state(self, project_dir: Path,
                       keep: Callable[[dict[str, object]], dict[str, object]]) -> None:
        state = self.read_state(project_dir)
        state["phases"] = keep(state["phases"])
        state["current"] = self.label
        self.write_state(project_dir, state)


@dataclass(frozen=True, slots=True)
class RestoreFromAugmented(_WorkspaceRestore):
    """Reset-to-augmentation-tag operation with its collaborators injected.
    Single-module workspaces only — errors loudly if the tag is missing or
    more than one module was augmented."""

    label: ClassVar[str] = "restart-from-augmented"

    def _find_restore_tag(self, project_dir: Path) -> str:
        tags = list_git_tags(project_dir, f"{AUGMENTED_TAG_PREFIX}*")
        if not tags:
            raise RuntimeError(
                "No task0c-augmented-* tag in the workspace. "
                "There is no augmented restore point — use --clean for a full wipe instead."
            )
        if len(tags) > 1:
            raise RuntimeError(
                f"Multi-module restore not supported in this version. Found tags: {tags}. "
                "Use --clean for a full wipe."
            )
        return tags[0]

    def _wipe_iteration_artifacts(self, root: Path, project_dir: Path, module_name: str) -> None:
        self._wipe_paths(root, _iteration_artifact_paths(project_dir, [module_name]))

    def run(self, root: Path, project_dir: Path) -> None:
        """Restore workspace to the task0c augmentation commit and clear only
        iteration state (strip / task1+ artefacts). Task0a/0b/0c outputs kept."""
        restore_tag = self._find_restore_tag(project_dir)
        module_name = restore_tag[len(AUGMENTED_TAG_PREFIX):]

        self._archive_reconstruction(project_dir)
        self._reset_hard(project_dir, restore_tag)
        self._wipe_iteration_artifacts(root, project_dir, module_name)

        # Pipeline state file: clear strip/task1+ entries so they re-run; keep
        # task0a/0b/0c entries so those tasks skip via sentinel check.
        self._rewrite_state(project_dir, drop_iteration_phases)
        self.log("  Cleared iteration entries from pipeline_state.md")
        self.log(f"  Workspace restored to {restore_tag}; "
                 "task0a/0b/0c kept, strip + task1+ will re-run.")


@dataclass(frozen=True, slots=True)
class RestoreFromPreAugment(_WorkspaceRestore):
    """Reset to the commit task0c built on (`task0c-pre-<module>`), so that
    task0c itself re-runs against the original source and tests.

    Accepts several tags when they name ONE commit — the augmentation runs for
    every module before any strip, so all of a workspace's pre-augmentation
    tags are written at the same HEAD; tags on different commits mean the
    workspace's history is not one this restore understands, and it refuses."""

    label: ClassVar[str] = "restart-from-pre-augment"

    def _find_restore_tags(self, project_dir: Path) -> tuple[list[str], str]:
        tags = list_git_tags(project_dir, f"{PRE_AUGMENT_TAG_PREFIX}*")
        if not tags:
            raise RuntimeError(
                "No task0c-pre-* tag in the workspace. The orchestrator writes it right "
                "before task0c on a git-managed run; a workspace built before that has none. "
                "Add it by hand at the `pipeline: task0b done` commit "
                "(git -C <workspace> tag task0c-pre-<module> <sha>), or use --clean for a "
                "full re-clone."
            )
        commits = {t: _commit_of(project_dir, t) for t in tags}
        if len(set(commits.values())) > 1:
            raise RuntimeError(
                f"task0c-pre-* tags name different commits: {commits}. A pre-augmentation "
                "restore point is one commit; delete the stale tag(s) or use --clean."
            )
        return tags, commits[tags[0]]

    def _git_clean(self, project_dir: Path) -> None:
        """`git clean -fd`, logging every path it removed. `-fd` and NOT `-x`:
        the workspace's .gitignore is `/target/` + `/Cargo.lock`, so `-x` would
        delete cargo's build cache — hundreds of MB and minutes of compile the
        next sweep would pay back for nothing, since ignored files are by
        definition not artefacts the pipeline tracks and none of them make a
        phase read as done. Untracked-but-not-ignored files are exactly the
        ones a git-managed run should never have left behind, and they go."""
        out = subprocess.run(["git", "clean", "-fd"],
                             cwd=project_dir, check=True, capture_output=True, text=True, errors="replace").stdout
        for line in out.splitlines():
            if line.startswith("Removing "):
                self.log(f"  Deleted {line[len('Removing '):]} (untracked)")

    def _wipe_augmentation_artifacts(self, root: Path, project_dir: Path,
                                     module_names: Iterable[str]) -> None:
        """Everything the augmented restore wipes, plus what task0c itself
        wrote: its attempt ledger and the sentinels the cluster augmentor
        reads as "already augmented" (`is_cluster_augmented` /
        `is_module_augmented`) — with those on disk the re-run would skip the
        very phase this restore exists to repeat. In a workspace that tracks
        them the reset has already removed them; this is the explicit,
        logged pass for the ones it did not."""
        paths = _iteration_artifact_paths(project_dir, module_names)
        paths += [
            project_dir / TASK0C_ATTEMPTS_LOG_NAME,
            project_dir / CLUSTER_AUGMENTED_DIR,
            project_dir / MODULE_AUGMENTED_DIR,
        ]
        self._wipe_paths(root, paths)

    def _find_behaviour_tags(self, project_dir: Path, pre_commit: str,
                             module_names: Iterable[str]) -> tuple[list[str], str] | None:
        """The behaviour restore point (`task0d-done-<module>`), or None when
        the workspace has none (it predates the point, or its harvest never
        completed before a task0c). A point that is present but is not one
        commit, does not cover the pre-augmentation tag's modules or is not
        built on that tag is refused: keeping it would keep a harvest of some
        other tree."""
        tags = list_git_tags(project_dir, f"{BEHAVIOUR_TAG_PREFIX}*")
        if not tags:
            return None
        remedy = ("pass --reharvest-behaviours (restart_from.sh task0d) to delete it and "
                  "harvest again")
        commits = {t: _commit_of(project_dir, t) for t in tags}
        if len(set(commits.values())) > 1:
            raise RuntimeError(f"task0d-done-* tags name different commits: {commits}; {remedy}.")
        modules = {t[len(BEHAVIOUR_TAG_PREFIX):] for t in tags}
        if modules != set(module_names):
            raise RuntimeError(f"task0d-done-* tags cover module(s) {sorted(modules)}, the "
                               f"pre-augmentation tags {sorted(module_names)}; {remedy}.")
        commit = commits[tags[0]]
        built_on = subprocess.run(["git", "merge-base", "--is-ancestor", pre_commit, commit],
                                  cwd=project_dir, capture_output=True).returncode == 0
        if not built_on:
            raise RuntimeError(f"{tags[0]} ({commit[:10]}) is not built on the pre-augmentation "
                               f"commit {pre_commit[:10]}; {remedy}.")
        return tags, commit

    def _delete_tags(self, project_dir: Path, tags: Iterable[str]) -> None:
        for tag in tags:
            subprocess.run(["git", "tag", "-d", tag], cwd=project_dir,
                           check=True, capture_output=True)
            self.log(f"  [{self.label}] deleted {tag} (the harvest is re-run; the next "
                     "complete harvest writes it again)")

    def run(self, root: Path, project_dir: Path, *, reharvest: bool = False) -> None:
        """Restore the workspace to the commit task0c builds on: archive the
        reconstruction, hard-reset, clean untracked files, wipe task0c/strip/
        task1+ artefacts, and rewrite pipeline_state.md.

        The commit is the behaviour restore point (`task0d-done-<module>`,
        after task0d) when the workspace has one: the harvest and its
        `task0d/<cluster>` entries are kept and task0d skips. With `reharvest`,
        or in a workspace without that point, it is `task0c-pre-<module>`
        (before task0d) and only cfg_scan, test_cmd, task0a* and task0b are
        kept; `reharvest` also deletes the behaviour tags, which a resumed
        harvest would otherwise never re-point."""
        tags, pre_commit = self._find_restore_tags(project_dir)
        module_names = [t[len(PRE_AUGMENT_TAG_PREFIX):] for t in tags]
        behaviour = None
        if reharvest:
            self._delete_tags(project_dir, list_git_tags(project_dir, f"{BEHAVIOUR_TAG_PREFIX}*"))
        else:
            behaviour = self._find_behaviour_tags(project_dir, pre_commit, module_names)
        target, commit = (behaviour[0][0], behaviour[1]) if behaviour else (tags[0], pre_commit)

        self._archive_reconstruction(project_dir)
        self._reset_hard(project_dir, target)
        self.log(f"  [{self.label}] at {commit[:10]} for module(s) {', '.join(module_names)}")
        self._git_clean(project_dir)
        self._wipe_augmentation_artifacts(root, project_dir, module_names)

        self._rewrite_state(project_dir, lambda phases: drop_pre_augment_phases(
            phases, keep_behaviours=behaviour is not None))
        if behaviour is not None:
            self.log("  Cleared task0c and later entries from pipeline_state.md "
                     "(kept cfg_scan, test_cmd, task0a*, task0b, task0d/*)")
            self.log(f"  Workspace restored to {target}; task0a/0b and the task0d behaviour "
                     "harvest kept, task0c + strip + task1+ will re-run "
                     "(--reharvest-behaviours re-runs task0d too).")
            return
        self.log("  Cleared task0c and later entries from pipeline_state.md "
                 "(kept cfg_scan, test_cmd, task0a*, task0b)")
        why = ("re-harvest requested" if reharvest else
               "no task0d-done-* tag in this workspace; the harvest writes it this run")
        self.log(f"  Workspace restored to {tags[0]}; task0a/0b kept, task0d + task0c + "
                 f"strip + task1+ will re-run ({why}).")
