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

from contextlib import suppress
from pathlib import Path

from pipeline_core.adapters.discovery import is_gen_tests_file
from pipeline_core.domain.body_ledger import oracle_fingerprint

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
