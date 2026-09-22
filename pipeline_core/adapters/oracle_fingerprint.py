"""The oracle a cluster tree is scored by, as a fingerprint the ledger can
compare.

WHAT THE ORACLE IS, from the code that runs it. `run_phase_c_tdd` runs
`cargo test` with the cluster tree as cwd (`target_src_dir`, i.e.
`.skeleton-filled/<cluster>`), and `task2_generator` makes that directory a
runnable crate root before the loop starts: it propagates every
`<stem>.gen_tests_<N>.rs` sibling from the workspace `src/` into the tree's
`src/` (34 of them on the live anyhow tree, 2026-08-26) and symlinks the
workspace `tests/` to `<tree>/tests`. So what grades a body is: the tree's
gen_tests siblings, the crate's `tests/**/*.rs` (integration tests, the
`ui/` compiletest cases, `common/`), and the baked behaviour suite
`.skeleton/_behaviours_<cluster>.json` the t3 oracle reads. Those three,
keyed by their path, are what this module digests.

WHY THE LEDGER NEEDS IT. `LedgerEntry` carried layout identity only, and a
layout is not an oracle: the planned cold run re-runs t0c and regenerates
the gen_tests while t1.5 leaves most files byte-identical, so the cluster-
entry re-seed would have put back 73 of 85 latest stable units — every one
proven by tests the run was replacing (`scripts/ledger_reseed_exposure.py`,
2026-08-26). The fingerprint is stricter than the per-cluster test subset
(a nightly-only test file moving also moves stable's oracle); that costs a
re-derivation and never a body vouched for by tests that no longer exist,
which is the direction every gate in `domain/body_ledger.py` chooses.
"""
from __future__ import annotations

import subprocess
from contextlib import suppress
from pathlib import Path

from pipeline_core.adapters.discovery import is_gen_tests_file
from pipeline_core.domain.body_ledger import oracle_fingerprint
from pipeline_core.domain.oracle_guard import OracleDrift, oracle_drift

BEHAVIOURS_KEY = ".skeleton/_behaviours_{cluster}.json"


def cluster_oracle_files(src_dir: Path, project_dir: Path,
                         cluster: str) -> list[tuple[str, str]]:
    """`(rel_path, text)` for every file that grades the cluster tree at
    `src_dir` (the cluster crate root — what cargo runs in).

    The `tests/` directory is read from the cluster root when it exists
    (the symlink the fill phase creates) and from the workspace otherwise
    (a cluster entry that runs before the symlink), so the key is
    `tests/<rel>` either way and the fingerprint does not move with the
    symlink. Exposed separately from the digest so a caller can SEE the set
    — an empty gen_tests list on a tree that has siblings is a rooting bug,
    not a no-op, and a fingerprint alone cannot show it."""
    root = Path(src_dir)
    out: list[tuple[str, str]] = []

    def _read(p: Path) -> str | None:
        with suppress(OSError):
            return p.read_text(encoding="utf-8", errors="replace")
        return None

    src = root / "src"
    if src.is_dir():
        for p in sorted(src.rglob("*.rs")):
            if not (p.is_file() and is_gen_tests_file(p.name)):
                continue
            text = _read(p)
            if text is not None:
                out.append((p.relative_to(root).as_posix(), text))
    tests = root / "tests"
    if not tests.is_dir():
        tests = Path(project_dir) / "tests"
    if tests.is_dir():
        for p in sorted(tests.rglob("*.rs")):
            if not p.is_file():
                continue
            text = _read(p)
            if text is not None:
                out.append(("tests/" + p.relative_to(tests).as_posix(), text))
    beh = Path(project_dir) / ".skeleton" / f"_behaviours_{cluster}.json"
    if beh.is_file():
        text = _read(beh)
        if text is not None:
            out.append((BEHAVIOURS_KEY.format(cluster=cluster), text))
    return out


def cluster_oracle_fp(src_dir: Path, project_dir: Path, cluster: str) -> str:
    """`oracle_fingerprint` over `cluster_oracle_files` — the value the loop
    stamps on every ledger row and hands to the gate."""
    return oracle_fingerprint(cluster_oracle_files(src_dir, project_dir,
                                                   cluster))


# --- the seal ----------------------------------------------------------------
#
# The fingerprint above says THAT the oracle moved; it cannot say what moved or
# who moved it, and a moved fingerprint is otherwise silent (run
# `92187-f71f12bf`, 2026-09-22: `1c3b0664` -> `580d469e` from an edit made
# outside the pipeline, visible only as a new hash on the ledger line). Strip is
# the last writer of the oracle, so strip seals it with a tag at the strip
# commit, and every launch compares the workspace against that tag before
# anything commits (`domain/oracle_guard.py`).
#
# The files are NOT made read-only. That was tried the same day and broke the
# assertion probe on every iteration of run `190214-8de7c7a5`: the probe stages
# `tests/common/mod.rs` with `copy2`, which carries the mode, and its next copy
# onto the read-only stage file failed `EACCES`. Every copier of the oracle that
# preserves modes (`copy2`, `copytree`'s default) inherits that failure, and
# they cannot all be enumerated, so the refusal is the seal check alone.

ORACLE_TAG_PREFIX = "oracle-"


def oracle_tag(module: str) -> str:
    return f"{ORACLE_TAG_PREFIX}{module}"


def _is_oracle_path(rel: str) -> bool:
    """The workspace half of `cluster_oracle_files`: `tests/**/*.rs` (build
    output under a `target/` excluded), the gen_tests siblings under `src/`,
    and the baked behaviour suites."""
    parts = rel.split("/")
    if parts[0] == "tests":
        return rel.endswith(".rs") and "target" not in parts[1:-1]
    if parts[0] == "src":
        return is_gen_tests_file(parts[-1])
    return (len(parts) == 2 and parts[0] == ".skeleton"
            and parts[1].startswith("_behaviours_") and parts[1].endswith(".json"))


def workspace_oracle_files(project_dir: Path) -> dict[str, str]:
    """`rel path -> text` for every oracle file in the WORKSPACE (not a
    cluster tree: the tree's `tests` is a symlink here and its gen_tests are
    copies of these)."""
    root = Path(project_dir)
    out: dict[str, str] = {}
    for base in ("tests", "src"):
        d = root / base
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if _is_oracle_path(rel):
                with suppress(OSError):
                    out[rel] = p.read_text(encoding="utf-8", errors="replace")
    sk = root / ".skeleton"
    if sk.is_dir():
        for p in sorted(sk.glob("_behaviours_*.json")):
            rel = p.relative_to(root).as_posix()
            with suppress(OSError):
                out[rel] = p.read_text(encoding="utf-8", errors="replace")
    return out


def sealed_oracle_files(project_dir: Path, tag: str) -> dict[str, str] | None:
    """The oracle as committed at `tag`, or None when the tag does not exist
    (a workspace sealed before the seal existed, or not git-managed)."""
    root = Path(project_dir)
    ls = subprocess.run(["git", "ls-tree", "-r", "--name-only", tag],
                        cwd=root, capture_output=True, text=True)
    if ls.returncode != 0:
        return None
    rels = [r for r in ls.stdout.splitlines() if r and _is_oracle_path(r)]
    if not rels:
        return {}
    batch = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=root, capture_output=True,
        input="".join(f"{tag}:{r}\n" for r in rels).encode("utf-8"),
        check=True)
    out: dict[str, str] = {}
    buf = batch.stdout
    pos = 0
    for rel in rels:
        nl = buf.index(b"\n", pos)
        header = buf[pos:nl].decode("utf-8", errors="replace").split()
        pos = nl + 1
        if len(header) < 3 or header[1] == "missing":
            continue
        size = int(header[2])
        out[rel] = buf[pos:pos + size].decode("utf-8", errors="replace")
        pos += size + 1                       # the object's trailing newline
    return out


def workspace_oracle_drift(project_dir: Path, module: str) -> OracleDrift | None:
    """The workspace oracle against the seal of `module`; None when unsealed."""
    tag = oracle_tag(module)
    sealed = sealed_oracle_files(project_dir, tag)
    if sealed is None:
        return None
    return oracle_drift(workspace_oracle_files(project_dir), sealed, seal=tag)
