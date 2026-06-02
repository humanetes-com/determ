"""Workspace adapter — the I/O layer for the git-managed project workspace under tmp/.

Relocated verbatim from pipeline.py (slice 4b). Bodies are unchanged; former module
globals (``_log``, ``_emit``, ``_git_managed``) become an injected :class:`WorkspaceDeps`
and an explicit ``managed`` argument. Adapter imports only stdlib — never ``pipeline.py``.

Two shapes are offered:
- module-level functions (``git_commit``/``git_tag``/…) that take ``deps`` + an explicit
  ``project_dir`` — the form pipeline.py's thin shims delegate to during migration;
- :class:`GitWorkspace`, a root-bound object satisfying ports.Workspace, the target the
  call sites move onto in slice 5.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspaceDeps:
    """Collaborators the workspace I/O needs, injected by the composition root."""

    log: Callable[[str], None]
    emit: Callable[..., None]


def git_default_branch(project_dir: Path) -> str:
    """Return the default branch name (main, master, etc.)."""
    for cmd in [
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
    ]:
        try:
            out = subprocess.check_output(cmd, cwd=project_dir,
                                          stderr=subprocess.DEVNULL, text=True).strip()
            return out.split("/")[-1]
        except Exception:
            pass
    # Fallback: check which of main/master exists locally
    for branch in ("main", "master"):
        r = subprocess.run(["git", "rev-parse", "--verify", branch],
                           cwd=project_dir, capture_output=True)
        if r.returncode == 0:
            return branch
    return "main"


def git_file_hash(root: Path, filename: str) -> str:
    try:
        result = subprocess.run(
            ["git", "log", "--format=%h", "-1", filename],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        h = result.stdout.strip()
        return h if h else "unknown"
    except Exception:
        return "unknown"


def git_commit(deps: WorkspaceDeps, project_dir: Path, message: str, *, managed: bool) -> bool:
    """Stage all changes and commit. Returns True if a commit was made."""
    if not managed:
        return False
    try:
        subprocess.run(["git", "add", "-A"], cwd=project_dir,
                       check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                              cwd=project_dir, capture_output=True)
        if diff.returncode == 0:
            return False  # nothing staged
        subprocess.run(
            ["git", "commit", "-m", message, "--no-gpg-sign"],
            cwd=project_dir, check=True, capture_output=True,
        )
        deps.log(f"  [git] committed: {message}")
        try:
            _sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                                  cwd=project_dir, capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            _sha = ""
        deps.emit("git.commit", data={"message": message, "workspace_sha": _sha})
        return True
    except Exception as e:
        deps.log(f"  [git] commit failed: {e}")
        deps.emit("git.error", data={"message": message, "error": str(e)[:300]})
        return False


def git_tag(deps: WorkspaceDeps, project_dir: Path, tag: str, *, managed: bool) -> None:
    """Create or update a lightweight tag at HEAD."""
    if not managed:
        return
    try:
        subprocess.run(["git", "tag", "-f", tag], cwd=project_dir,
                       check=True, capture_output=True)
        deps.log(f"  [git] tagged: {tag}")
    except Exception as e:
        deps.log(f"  [git] tag failed: {e}")


def git_clean_reset(deps: WorkspaceDeps, project_dir: Path) -> None:
    """Reset workspace to default branch and recreate the pipeline branch."""
    try:
        default = git_default_branch(project_dir)
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=project_dir, capture_output=True)
        subprocess.run(["git", "clean", "-fd"], cwd=project_dir, capture_output=True)
        subprocess.run(["git", "checkout", default], cwd=project_dir,
                       check=True, capture_output=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{default}"],
                       cwd=project_dir, capture_output=True)
        subprocess.run(["git", "branch", "-D", "pipeline"],
                       cwd=project_dir, capture_output=True)  # ok if not exists
        subprocess.run(["git", "checkout", "-b", "pipeline"],
                       cwd=project_dir, check=True, capture_output=True)
        deps.log(f"  [git] reset to {default}, recreated pipeline branch")
    except Exception as e:
        deps.log(f"  [git] clean reset failed: {e}")


class GitWorkspace:
    """Root-bound git workspace satisfying ports.Workspace. Migration target (slice 5)."""

    def __init__(self, root: Path, deps: WorkspaceDeps, *, managed: bool) -> None:
        self.root = Path(root)
        self._deps = deps
        self._managed = managed

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def write(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def commit(self, message: str, *, tag: str | None = None) -> None:
        git_commit(self._deps, self.root, message, managed=self._managed)
        if tag:
            git_tag(self._deps, self.root, tag, managed=self._managed)

    def reset_clean(self) -> None:
        git_clean_reset(self._deps, self.root)

    def default_branch(self) -> str:
        return git_default_branch(self.root)

    def file_hash(self, filename: str) -> str:
        return git_file_hash(self.root, filename)
