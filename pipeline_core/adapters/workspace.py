"""Workspace adapter — the I/O layer for the git-managed project workspace under tmp/.

Relocated verbatim from pipeline.py. Bodies are unchanged; former module
globals (``_log``, ``_emit``, ``_git_managed``) become an injected :class:`WorkspaceDeps`
and an explicit ``managed`` argument. Adapter imports only stdlib — never ``pipeline.py``.

Two shapes are offered:
- module-level functions (``git_commit``/``git_tag``/…) that take ``deps`` + an explicit
  ``project_dir`` — the form pipeline.py's thin shims delegate to during migration;
- :class:`GitWorkspace`, a root-bound object satisfying ports.Workspace, the target the
  call sites move onto.
"""

from __future__ import annotations

import shutil
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


def git_commit_paths(
    deps: WorkspaceDeps, project_dir: Path, rel_paths: list[str], message: str,
    *, managed: bool,
) -> bool:
    """Stage ONLY ``rel_paths`` and commit. Focused alternative to
    ``git_commit``'s ``git add -A`` — checkpoints the fill tree without
    sweeping in build artifacts (``.skeleton/**/target``, absent at the earlier
    whole-tree checkpoints but present once phase-c has built). Returns True if
    a commit was made."""
    if not managed:
        return False
    try:
        subprocess.run(["git", "add", "--", *rel_paths], cwd=project_dir,
                       check=True, capture_output=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", *rel_paths],
            cwd=project_dir, capture_output=True)
        if staged.returncode == 0:
            return False  # nothing staged under these paths
        subprocess.run(
            ["git", "commit", "-m", message, "--no-gpg-sign"],
            cwd=project_dir, check=True, capture_output=True,
        )
        deps.log(f"  [git] committed: {message}")
        deps.emit("git.commit", data={"message": message})
        return True
    except Exception as e:
        deps.log(f"  [git] fill-tree commit failed: {e}")
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


def write_workspace_stub(crate_type: str, module_dir: Path) -> Path:
    """
    Write a minimal entry-point stub so Cargo can parse the workspace.
    Bin crates get `fn main() {}`, all others get an empty lib stub.
    Always writes (creates or overwrites) so state is deterministic.
    Returns the path written.
    """
    if "bin" in crate_type.lower() and "lib" not in crate_type.lower():
        entry = module_dir / "src" / "main.rs"
        content = "fn main() {}\n"
    else:
        entry = module_dir / "src" / "lib.rs"
        content = "// pipeline workspace stub\n"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(content)
    return entry


def is_git_repo(path: Path) -> bool:
    """True if `path` is inside a git working tree."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path, check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def setup_workspace(deps: WorkspaceDeps, repo: str, workspace_dir: Path,
                    force_fresh: bool = False) -> Path:
    """
    Prepare a git-managed workspace for the given repo (URL or local path).

    - URL       → git clone into workspace_dir/{name}
    - local git → copy into workspace_dir/{name} (preserves history)
    - local dir → copy into workspace_dir/{name}, git init + initial commit

    In all cases, checks out (or creates) a 'pipeline' branch and returns
    the workspace path.

    If `force_fresh` is True AND the repo source is a URL, an existing
    workspace is deleted and re-cloned. This is what --clean + --repo means:
    nuke target/, stale gen_tests artifacts, half-applied LLM edits — start
    from a true upstream HEAD. For local-path repos we never auto-delete the
    user's source tree.
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Derive workspace name from repo
    # Normalize various GitHub URL forms to HTTPS (SSH requires a key; HTTPS works everywhere).
    # git@github.com:user/repo.git  →  https://github.com/user/repo
    # github.com/user/repo          →  https://github.com/user/repo
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo.split(":", 1)[1]
    elif repo.startswith("github.com/"):
        repo = "https://" + repo

    name = repo.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    dest = workspace_dir / name

    _is_url = repo.startswith("http://") or repo.startswith("https://") \
              or repo.startswith("git@") or repo.startswith("ssh://")

    if dest.exists() and force_fresh and _is_url:
        deps.log(f"  [git] --clean + URL repo: removing {dest} for fresh re-clone")
        shutil.rmtree(dest)

    if dest.exists():
        deps.log(f"  [git] workspace exists at {dest} — resuming")
    elif _is_url:
        deps.log(f"  [git] cloning {repo} → {dest}")
        subprocess.run(["git", "clone", repo, str(dest)], check=True)
    else:
        src = Path(repo).resolve()
        if not src.exists():
            raise RuntimeError(f"Repo path not found: {src}")
        deps.log(f"  [git] copying {src} → {dest}")
        shutil.copytree(str(src), str(dest))
        if not (dest / ".git").exists():
            deps.log("  [git] no .git found — initialising git repo")
            subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "initial: source snapshot", "--no-gpg-sign"],
                cwd=dest, check=True, capture_output=True,
            )
            deps.log("  [git] initial commit done")

    # Checkout or create pipeline branch
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=dest, capture_output=True, text=True,
    ).stdout.strip()
    if current != "pipeline":
        branches = subprocess.run(
            ["git", "branch", "--list", "pipeline"],
            cwd=dest, capture_output=True, text=True,
        ).stdout.strip()
        if branches:
            subprocess.run(["git", "checkout", "pipeline"],
                           cwd=dest, check=True, capture_output=True)
            deps.log("  [git] checked out existing pipeline branch")
        else:
            subprocess.run(["git", "checkout", "-b", "pipeline"],
                           cwd=dest, check=True, capture_output=True)
            deps.log("  [git] created pipeline branch")

    return dest


class GitWorkspace:
    """Root-bound git workspace satisfying ports.Workspace. Migration target."""

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
